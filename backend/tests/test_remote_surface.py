"""The remote surface (2026-08-03, Tier 2 item 5 — "one machine, one room").

⚠️ MOST OF THIS FILE ASSERTS ABSENCE, not refusal, and that distinction IS the
feature. The remote listener is safe because the dangerous routes are NOT
MOUNTED on it — so the tests check the mounted route table, not response codes
from a gate. A 403 would mean a check ran and worked; a 404 means there was
never anything there to check.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.core.remote_app import create_remote_app, remote_routes
from app.core.remote_manifest import DENIED, REMOTE_ROUTES, is_remote_route
from app.core.remote_tokens import (
    MAX_DEVICES,
    hash_token,
    list_devices,
    pair_device,
    revoke_device,
    verify_token,
)
from main import app


def _live_routes() -> list[tuple[list[str], str]]:
    """Every route the REAL app serves, methods normalised."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = {
            m for m in (getattr(route, "methods", None) or {"WEBSOCKET"})
            if m not in ("HEAD", "OPTIONS")
        }
        out.append((sorted(methods), path))
    return out


# ------------------------------------------------------- the coverage invariant


def test_every_route_is_allowed_or_denied():
    """⚠️ THE GUARD THAT KEEPS THIS SHUT — and it earned its keep twice on its
    first run: it caught that `POST /api/threads` needed denying while
    `GET /api/threads` was allowed (a path can differ by method), and it caught
    the pairing routes the moment they were registered.

    A router added later fails here until someone DECIDES about it. Without
    this, "default deny" quietly degrades into "denied whatever we happened to
    think of that day"."""
    uncovered = [
        (methods, path) for methods, path in _live_routes()
        if not is_remote_route(methods, path) and path not in DENIED
    ]
    assert not uncovered, (
        f"route(s) in neither map: {uncovered}. Add them to REMOTE_ROUTES, or "
        "to DENIED with a reason — silence is not a decision."
    )


def test_every_denial_carries_a_reason():
    blank = [path for path, reason in DENIED.items() if not reason.strip()]
    assert not blank, f"denied with no reason: {blank}"


def test_every_allowed_route_actually_exists():
    """A manifest entry for a route that no longer exists is dead weight that
    reads as coverage."""
    live = {(m, path) for methods, path in _live_routes() for m in methods}
    missing = [entry for entry in REMOTE_ROUTES if entry not in live]
    assert not missing, f"manifest names route(s) the app does not serve: {missing}"


# ---------------------------------------------------------- what is NOT there


@pytest.mark.parametrize("method,path", [
    ("POST", "/chat/stream"),
    ("POST", "/chat"),
    ("POST", "/api/agent/execute"),
    ("POST", "/api/routines/{routine_id}/run"),
    ("POST", "/api/initiative/run-now"),
    ("POST", "/api/initiative/suggestions/{suggestion_id}/accept"),
    ("POST", "/api/index/rebuild"),
    ("POST", "/api/browser/login"),
    ("POST", "/api/settings/briefing/run-now"),
    ("POST", "/api/integrations/google/connect"),
])
def test_nothing_that_can_start_work_is_mounted(method, path):
    """⚠️ "NEVER A SHELL", asserted as ABSENCE. Every one of these can begin
    something — a plan, a task, an index pass, a browser window, an OAuth flow.
    None is reachable, because none is there."""
    assert (method, path) not in remote_routes(create_remote_app(app))


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/context/world"),      # OCR'd screen text
    ("GET", "/api/agent/tools"),        # a map of everything Jarvis can do
    ("GET", "/api/browser/media"),      # open tab URLs and titles
    ("GET", "/memory/search"),          # everything Jarvis knows about the user
    ("GET", "/api/activity/routing"),   # a copy of what was typed
    ("GET", "/api/autofill"),           # form-fill data, some of it secret
])
def test_the_graded_reads_are_not_mounted_either(method, path):
    """These are READS, and denying them is a judgement rather than a rule: the
    remote surface carries what is needed to answer a pending question and no
    more."""
    assert (method, path) not in remote_routes(create_remote_app(app))


def test_pairing_itself_is_not_reachable_remotely():
    """A paired device that can pair another, or revoke its own revocation, is
    a credential that cannot be taken away."""
    mounted = remote_routes(create_remote_app(app))
    assert ("POST", "/api/remote/pair") not in mounted
    assert ("DELETE", "/api/remote/{device_id}") not in mounted
    assert ("GET", "/api/remote") not in mounted


def test_the_push_socket_is_not_mounted():
    """/ws is server→client and would hand a remote client every event on the
    machine. It needs its own session story first."""
    remote = create_remote_app(app)
    assert not any(getattr(r, "path", None) == "/ws" for r in remote.routes)


