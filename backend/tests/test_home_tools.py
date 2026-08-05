"""
Feature 1 — Home & IoT tools + the entity-id lock.

The suite never touches a real hub: HOME_SERVICE_FACTORY is pointed at a fake
that RECORDS every service call the tools build, so tests assert the exact
(domain, service, data) that would reach the house — which matters more here
than for any other integration, because the failure mode is a door unlocking.

The entity-id lock mirrors the Part 4 event-id lock:
  1. tool-level:     entity_id is required; the service is chosen from a fixed
                     map, never from model text; unsupported domains refused.
  2. planner-level:  _entity_id_violation rejects an entity_id no read step in
                     THIS plan returned.
  3. approval-level: _step_action_detail renders the full contract, and
                     _enrich_entity_action_detail names the real device + room.
And placeholder_resolver fills a PENDING entity_id from a read that pins one
device — the home mirror of the event-id fill.
"""
import pytest

import app.tools  # noqa: F401 — registers every tool
from app.agents import placeholder_resolver
from app.agents.planner import (
    _completed_devices,
    _enrich_entity_action_detail,
    _entity_id_grounding,
    _entity_id_violation,
    _step_action_detail,
)
from app.agents.planner import AgentPlanner
from app.agents.rendering import _RESULT_FORMATTERS
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.integrations import home_assistant
from app.integrations.home_assistant import (
    Device,
    HomeApiError,
    HomeNotConnectedError,
    domain_of,
    validate_base_url,
)
from app.tools import home_tools
from app.tools.registry import execute_tool, registry


# ------------------------------------------------------------- fake the hub

class FakeHub:
    """Records every call. `states` is the device inventory the tools read."""

    def __init__(self, devices=None, fail=None):
        self.devices = devices if devices is not None else _default_devices()
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = fail
        self.state_calls: list[str] = []

    async def states(self, *, force: bool = False):
        if self.fail:
            raise self.fail
        return self.devices

    async def state(self, entity_id: str):
        self.state_calls.append(entity_id)
        if self.fail:
            raise self.fail
        return next((d for d in self.devices if d.entity_id == entity_id), None)

    async def call_service(self, domain: str, service: str, data: dict):
        if self.fail:
            raise self.fail
        self.calls.append((domain, service, dict(data)))
        return {}

    async def ping(self):
        if self.fail:
            raise self.fail
        return "2026.8.1"


def _device(entity_id, name, state="off", area="", **attrs):
    return Device(
        entity_id=entity_id, name=name, domain=domain_of(entity_id),
        state=state, area=area, attributes=attrs,
    )


def _default_devices():
    return [
        _device("light.kitchen_main", "Kitchen Lights", "off", "Kitchen"),
        _device("light.bedroom_lamp", "Bedroom Lamp", "on", "Bedroom"),
        _device("lock.front_door", "Front Door", "locked", "Hallway"),
        _device("climate.living_room", "Living Room Thermostat", "heat", "Living Room",
                current_temperature=19.5),
        _device("scene.movie_night", "Movie Night", "unknown", ""),
        _device("sensor.outdoor_temp", "Outdoor Temperature", "12.4", "Garden"),
    ]


@pytest.fixture
def hub(monkeypatch):
    """A fake hub wired into the factory seam AND an enabled config, so the
    tools' own `_client()` (which reads app_settings) resolves to it."""
    fake = FakeHub()
    monkeypatch.setattr(home_assistant, "HOME_SERVICE_FACTORY", lambda: fake)

    from app.core.app_settings import HomeConfig

    async def _config(_db):
        return HomeConfig(enabled=True, base_url="http://hub.local:8123")

    monkeypatch.setattr("app.core.app_settings.get_home_config", _config)

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(home_tools, "SESSION_FACTORY", lambda: _Session())
    return fake


# ============================================================ base URL gate

@pytest.mark.parametrize("raw,expected", [
    ("http://homeassistant.local:8123", "http://homeassistant.local:8123"),
    ("https://ha.example.com/", "https://ha.example.com"),
    ("192.168.1.50:8123", "http://192.168.1.50:8123"),  # bare host gets http://
    ("  http://hub.local:8123/  ", "http://hub.local:8123"),
])
def test_base_url_is_normalized(raw, expected):
    assert validate_base_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ",
    "ftp://hub.local",
    "file:///etc/passwd",
    "http://hub.local:8123?token=x",   # a base URL carries no query
    "http://hub.local:8123#frag",
])
def test_a_bad_base_url_is_refused(raw):
    with pytest.raises(ValueError):
        validate_base_url(raw)


