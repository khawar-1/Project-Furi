"""Spoken approval — the contract, and the binding (2026-08-03, Tier 2 item 8).

⚠️ THIS ROUND REVERSES A DELIBERATE REFUSAL, so most of these tests are about
the conditions under which that reversal is honest.

On 2026-08-03 `task_router._is_typed_approval` was built to REFUSE a typed
"yes" and nudge the user back to the card, because consent to a write is
consent to a SIGNATURE SET and a bare word is bound to nothing. That reasoning
is not weakened here — it is SATISFIED through a different channel: the client
echoes the hash of the contract it was given, and the server re-derives it from
the plan it popped. A client can only hold that hash if it received the
contract; a plan whose steps changed produces a different one.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents.schemas import AgentPlan, PermissionLevel, PlanStep, PlanStatus, StepStatus
from app.agents.spoken import (
    SPOKEN_FALLBACK,
    SPOKEN_STEPS,
    is_spoken_approval,
    plan_needs_screen,
    spoken_plan_text,
)


def _step(tool: str, level: PermissionLevel, **params) -> PlanStep:
    return PlanStep(
        description=f"do {tool}", tool=tool, parameters=params,
        permission_level=level, status=StepStatus.PENDING,
        requires_approval=level != PermissionLevel.READ,
    )


def _plan(*steps: PlanStep, status: PlanStatus = PlanStatus.AWAITING_APPROVAL) -> AgentPlan:
    return AgentPlan(goal="a goal", steps=list(steps), status=status)


# ------------------------------------------------------------- consent wording


@pytest.mark.parametrize("said", [
    "approve", "Approve.", "approved", "yes, approve", "confirm", "confirm it",
    "go ahead", "go ahead please", "do it", "do it jarvis", "send it",
    "permission granted", "yeah go ahead",
])
def test_clear_spoken_consent_is_accepted(said):
    assert is_spoken_approval(said) is True


@pytest.mark.parametrize("said", [
    "", "no", "cancel", "stop", "don't", "wait",
    "approve but not the second one", "go ahead with the first one only",
    "yes but use the D drive", "what does that do", "read it again",
])
def test_anything_that_is_not_clear_consent_is_refused(said):
    """Fails CLOSED. A misread steer replans; a misread consent runs a delete."""
    assert is_spoken_approval(said) is False


@pytest.mark.parametrize("said", ["yes", "yeah", "ok", "okay", "sure", "fine"])
def test_a_bare_yes_does_not_approve_by_voice(said):
    """⚠️ NARROWER THAN THE TYPED SET, ON PURPOSE. `_is_typed_approval` accepts
    a bare "yes" because it was at least typed AT the card. A spoken one may be
    ambient — said to someone in the room, or to the television, while the mic
    is open. Requiring an explicit approval verb makes consent an ACT rather
    than a coincidence, and the spoken contract ends by naming the word."""
    assert is_spoken_approval(said) is False


def test_the_carry_on_word_set_would_have_flipped_a_delete_on():
    """⚠️ THE MEASURED CONTRADICTION THAT KILLED WORD-SET REUSE, frozen.

    `planner._CARRY_ON_RE` is the obvious thing to borrow — it already means "a
    whole-message affirmation with no correction in it". But it accepts "never
    mind" and "nvm", which at a PAUSE mean *forget I interrupted, carry on* and
    at an APPROVAL CARD mean the exact opposite. Reusing it would have read a
    request to DROP a delete as permission to RUN it."""
    from app.agents.planner import _is_bare_continue

    for said in ("never mind", "nvm"):
        assert _is_bare_continue(said) is True, "the contradiction is gone — re-check"
        assert is_spoken_approval(said) is False, (
            f"{said!r} would approve by voice — that is the word-set collision"
        )


# ------------------------------------------------------------ the spoken shape


def test_the_contract_names_the_count_the_change_and_where_it_lands():
    plan = _plan(_step(
        "delete_files", PermissionLevel.DESTRUCTIVE,
        paths=[r"C:\Users\D\Desktop\p3\a.txt", r"C:\Users\D\Desktop\p3\b.txt"],
    ))
    spoken = spoken_plan_text(plan)

    assert "a.txt" in spoken and "b.txt" in spoken
    assert "trash" in spoken
    assert "destructive" in spoken.lower()
    assert "Nothing has changed yet" in spoken
    # No drive-letter path dumps — a person cannot hold those.
    assert "C:\\" not in spoken


def test_a_long_list_becomes_a_count_not_a_recital():
    """Speech is linear and unskimmable. Reading twenty filenames aloud is
    worse than saying "twenty files"."""
    plan = _plan(_step(
        "delete_files", PermissionLevel.DESTRUCTIVE,
        paths=[f"/tmp/file{i}.txt" for i in range(20)],
    ))
    spoken = spoken_plan_text(plan)

    assert "20 files" in spoken
    assert "file7.txt" not in spoken


def test_the_contract_teaches_its_own_phrase():
    """⚠️ Because a bare "yes" is refused, a user who is never told what to say
    is left guessing — the exact "magic word" dead end the 2026-07-17 round
    exists to kill."""
    plan = _plan(_step("create_file", PermissionLevel.WRITE, path="/tmp/x.txt"))
    spoken = spoken_plan_text(plan)

    assert "approve" in spoken.lower()
    assert "cancel" in spoken.lower()
    # And the phrase it teaches must be one the checker actually accepts.
    assert is_spoken_approval("approve") is True


def test_several_steps_are_counted_and_numbered():
    plan = _plan(
        _step("create_folder", PermissionLevel.WRITE, path="/tmp/pdfs"),
        _step("move_files", PermissionLevel.WRITE, sources=["/a.pdf"], destination="/tmp/pdfs"),
    )
    spoken = spoken_plan_text(plan)
    assert "2 things" in spoken
    assert "1." in spoken and "2." in spoken


def test_nothing_is_spoken_for_a_plan_that_is_not_asking_for_consent():
    for status in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.AWAITING_CHOICE):
        plan = _plan(_step("create_file", PermissionLevel.WRITE, path="/x"), status=status)
        assert spoken_plan_text(plan) == ""


def test_the_spoken_form_involves_no_llm():
    """Same guarantee as its visual twin: what the user approves is never
    paraphrased. Proven structurally — the module imports no provider."""
    import inspect

    import app.agents.spoken as spoken

    source = inspect.getsource(spoken)
    assert "provider" not in source.lower()
    assert "LLMProvider" not in source


def test_every_non_read_tool_has_a_spoken_form():
    """⚠️ THE COVERAGE INVARIANT. A new destructive tool nobody taught to speak
    would be approved aloud as "step 1" — the user consenting to a word they
    were never told the meaning of. Same discipline as
    `test_every_path_param_is_covered_or_exempt`, which found a real hole on its
    first run."""
    from app.tools.registry import registry

    missing = [
        name for name in registry.names()
        if registry.get(name).permission_level != PermissionLevel.READ
        and name not in SPOKEN_STEPS
        and name not in SPOKEN_FALLBACK
    ]
    assert not missing, (
        f"non-READ tool(s) with no spoken form: {missing}. Add one in "
        "app/agents/spoken.py, or list it in SPOKEN_FALLBACK and say why."
    )


def test_a_broken_parameter_never_mutes_the_contract():
    """A renderer that throws must degrade to the step's description, not to
    silence — an approval card with nothing spoken is the worst outcome."""
    plan = _plan(_step("move_files", PermissionLevel.WRITE, sources=None, destination=None))
    assert spoken_plan_text(plan) != ""


# ------------------------------------------------------------- the hash binding


def test_the_hash_covers_the_pending_steps_and_changes_with_them():
    plan = _plan(_step("delete_file", PermissionLevel.DESTRUCTIVE, path="/tmp/a.txt"))
    original = plan.contract_hash()

    assert original == plan.contract_hash(), "the hash must be stable"

    plan.steps[0].parameters["path"] = "/tmp/b.txt"
    assert plan.contract_hash() != original, (
        "changing WHICH file is deleted produced the same contract hash"
    )


def test_reordering_the_steps_is_a_different_contract():
    a = _step("create_folder", PermissionLevel.WRITE, path="/tmp/x")
    b = _step("create_file", PermissionLevel.WRITE, path="/tmp/x/y.txt")
    assert _plan(a, b).contract_hash() != _plan(b, a).contract_hash()


def test_completed_steps_do_not_change_the_contract():
    """Approval is about what is STILL TO RUN. A read that has already
    completed is not part of what the user is consenting to."""
    pending = _step("delete_file", PermissionLevel.DESTRUCTIVE, path="/tmp/a.txt")
    done = _step("search_files", PermissionLevel.READ, query="x")
    done.status = StepStatus.COMPLETED
    assert _plan(pending).contract_hash() == _plan(done, pending).contract_hash()


def test_the_contract_and_its_hash_ride_the_shared_serializer():
    """⚠️ ONE serializer, so the inline plan chunk, the background task push and
    the phone surface cannot diverge. `agent._plan_response` used to be a second
    copy of this and would have been the one surface without them."""
    from app.agents.rendering import serialize_plan_for_api
    from app.api.agent import _plan_response

    plan = _plan(_step("delete_file", PermissionLevel.DESTRUCTIVE, path="/tmp/a.txt"))
    for data in (serialize_plan_for_api(plan), _plan_response(plan)):
        assert data["contract_hash"] == plan.contract_hash()
        assert "delete" in data["spoken_contract"].lower()


def test_a_plan_not_awaiting_approval_carries_no_contract():
    from app.agents.rendering import serialize_plan_for_api

    plan = _plan(_step("create_file", PermissionLevel.WRITE, path="/x"),
                 status=PlanStatus.COMPLETED)
    data = serialize_plan_for_api(plan)
    assert "contract_hash" not in data
    assert "spoken_contract" not in data


# ------------------------------------------------------------- the level gate


def test_off_means_never():
    plan = _plan(_step("create_file", PermissionLevel.WRITE, path="/x"))
    assert plan_needs_screen(plan, "off") is True


def test_write_level_lets_a_write_through_but_not_a_delete():
    write_only = _plan(_step("create_file", PermissionLevel.WRITE, path="/x"))
    with_delete = _plan(
        _step("create_file", PermissionLevel.WRITE, path="/x"),
        _step("delete_file", PermissionLevel.DESTRUCTIVE, path="/y"),
    )
    assert plan_needs_screen(write_only, "write") is False
    assert plan_needs_screen(with_delete, "write") is True


def test_all_covers_destructive_too():
    plan = _plan(_step("delete_file", PermissionLevel.DESTRUCTIVE, path="/y"))
    assert plan_needs_screen(plan, "all") is False


def test_an_unknown_level_needs_the_screen():
    """Fails to the SAFE end. A corrupt or hand-edited config must never widen
    who may approve a write."""
    plan = _plan(_step("create_file", PermissionLevel.WRITE, path="/x"))
    for level in ("", "everything", "yes", None):
        assert plan_needs_screen(plan, level) is True


def test_the_config_default_is_off_and_a_bad_value_falls_back_to_off():
    from app.core.app_settings import _coerce_voice, default_voice_config

    assert default_voice_config().spoken_approval == "off"
    assert _coerce_voice({"enabled": True}).spoken_approval == "off"
    assert _coerce_voice({"spoken_approval": "everything"}).spoken_approval == "off"
    assert _coerce_voice({"spoken_approval": "all"}).spoken_approval == "all"


# =========================================================== over real HTTP
# The three guards, driven through the actual endpoint against a REAL planner
# and a REAL file on disk — so "nothing has run" is a claim about the
# filesystem, not about a mock.

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents import plan_store
from app.core.app_settings import VoiceConfig, default_voice_config, set_voice_config
from app.core.dependencies import get_db, get_llm_provider
from app.db.database import Base
from main import app

from tests.test_agent_api import FakeProvider, plan_json, step


@pytest_asyncio.fixture
async def api(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("spoken-api-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    plan_store._PENDING_PLANS.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.factory = factory  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


async def _set_level(api, level: str) -> None:
    async with api.factory() as db:
        base = default_voice_config()
        await set_voice_config(db, VoiceConfig(**{**base.__dict__, "spoken_approval": level}))


async def _park_a_delete(api, tmp_path) -> tuple[dict, "object"]:
    """A REAL plan stopped at a real approval card over a real file."""
    target = tmp_path / "doomed.txt"
    target.write_text("still here", encoding="utf-8")
    drafted = plan_json([step("Delete it", "delete_file", path=str(target))])
    provider = FakeProvider([drafted, drafted])
    app.dependency_overrides[get_llm_provider] = lambda: provider

    plan = (await api.post("/api/agent/execute", json={"goal": f"delete {target}"})).json()
    assert plan["status"] == "awaiting_approval"
    assert target.exists()
    return plan, target


@pytest.mark.asyncio
async def test_the_endpoint_hands_out_a_spoken_contract_and_its_hash(api, tmp_path):
    plan, _ = await _park_a_delete(api, tmp_path)
    assert plan["contract_hash"]
    assert "doomed.txt" in plan["spoken_contract"]
    assert "destructive" in plan["spoken_contract"].lower()


@pytest.mark.asyncio
async def test_spoken_approval_is_refused_while_the_setting_is_off(api, tmp_path):
    """DEFAULT-OFF, enforced SERVER-side so a stale or hostile client cannot
    approve by voice while the user has it turned off."""
    await _set_level(api, "off")
    plan, target = await _park_a_delete(api, tmp_path)

    r = await api.post("/api/agent/approve", json={
        "plan_id": plan["id"], "approved": True,
        "spoken": {"contract_hash": plan["contract_hash"], "utterance": "approve"},
    })

    assert r.status_code == 403
    assert target.exists(), "the file was deleted while spoken approval was OFF"
    # And the card is still answerable — a refusal must not consume the plan.
    assert plan_store.get_plan(plan["id"]) is not None


@pytest.mark.asyncio
async def test_write_level_still_sends_a_delete_to_the_card(api, tmp_path):
    await _set_level(api, "write")
    plan, target = await _park_a_delete(api, tmp_path)

    r = await api.post("/api/agent/approve", json={
        "plan_id": plan["id"], "approved": True,
        "spoken": {"contract_hash": plan["contract_hash"], "utterance": "approve"},
    })

    assert r.status_code == 403
    assert target.exists()


@pytest.mark.asyncio
async def test_a_stale_hash_is_refused_and_the_plan_is_untouched(api, tmp_path):
    """⚠️ THE BINDING. A hash from a DIFFERENT contract must not approve this
    one — that is the whole reason spoken consent is allowed at all."""
    await _set_level(api, "all")
    plan, target = await _park_a_delete(api, tmp_path)

    r = await api.post("/api/agent/approve", json={
        "plan_id": plan["id"], "approved": True,
        "spoken": {"contract_hash": "0" * 64, "utterance": "approve"},
    })

    assert r.status_code == 409
    assert target.exists(), "a stale hash deleted the file"
    assert plan_store.get_plan(plan["id"]) is not None, "the card was consumed"


@pytest.mark.asyncio
async def test_an_utterance_that_is_not_consent_is_refused(api, tmp_path):
    await _set_level(api, "all")
    plan, target = await _park_a_delete(api, tmp_path)

    for said in ("yes", "what does that do", "approve the first one only"):
        r = await api.post("/api/agent/approve", json={
            "plan_id": plan["id"], "approved": True,
            "spoken": {"contract_hash": plan["contract_hash"], "utterance": said},
        })
        assert r.status_code == 422, f"{said!r} was accepted as consent"
        assert target.exists()
        assert plan_store.get_plan(plan["id"]) is not None


@pytest.mark.asyncio
async def test_a_matching_contract_at_the_all_level_runs_it(api, tmp_path):
    """The positive case — everything above is worthless if this never works."""
    await _set_level(api, "all")
    plan, target = await _park_a_delete(api, tmp_path)

    r = await api.post("/api/agent/approve", json={
        "plan_id": plan["id"], "approved": True,
        "spoken": {"contract_hash": plan["contract_hash"], "utterance": "approve"},
    })

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert not target.exists(), "the approved delete did not run"


@pytest.mark.asyncio
async def test_the_card_path_still_needs_no_echo(api, tmp_path):
    """Approving ON the card sends no `spoken` block at all — the card IS the
    contract. This must keep working untouched."""
    await _set_level(api, "off")
    plan, target = await _park_a_delete(api, tmp_path)

    r = await api.post("/api/agent/approve", json={"plan_id": plan["id"], "approved": True})

    assert r.status_code == 200
    assert not target.exists()


@pytest.mark.asyncio
async def test_every_voice_config_field_survives_a_round_trip(api):
    """⚠️ THE BUG THIS ROUND SHIPPED AND CAUGHT, frozen.

    `set_voice_config` used to name all fourteen fields by hand, making it the
    FOURTH place the field list lived (dataclass, default, coercer, writer).
    Adding `spoken_approval` updated three of them and the WRITE silently
    dropped it: a level the user had set read back as its default, with nothing
    anywhere saying so — a consent setting that quietly reverts is exactly the
    failure this feature must not have.

    Asserting the WHOLE dataclass round-trips, rather than one field, is what
    makes this catch the next field too."""
    import dataclasses

    from app.core.app_settings import get_voice_config

    original = VoiceConfig(**{
        **default_voice_config().__dict__,
        "spoken_approval": "all",
        "enabled": True,
        "speak_all_responses": True,
        "tts_speed": 1.25,
    })
    async with api.factory() as db:
        await set_voice_config(db, original)
    async with api.factory() as db:
        assert await get_voice_config(db) == original

    # And the writer must not be a hand-kept list again.
    import inspect

    from app.core import app_settings

    source = inspect.getsource(app_settings.set_voice_config)
    assert "asdict(" in source, (
        "set_voice_config is naming fields by hand again — the next field added "
        "will be silently dropped on write"
    )
    assert len(dataclasses.fields(VoiceConfig)) >= 15