def test_no_api_explorer_on_a_lan_port():
    remote = create_remote_app(app)
    assert remote.docs_url is None
    assert remote.openapi_url is None


# -------------------------------------------------------------- what IS there


def test_the_four_answers_are_mounted():
    """The positive case — everything above is worthless if the surface cannot
    do its one job."""
    mounted = remote_routes(create_remote_app(app))
    for entry in [
        ("POST", "/api/agent/approve"),
        ("POST", "/api/agent/choose"),
        ("POST", "/api/tasks/{task_id}/pause"),
        ("POST", "/api/tasks/{task_id}/cancel"),
    ]:
        assert entry in mounted, f"{entry} is missing — the surface cannot answer"


def test_the_reads_a_phone_needs_are_mounted():
    mounted = remote_routes(create_remote_app(app))
    for entry in [
        ("GET", "/health"),
        ("GET", "/api/tasks"),
        ("GET", "/api/activity"),
        ("GET", "/chat/sessions/{session_id}/messages"),
    ]:
        assert entry in mounted


def test_the_remote_app_is_much_smaller_than_the_real_one():
    """A sanity bound: if this ever approaches the full app, the filter has
    stopped filtering."""
    mounted = remote_routes(create_remote_app(app))
    assert len(mounted) < 20
    assert len(mounted) < len(_live_routes()) / 3


def test_the_handlers_are_the_SAME_objects_not_a_second_implementation():
    """⚠️ Copying route objects rather than re-declaring them is what stops the
    remote surface drifting into a second implementation of approve — which
    would be the worst of all worlds: two approval paths, one of them less
    tested."""
    from fastapi.routing import APIRoute

    remote = create_remote_app(app)
    source = {
        (tuple(sorted(r.methods)), r.path): r.endpoint
        for r in app.routes if isinstance(r, APIRoute)
    }
    for r in remote.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.path == "/":
            continue  # the phone page — deliberately remote-only, see below
        assert source[(tuple(sorted(r.methods)), r.path)] is r.endpoint


def test_the_phone_page_exists_only_on_the_remote_listener():
    """The page is DECLARED on the remote app rather than copied, because it
    exists only there — the desktop has its own UI and must never serve this
    one. It is also the single route that loads without a token, since it is
    where the token gets installed; it is inert without one."""
    remote_paths = {getattr(r, "path", None) for r in create_remote_app(app).routes}
    main_paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in remote_paths
    assert "/" not in main_paths


def test_the_page_carries_no_way_to_start_work():
    """A compose box on the phone would be a request to the one route that is
    not there. The page must not imply a capability the surface lacks."""
    from app.core.remote_page import REMOTE_PAGE

    assert "/chat/stream" not in REMOTE_PAGE
    assert "/api/agent/execute" not in REMOTE_PAGE
    # Every endpoint it does call must be on the manifest.
    import re

    for path in set(re.findall(r"'(/api/[a-z/]+)", REMOTE_PAGE)):
        assert any(path.startswith(p.rstrip("{").rstrip("/")) or p.startswith(path)
                   for _, p in REMOTE_ROUTES), f"the page calls {path}, which is not mounted"


# -------------------------------------------------------------- device tokens


@pytest.mark.asyncio
async def test_a_paired_device_verifies_and_the_token_is_stored_hashed(db_session):
    device, token = await pair_device(db_session, "khawar's phone")

    assert await verify_token(db_session, token) is not None
    # ⚠️ THE STORED FORM IS A HASH. A leaked jarvis.db, backup or sync copy is
    # useless for getting in — we only ever need to COMPARE, never to read.
    assert device.token_hash == hash_token(token)
    assert token not in device.token_hash
    stored = await list_devices(db_session)
    assert all(token not in str(d.__dict__) for d in stored)


@pytest.mark.asyncio
async def test_a_wrong_token_verifies_as_nothing(db_session):
    await pair_device(db_session, "phone")
    for bad in ("", "not-a-token", "x" * 43):
        assert await verify_token(db_session, bad) is None


@pytest.mark.asyncio
async def test_revoking_takes_effect_immediately(db_session):
    device, token = await pair_device(db_session, "lost phone")
    assert await verify_token(db_session, token) is not None

    assert await revoke_device(db_session, device.id) is True

    assert await verify_token(db_session, token) is None
    assert await revoke_device(db_session, "no-such-id") is False