def test_the_tools_expose_no_url_parameter_at_all():
    """THE bound on where this client can point: a model chooses an entity, never
    an address. If a URL/host/path parameter ever appears on a home tool, the
    'the base URL can only come from the user's own configuration' guarantee in
    home_assistant.py's docstring is no longer true."""
    banned = ("url", "base_url", "host", "hostname", "path", "endpoint", "address")
    for name in ("list_devices", "get_device_state", "set_device_state",
                 "run_scene", "set_climate"):
        props = registry.get(name).definition().parameters.get("properties", {})
        assert not (set(props) & set(banned)), f"{name} exposes an address parameter"


# ================================================================ read tools

async def test_list_devices_returns_entity_ids_and_rooms(hub):
    result = await registry.get("list_devices").execute()
    assert result.success
    ids = [d["entity_id"] for d in result.output["devices"]]
    assert "light.kitchen_main" in ids
    assert result.output["count"] == len(_default_devices())
    assert "Kitchen" in result.output["areas"]


async def test_list_devices_filters_by_area_and_domain(hub):
    by_area = await registry.get("list_devices").execute(area="kitchen")
    assert [d["entity_id"] for d in by_area.output["devices"]] == ["light.kitchen_main"]

    by_domain = await registry.get("list_devices").execute(domain="lock")
    assert [d["entity_id"] for d in by_domain.output["devices"]] == ["lock.front_door"]


async def test_an_unknown_domain_filter_names_the_real_ones(hub):
    result = await registry.get("list_devices").execute(domain="toaster")
    assert not result.success
    assert "toaster" in result.error and "light" in result.error


async def test_get_device_state_reads_live_not_the_cached_list(hub):
    result = await registry.get("get_device_state").execute(entity_id="lock.front_door")
    assert result.success
    assert result.output["state"] == "locked"
    # It asked the hub for THAT entity rather than filtering a snapshot: a
    # state question must not be answered from a minute-old cache.
    assert hub.state_calls == ["lock.front_door"]


async def test_get_device_state_shapes_its_row_like_list_devices(hub):
    """Both reads must expose `devices: [...]`, because the entity-id grounding
    and the PENDING resolver read them through ONE code path. A different shape
    here would silently make get_device_state unable to ground a write."""
    result = await registry.get("get_device_state").execute(entity_id="light.kitchen_main")
    assert result.output["devices"][0]["entity_id"] == "light.kitchen_main"


async def test_a_missing_device_says_how_to_find_the_real_ids(hub):
    result = await registry.get("get_device_state").execute(entity_id="light.nope")
    assert not result.success
    assert "list_devices" in result.error


# =============================================================== write tools

async def test_set_device_state_calls_the_mapped_service(hub, db_session):
    result = await execute_tool(
        "set_device_state",
        {"entity_id": "light.kitchen_main", "state": "on"}, db_session, approved=True,
    )
    assert result.success
    assert hub.calls == [("light", "turn_on", {"entity_id": "light.kitchen_main"})]


async def test_a_lock_uses_lock_not_turn_on(hub, db_session):
    await execute_tool(
        "set_device_state",
        {"entity_id": "lock.front_door", "state": "unlock"}, db_session, approved=True,
    )
    assert hub.calls[0][:2] == ("lock", "unlock")


async def test_allowed_attributes_ride_along_and_unknown_ones_are_dropped(hub, db_session):
    await execute_tool(
        "set_device_state",
        {
            "entity_id": "light.kitchen_main", "state": "on",
            "attributes": {"brightness_pct": 80, "explode": True, "shell": "rm -rf /"},
        },
        db_session, approved=True,
    )
    _, _, data = hub.calls[0]
    assert data["brightness_pct"] == 80
    assert "explode" not in data and "shell" not in data


async def test_a_state_the_domain_does_not_accept_is_refused_before_any_call(hub, db_session):
    result = await execute_tool(
        "set_device_state",
        {"entity_id": "light.kitchen_main", "state": "unlock"}, db_session, approved=True,
    )
    assert not result.success
    assert hub.calls == []


async def test_a_sensor_cannot_be_switched(hub, db_session):
    result = await execute_tool(
        "set_device_state",
        {"entity_id": "sensor.outdoor_temp", "state": "on"}, db_session, approved=True,
    )
    assert not result.success
    assert "get_device_state" in result.error
    assert hub.calls == []


async def test_run_scene_refuses_anything_that_is_not_a_scene(hub, db_session):
    result = await execute_tool(
        "run_scene",
        {"entity_id": "light.kitchen_main"}, db_session, approved=True,
    )
    assert not result.success
    assert hub.calls == []


async def test_run_scene_activates_a_real_scene(hub, db_session):
    result = await execute_tool(
        "run_scene", {"entity_id": "scene.movie_night"}, db_session,
        approved=True,
    )
    assert result.success
    assert hub.calls == [("scene", "turn_on", {"entity_id": "scene.movie_night"})]


async def test_set_climate_sends_mode_then_temperature(hub, db_session):
    result = await execute_tool(
        "set_climate",
        {"entity_id": "climate.living_room", "temperature": 21.5, "mode": "heat"},
        db_session, approved=True,
    )
    assert result.success
    assert [(d, s) for d, s, _ in hub.calls] == [
        ("climate", "set_hvac_mode"), ("climate", "set_temperature"),
    ]
    assert hub.calls[1][2]["temperature"] == 21.5


@pytest.mark.parametrize("temperature", [-5, 0, 4.9, 35.1, 220, "warm"])
async def test_an_out_of_range_temperature_fails_rather_than_clamping(hub, db_session, temperature):
    """Clamping silently would set a temperature nobody asked for — a typo'd 220
    must fail, not quietly become 35."""
    result = await execute_tool(
        "set_climate",
        {"entity_id": "climate.living_room", "temperature": temperature},
        db_session, approved=True,
    )
    assert not result.success
    assert hub.calls == []


async def test_set_climate_with_nothing_to_change_is_refused(hub, db_session):
    result = await execute_tool(
        "set_climate", {"entity_id": "climate.living_room"}, db_session,
        approved=True,
    )
    assert not result.success
    assert hub.calls == []


# ===================================================== the structural gate

@pytest.mark.parametrize("tool,params", [
    ("set_device_state", {"entity_id": "light.kitchen_main", "state": "on"}),
    ("run_scene", {"entity_id": "scene.movie_night"}),
    ("set_climate", {"entity_id": "climate.living_room", "temperature": 21}),
])
async def test_an_unapproved_write_never_reaches_the_hub(hub, db_session, tool, params):
    """The house rule, on the one feature where breaking it is physical:
    execute_tool refuses a non-READ tool without approved=True BEFORE execute()
    runs, so no service call is built at all."""
    result = await execute_tool(tool, params, db_session, approved=False)
    assert not result.success
    assert result.requires_approval
    assert hub.calls == []


def test_the_permission_levels_are_what_the_gate_reads():
    from app.tools.registry import mutates
    assert registry.get("list_devices").permission_level == PermissionLevel.READ
    assert registry.get("get_device_state").permission_level == PermissionLevel.READ
    for name in ("set_device_state", "run_scene", "set_climate"):
        assert registry.get(name).permission_level == PermissionLevel.WRITE
        assert mutates(name) is True


def test_there_is_no_free_text_service_call_tool():
    """A `call_service(domain, service, data)` passthrough would let a plan reach
    anything on the hub — including HA's own shell_command and notify
    integrations. The fixed _DOMAIN_SERVICES map IS the safety boundary."""
    assert registry.get("call_service") is None
    assert registry.get("run_automation") is None
    for services in home_tools._DOMAIN_SERVICES.values():
        assert "shell_command" not in services.values()


# ============================================== not-connected degradation