@pytest.mark.asyncio
async def test_an_expired_pairing_stops_working_on_its_own(db_session):
    """An abandoned device must not stay a key forever."""
    device, token = await pair_device(db_session, "old phone", ttl_days=1)
    assert await verify_token(db_session, token) is not None

    from datetime import timedelta

    from app.core.remote_tokens import _load, _save
    from app.db.models import utc_iso, utc_now

    devices = await _load(db_session)
    for d in devices:
        d.expires_at = utc_iso(utc_now() - timedelta(days=1))
    await _save(db_session, devices)

    assert await verify_token(db_session, token) is None


@pytest.mark.asyncio
async def test_an_unparseable_expiry_is_not_live(db_session):
    """Fails CLOSED. A hand-edited or corrupt row must never be a valid
    credential."""
    from app.core.remote_tokens import RemoteDevice

    broken = RemoteDevice(
        id="x", name="n", token_hash="h", created_at="", expires_at="not-a-date"
    )
    assert broken.is_live() is False


@pytest.mark.asyncio
async def test_pairing_is_capped(db_session):
    for i in range(MAX_DEVICES):
        await pair_device(db_session, f"device {i}")
    with pytest.raises(ValueError):
        await pair_device(db_session, "one too many")


@pytest.mark.asyncio
async def test_two_pairings_never_collide(db_session):
    _, a = await pair_device(db_session, "a")
    _, b = await pair_device(db_session, "b")
    assert a != b
    da = await verify_token(db_session, a)
    dbv = await verify_token(db_session, b)
    assert da is not None and dbv is not None and da.id != dbv.id


# ------------------------------------------------------------------ the config