@pytest.mark.parametrize("tool,params", [
    ("list_devices", {}),
    ("get_device_state", {"entity_id": "light.kitchen_main"}),
    ("set_device_state", {"entity_id": "light.kitchen_main", "state": "on"}),
    ("run_scene", {"entity_id": "scene.movie_night"}),
    ("set_climate", {"entity_id": "climate.living_room", "temperature": 21}),
])
async def test_a_missing_hub_degrades_to_a_clean_failure(monkeypatch, db_session, tool, params):
    """No hub configured is a NORMAL state, not a crash — every tool surfaces
    the stable message verbatim (the GoogleNotConnectedError contract)."""
    def _refuse():
        raise HomeNotConnectedError()

    monkeypatch.setattr(home_assistant, "HOME_SERVICE_FACTORY", _refuse)

    from app.core.app_settings import HomeConfig

    async def _config(_db):
        return HomeConfig(enabled=True, base_url="http://hub.local:8123")

    monkeypatch.setattr("app.core.app_settings.get_home_config", _config)

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(home_tools, "SESSION_FACTORY", lambda: _Session())

    result = await execute_tool(tool, params, db_session, approved=True)
    assert not result.success
    assert "Home Assistant is not connected" in result.error


async def test_the_feature_being_off_is_its_own_message(monkeypatch, hub):
    from app.core.app_settings import HomeConfig

    async def _off(_db):
        return HomeConfig(enabled=False, base_url="http://hub.local:8123")

    monkeypatch.setattr("app.core.app_settings.get_home_config", _off)
    result = await registry.get("list_devices").execute()
    assert not result.success
    assert "Settings" in result.error


async def test_an_api_error_is_reported_not_raised(monkeypatch, hub):
    hub.fail = HomeApiError("Home Assistant API error (HTTP 500): boom")
    result = await registry.get("list_devices").execute()
    assert not result.success
    assert "HTTP 500" in result.error


# ======================================================== the entity-id lock

def _read_step(devices, tool="list_devices"):
    return PlanStep(
        description="Find the devices", tool=tool, parameters={},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED,
        result=ToolResult(
            success=True,
            output={"devices": devices, "count": len(devices)},
            permission_level=PermissionLevel.READ,
        ),
    )


def _write_step(entity_id, tool="set_device_state", state="off"):
    params = {"entity_id": entity_id}
    if tool == "set_device_state":
        params["state"] = state
    return PlanStep(
        description=f"Turn off {entity_id}", tool=tool,
        parameters=params, permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )


_ROWS = [
    {"entity_id": "light.kitchen_main", "name": "Kitchen Lights",
     "area": "Kitchen", "state": "on", "domain": "light"},
]


def test_a_concrete_entity_id_is_ungrounded_at_draft_time():
    """Nothing is completed in a fresh plan, so ANY concrete id is rejected —
    which is what forces a read step + a PENDING placeholder."""
    plan = AgentPlan(goal="turn off the kitchen lights")
    assert _entity_id_grounding(plan) == set()
    feedback = _entity_id_violation([_write_step("light.kitchen_main")], set())
    assert feedback and "list_devices" in feedback and "PENDING" in feedback


def test_an_id_a_read_step_returned_is_grounded():
    plan = AgentPlan(goal="turn off the kitchen lights")
    plan.steps = [_read_step(_ROWS)]
    ids = _entity_id_grounding(plan)
    assert ids == {"light.kitchen_main"}
    assert _entity_id_violation([_write_step("light.kitchen_main")], ids) is None


def test_a_hallucinated_id_is_rejected_even_when_a_read_ran():
    """The incident this exists for: 'turn off the light' resolving to a
    light.bedroom nobody returned — which on a real hub might be a lock."""
    plan = AgentPlan(goal="turn off the light")
    plan.steps = [_read_step(_ROWS)]
    feedback = _entity_id_violation(
        [_write_step("light.bedroom")], _entity_id_grounding(plan)
    )
    assert feedback and "light.bedroom" in feedback


def test_a_pending_placeholder_is_not_judged_yet():
    assert _entity_id_violation(
        [_write_step("PENDING: the kitchen lights")], set()
    ) is None


@pytest.mark.parametrize("tool", ["set_device_state", "run_scene", "set_climate"])
def test_every_home_write_tool_is_covered_by_the_lock(tool):
    """A new home WRITE tool that is not in _ENTITY_ID_TOOLS would accept an
    invented entity id — the guard-coverage hole this project has shipped three
    times. Every WRITE tool in the module must be listed."""
    from app.agents.planner import _ENTITY_ID_TOOLS
    assert tool in _ENTITY_ID_TOOLS
    assert _entity_id_violation([_write_step("light.invented", tool=tool)], set())


def test_a_read_tool_is_never_gated_by_the_lock():
    step = PlanStep(
        description="read", tool="get_device_state",
        parameters={"entity_id": "light.anything"},
        permission_level=PermissionLevel.READ, requires_approval=False,
    )
    assert _entity_id_violation([step], set()) is None


def test_completed_devices_ignores_a_failed_read():
    plan = AgentPlan(goal="g")
    failed = _read_step(_ROWS)
    failed.status = StepStatus.FAILED
    plan.steps = [failed]
    assert _completed_devices(plan) == []


# ==================================================== the approval contract

def test_the_action_detail_renders_the_full_contract():
    detail = _step_action_detail(
        "set_device_state",
        {"entity_id": "light.kitchen_main", "state": "on",
         "attributes": {"brightness_pct": 80}},
    )
    assert "light.kitchen_main" in detail and "on" in detail
    assert "brightness_pct: 80" in detail


def test_set_climate_names_the_temperature_and_mode():
    detail = _step_action_detail(
        "set_climate",
        {"entity_id": "climate.living_room", "temperature": 21.5, "mode": "heat"},
    )
    assert "21.5" in detail and "heat" in detail


def test_run_scene_warns_that_a_scene_touches_several_devices():
    detail = _step_action_detail("run_scene", {"entity_id": "scene.movie_night"})
    assert "scene.movie_night" in detail and "several" in detail


def test_the_card_names_the_room_and_the_current_state():
    """Approving "turn off light.a1b2" tells the user nothing. The enrichment is
    code-derived from this plan's own reads — the LLM cannot author it."""
    plan = AgentPlan(goal="turn off the kitchen lights")
    plan.steps = [_read_step(_ROWS)]
    step = _write_step("light.kitchen_main")
    step.action_detail = _step_action_detail(step.tool, step.parameters)
    _enrich_entity_action_detail(plan, step)
    assert "Kitchen Lights" in step.action_detail
    assert "(Kitchen)" in step.action_detail
    assert "currently on" in step.action_detail


def test_enrichment_does_not_stack_on_a_second_pass():
    plan = AgentPlan(goal="g")
    plan.steps = [_read_step(_ROWS)]
    step = _write_step("light.kitchen_main")
    _enrich_entity_action_detail(plan, step)
    _enrich_entity_action_detail(plan, step)
    assert step.action_detail.count("device: Kitchen Lights") == 1


def test_enrichment_is_a_no_op_for_an_unknown_id():
    plan = AgentPlan(goal="g")
    plan.steps = [_read_step(_ROWS)]
    step = _write_step("light.unknown")
    step.action_detail = "set home device light.unknown → off"
    _enrich_entity_action_detail(plan, step)
    assert step.action_detail == "set home device light.unknown → off"


# ================================================= PENDING entity resolution

def _resolve(template, completed):
    """Drive the REAL public entry point (resolve), not a private helper — a test
    that calls an internal shape the planner never uses measures nothing."""
    plan = AgentPlan(goal="g", steps=list(completed) + [template])
    return placeholder_resolver.resolve(plan, len(completed), max_new=10)


def test_a_named_device_fills_in_code():
    template = _write_step("PENDING: the kitchen lights")
    steps = _resolve(template, [_read_step(_ROWS + [
        {"entity_id": "light.bedroom_lamp", "name": "Bedroom Lamp",
         "area": "Bedroom", "state": "on", "domain": "light"},
    ])])
    assert steps is not None and len(steps) == 1
    assert steps[0].parameters["entity_id"] == "light.kitchen_main"
    assert "Kitchen Lights" in steps[0].description


def test_the_room_alone_is_enough_to_name_a_device():
    """"PENDING: the kitchen lights" names the AREA as often as the device, so
    the match considers both."""
    template = _write_step("PENDING: whatever is in the kitchen")
    steps = _resolve(template, [_read_step(_ROWS)])
    assert steps[0].parameters["entity_id"] == "light.kitchen_main"