def test_backend_host_is_actually_passed_at_every_launch_site():
    """⚠️ IT WAS DECORATIVE UNTIL THIS ROUND. `BACKEND_HOST` appeared in exactly
    one place — the startup log — and nothing bound it: loopback held only
    because uvicorn DEFAULTS to it. Setting it in .env changed nothing in either
    direction, which is the worst kind of config: one that reads as a guarantee
    and is not."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    package_json = (root / "package.json").read_text(encoding="utf-8")
    assert "--host" in package_json, "the dev script does not pin a host"

    electron = (root / "electron" / "main.ts").read_text(encoding="utf-8")
    assert "'--host'" in electron, "the production spawn does not pin a host"
    assert "BACKEND_HOST" in electron


def test_the_remote_listener_is_off_by_default():
    from app.core.config import Settings

    assert Settings().REMOTE_ENABLED is False


# ------------------------------------------ the pairing link (2026-08-04)
# The listener, the manifest and the tokens all shipped correct on 2026-08-03.
# What did not was the walk from "the feature exists" to "a person can reach
# it": pairing returned the literal string "http://<this-machine>:8765", and
# the phone page told an unpaired user to scan a QR code that did not exist.


def test_lan_address_returns_a_usable_v4_address():
    """It is called on the pairing path, so it must always answer with
    something typeable — never raise, never a wildcard."""
    from app.core.remote_link import _WILDCARDS, lan_address

    address = lan_address()
    parts = address.split(".")
    assert len(parts) == 4 and all(p.isdigit() for p in parts), address
    assert address not in _WILDCARDS


def test_lan_address_falls_back_to_loopback_when_the_network_is_gone(monkeypatch):
    """An offline machine still has to be able to pair — over loopback, if that
    is all there is."""
    import socket as socket_module

    from app.core import remote_link

    def explode(*args, **kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(socket_module, "socket", explode)
    assert remote_link.lan_address() == "127.0.0.1"


def test_a_wildcard_bind_is_never_put_in_a_link():
    """`REMOTE_HOST` defaults to 0.0.0.0 — right for binding, useless in a URL.
    A pinned host is echoed back; a wildcard sends us looking for the real one."""
    from app.core.remote_link import listen_address

    assert listen_address("192.168.1.50") == "192.168.1.50"
    for wildcard in ("0.0.0.0", "::", "", "   "):
        assert listen_address(wildcard) not in ("0.0.0.0", "::", "")


def test_the_token_rides_in_the_fragment():
    """⚠️ NOT THE QUERY STRING. A fragment never leaves the browser — it reaches
    no server, no log and no proxy. `remote_page.py` reads it from
    `location.hash`, and a query string would write a live credential into
    every access log between here and the phone."""
    from app.core.remote_link import pairing_link

    url = pairing_link("s3cret", host="10.0.0.4", port=8765)
    assert url == "http://10.0.0.4:8765/#t=s3cret"
    assert "?" not in url
    assert url.split("#", 1)[0].count("s3cret") == 0


def test_the_qr_is_a_data_uri_so_the_card_never_needs_inner_html():
    from app.core.remote_link import qr_data_uri

    uri = qr_data_uri("http://10.0.0.4:8765/#t=abc")
    assert uri is not None and uri.startswith("data:image/svg+xml")


def test_a_missing_segno_costs_the_qr_and_nothing_else(monkeypatch):
    """The dependency is optional (the rapidocr convention): without it the
    pairing card still shows a working, copyable link."""
    import builtins

    from app.core.remote_link import qr_data_uri

    real_import = builtins.__import__

    def no_segno(name, *args, **kwargs):
        if name == "segno":
            raise ImportError("no segno here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_segno)
    assert qr_data_uri("http://10.0.0.4:8765/#t=abc") is None


def test_qr_and_link_never_raise_on_junk():
    from app.core.remote_link import pairing_link, qr_data_uri

    assert qr_data_uri("") is None
    assert pairing_link("", host="", port=0).startswith("http://")


@pytest.mark.asyncio
async def test_pairing_hands_back_a_real_address_not_a_placeholder(db_session):
    """The incident frozen: the response used to carry
    `http://<this-machine>:8765`, so nothing about it could be scanned, typed
    or clicked."""
    from app.api.remote import PairRequest, pair

    body = await pair(PairRequest(name="phone"), db_session)

    assert "<this-machine>" not in body["url"]
    assert body["url"].startswith("http://")
    assert body["url"].endswith(f"#t={body['token']}")
    assert "qr" in body  # nullable, but the key is part of the contract


@pytest.mark.asyncio
async def test_the_status_route_reports_where_a_phone_would_reach_it(db_session):
    from app.api.remote import remote_status

    body = await remote_status(db_session)
    assert body["address"] not in ("0.0.0.0", "::", "")


def test_the_pairing_link_added_no_route_to_the_remote_surface():
    """The QR rides the EXISTING pair response on purpose. A new route would
    have had to be denied in the manifest, and pairing must stay local-only —
    a paired device that can pair another is a key that cannot be taken back."""
    assert not any(path.startswith("/api/remote") for _, path in REMOTE_ROUTES)


# --------------------------------------------- the phone's plan id (2026-08-04)


def test_the_phone_reads_the_tasks_own_plan_id():
    """⚠️ `tasks.py` serializes `plan_id` explicitly AND sets `plan` to null
    when `plan_payload` is missing or unparseable — so reading the id off the
    parsed payload posted an EMPTY plan_id and the approve 404'd, on exactly
    the tasks whose snapshot failed to round-trip."""
    from app.core.remote_page import REMOTE_PAGE

    assert "t.plan_id" in REMOTE_PAGE
    # The old form read the payload alone. A fallback to it is fine; reading
    # ONLY it is the defect.
    assert 'data-plan="${esc(p.id || \'\')}"' not in REMOTE_PAGE


# ------------------------------------- the phone echoes its contract (2026-08-04)


def test_the_phone_sends_the_hash_of_the_contract_it_drew():
    """⚠️ THREE PLACES PROMISED THIS AND THE PHONE DID NOT DO IT. The manifest's
    own docstring, `rendering.serialize_plan_for_api` and CLAUDE.md all said an
    off-card approval echoes the contract hash; `remote_page.py` posted only
    `{plan_id, approved}` — the "BACKEND_HOST was decorative" shape, a
    documented claim the code did not make.

    It matters here specifically because the phone renders `Task.plan_payload`,
    which is a POLLED SNAPSHOT: it can lag behind the parked plan when a steer
    or a replan re-parks it under the same id."""
    from app.core.remote_page import REMOTE_PAGE

    assert "p.contract_hash" in REMOTE_PAGE, "the hash is never read off the plan"
    assert "body.contract_hash = contractHash" in REMOTE_PAGE, "it is never sent"


def test_the_phone_does_not_gate_a_cancel_on_the_hash():
    """Cancelling is safe whatever the steps are now, and refusing one over a
    stale hash would strand the card with no way to answer it."""
    from app.core.remote_page import REMOTE_PAGE

    assert "act === 'approve' && contractHash" in REMOTE_PAGE


def test_an_echoed_hash_is_read_from_either_channel():
    """One accessor, so the pre-pop check and the post-pop re-derivation cannot
    drift into disagreeing about what the client echoed."""
    from app.api.agent import ApproveRequest

    spoken = ApproveRequest(
        plan_id="p", approved=True,
        spoken={"contract_hash": "a" * 64, "utterance": "approve"},
    )
    bare = ApproveRequest(plan_id="p", approved=True, contract_hash="b" * 64)
    card = ApproveRequest(plan_id="p", approved=True)

    assert spoken.echoed_contract_hash() == "a" * 64
    assert bare.echoed_contract_hash() == "b" * 64
    # The DESKTOP card echoes nothing and is deliberately not checked: it is
    # push-updated, so it IS the contract.
    assert card.echoed_contract_hash() is None