def test_several_plausible_devices_are_never_picked_between():
    template = _write_step("PENDING: the lights")
    steps = _resolve(template, [_read_step([
        {"entity_id": "light.kitchen_main", "name": "Kitchen Lights",
         "area": "Kitchen", "state": "on", "domain": "light"},
        {"entity_id": "light.bedroom_lamp", "name": "Bedroom Lights",
         "area": "Bedroom", "state": "on", "domain": "light"},
    ])])
    assert steps is None  # code never picks — it falls to the LLM/question path


def test_the_only_device_found_fills_even_without_a_name_match():
    template = _write_step("PENDING: that thing")
    steps = _resolve(template, [_read_step(_ROWS)])
    assert steps[0].parameters["entity_id"] == "light.kitchen_main"


def test_a_scene_placeholder_never_resolves_to_a_light():
    """⚠️ The single-candidate branch would otherwise hand run_scene a lamp: with
    only one device in the reads, "the only candidate" is a light. The domain
    filter is what stops a resolvable-looking step failing at the tool."""
    template = _write_step("PENDING: the goodnight scene", tool="run_scene")
    steps = _resolve(template, [_read_step(_ROWS)])
    assert steps is None


def test_a_scene_placeholder_resolves_to_the_scene(hub):
    template = _write_step("PENDING: movie night", tool="run_scene")
    steps = _resolve(template, [_read_step([
        {"entity_id": "light.kitchen_main", "name": "Kitchen Lights",
         "area": "Kitchen", "state": "on", "domain": "light"},
        {"entity_id": "scene.movie_night", "name": "Movie Night",
         "area": "", "state": "unknown", "domain": "scene"},
    ])])
    assert steps[0].parameters["entity_id"] == "scene.movie_night"


def test_a_climate_placeholder_only_considers_thermostats():
    template = _write_step("PENDING: the thermostat", tool="set_climate")
    steps = _resolve(template, [_read_step(_ROWS)])
    assert steps is None


def test_the_filled_step_gets_a_fresh_signature():
    """The user approves the REAL device, never the placeholder: a substituted
    step must not inherit an approval granted to the template."""
    template = _write_step("PENDING: the kitchen lights")
    steps = _resolve(template, [_read_step(_ROWS)])
    assert steps[0].signature() != template.signature()


# ==================================================================== render

def test_devices_render_grouped_by_room():
    text = _RESULT_FORMATTERS["list_devices"]({
        "devices": [
            {"entity_id": "light.kitchen_main", "name": "Kitchen Lights",
             "area": "Kitchen", "state": "on"},
            {"entity_id": "lock.front_door", "name": "Front Door",
             "area": "Hallway", "state": "locked"},
        ],
        "count": 2,
    })
    assert "Kitchen:" in text and "Hallway:" in text
    assert "Kitchen Lights — on" in text


def test_a_device_with_no_room_still_renders():
    text = _RESULT_FORMATTERS["list_devices"]({
        "devices": [{"entity_id": "scene.x", "name": "Movie Night",
                     "area": "", "state": "unknown"}],
        "count": 1,
    })
    assert "No room set" in text and "Movie Night" in text


def test_an_empty_result_says_so():
    assert "No matching devices" in _RESULT_FORMATTERS["list_devices"](
        {"devices": [], "count": 0}
    )


def test_a_real_source_truncation_is_reported_separately():
    """"(truncated)" must mean a fact about the world, not that our display
    budget ran out — the 2026-07-29 lesson."""
    text = _RESULT_FORMATTERS["list_devices"]({
        "devices": [{"entity_id": "light.a", "name": "A", "area": "", "state": "on"}],
        "count": 1, "truncated": True,
    })
    assert "more devices than were returned" in text


# ===================================== the lock, driven through the PLANNER
#
# ⚠️ THE GAP THESE CLOSE, found by the falsification harness and not by review.
# Every test above calls _entity_id_violation / _enrich_entity_action_detail
# DIRECTLY. That proves the FUNCTIONS work and says nothing about whether the
# planner ever calls them: reverting the guard's line in the reject chain, and
# reverting the enrichment call in _execute_node, both left those tests GREEN.
# Same shape as the 2026-07-17 fan-out round (a whole feature that had never
# fired under 1,578 green tests) and the 2026-08-03 sweep-wiring case. These
# drive the real graph, so they can see it.

async def test_the_planner_refuses_a_draft_that_invents_an_entity_id(db_session):
    """A first draft naming a concrete entity id must be REJECTED and retried —
    nothing is completed yet, so no read could have produced that id."""
    from tests.test_agent_planner import FakeProvider, plan_json, step

    # ⚠️ THE SCRIPT MATTERS. An earlier version handed the planner a GROUNDED
    # second response, and it passed with the guard reverted — because the
    # REFLECT round consumed that response and replaced the plan either way, so
    # the test could not see the guard at all. Every response here is the SAME
    # ungrounded draft, which makes the guard the only thing that can change the
    # outcome.
    ungrounded = plan_json([step("Turn off the lights", "set_device_state",
                                 entity_id="light.bedroom", state="off")])
    provider = FakeProvider([ungrounded] * 6)
    plan = await AgentPlanner(db_session, provider, session_id="s-home").start(
        "turn off the bedroom lights"
    )
    # The guard fired, so the draft was retried rather than accepted.
    assert provider.calls >= 2
    # The guarantee: an invented device id never survives into an approvable
    # plan. With the guard out of the chain this step is accepted verbatim and
    # the user is shown an approval card for a device nothing ever read — which
    # on a real hub might be a different room's lock.
    assert all(
        s.parameters.get("entity_id") != "light.bedroom" for s in plan.steps
    ), "an ungrounded entity id reached the plan"


async def test_the_approval_card_names_the_device_when_the_plan_pauses(db_session, hub):
    """The enrichment must be wired into the pause, not merely importable: the
    card the user actually sees has to carry the room and the current state."""
    from tests.test_agent_planner import FakeProvider, plan_json, step

    draft = plan_json([
        step("Find the kitchen devices", "list_devices", area="kitchen"),
        step("Turn the kitchen lights on", "set_device_state",
             entity_id="PENDING: the kitchen lights", state="on"),
    ])
    provider = FakeProvider([draft, draft, draft])
    plan = await AgentPlanner(db_session, provider, session_id="s-home2").start(
        "turn on the kitchen lights"
    )
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    paused = next(s for s in plan.steps if s.tool == "set_device_state")
    # The PENDING id was filled in code from the read...
    assert paused.parameters["entity_id"] == "light.kitchen_main"
    # ...and the card names the device, not the slug.
    assert "Kitchen Lights" in (paused.action_detail or "")
    assert "(Kitchen)" in (paused.action_detail or "")
    # Nothing has run: the gate is what the user is looking at.
    assert hub.calls == []


# ================================================================== the agent

def test_the_home_agent_sees_its_tools_and_no_shell():
    from app.agents.agent_registry import agent_for_label

    agent = agent_for_label("HOME")
    assert agent.key == "home"
    assert {"list_devices", "set_device_state", "run_scene"} <= agent.tools
    # A home agent has no business deleting files or running commands.
    assert "run_command" not in agent.tools
    assert "delete_file" not in agent.tools


def test_every_home_agent_tool_is_a_real_registered_tool():
    from app.agents.agent_registry import AGENTS

    for name in AGENTS["home"].tools:
        assert registry.get(name) is not None, f"{name} is not registered"


# ================================================================== the gate

@pytest.mark.parametrize("message", [
    "turn off the kitchen lights",
    "lock the front door",
    "set the thermostat to 21",
    "dim the bedroom lights to 30%",
    "close the blinds",
    "run movie night scene",
    "activate goodnight scene",
    "turn on the smart plug",
    "set the ac to 20 degrees",
])
def test_the_routing_gate_fires_for_home_requests(message):
    """The gate must reach the classifier, or HOME is unreachable from chat —
    the 2026-08-03 finding, where an entire indexed corpus was unroutable
    because the gate had no word for what it held."""
    from app.api.task_router import looks_like_task
    assert looks_like_task(message), f"gate closed for: {message}"


@pytest.mark.parametrize("message", [
    "the lights were beautiful last night",
    "he knocked on the door",
    "turn left at the corner",
    "I set the table for dinner",
    "that scene in the movie was great",
    "my heating bill is insane",
])
def test_ordinary_conversation_about_a_home_stays_closed(message):
    from app.api.task_router import looks_like_task
    assert not looks_like_task(message), f"gate fired for: {message}"
