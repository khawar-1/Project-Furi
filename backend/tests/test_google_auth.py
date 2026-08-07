"""
Phase 5 Part 1 — GoogleAuthManager + service factories.

Everything is hermetic: token files live in tmp paths (the conftest autouse
fixture guarantees the default manager does too), refresh/revoke/flow network
calls are monkeypatched. No test touches Google.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.integrations import google_auth, google_services
from app.integrations.google_auth import (
    SCOPES,
    GoogleAuthManager,
    GoogleNotConnectedError,
)


def _token_data(**overrides) -> dict:
    """A structurally valid stored token (google Credentials JSON shape)."""
    data = {
        "token": "access-token-abc",
        "refresh_token": "refresh-token-xyz",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": list(SCOPES),
        "universe_domain": "googleapis.com",
        "account": "",
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "_account_email": "user@example.com",
    }
    data.update(overrides)
    return data


def _write_token(manager: GoogleAuthManager, **overrides) -> None:
    manager.token_path.parent.mkdir(parents=True, exist_ok=True)
    manager.token_path.write_text(json.dumps(_token_data(**overrides)), encoding="utf-8")


@pytest.fixture
def manager(tmp_path) -> GoogleAuthManager:
    return GoogleAuthManager(token_path=tmp_path / "google_token.json")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "")


# ================================================================== status

def test_status_not_configured(manager, unconfigured):
    status = manager.status()
    assert status["configured"] is False
    assert status["connected"] is False
    assert "GOOGLE_CLIENT_ID" in status["detail"]


def test_status_no_token_file(manager, configured):
    status = manager.status()
    assert status["configured"] is True
    assert status["connected"] is False
    assert status["account_email"] is None


def test_status_connected(manager, configured):
    _write_token(manager)
    status = manager.status()
    assert status["connected"] is True
    assert status["account_email"] == "user@example.com"
    assert status["detail"] is None
    assert status["scopes"] == list(SCOPES)


def test_status_missing_scope_means_not_connected(manager, configured):
    # A token granted before a scope was added must force re-consent,
    # never silent partial capability.
    _write_token(manager, scopes=list(SCOPES)[:-1])
    status = manager.status()
    assert status["connected"] is False
    assert "permissions" in status["detail"]


def test_status_missing_refresh_token_means_not_connected(manager, configured):
    _write_token(manager, refresh_token=None)
    assert manager.status()["connected"] is False


def test_status_corrupt_token_file_never_crashes(manager, configured):
    manager.token_path.parent.mkdir(parents=True, exist_ok=True)
    manager.token_path.write_text("{not json", encoding="utf-8")
    assert manager.status()["connected"] is False


def test_status_never_leaks_token_material(manager, configured):
    _write_token(manager)
    dumped = json.dumps(manager.status())
    assert "access-token-abc" not in dumped
    assert "refresh-token-xyz" not in dumped


# ============================================================ credentials

async def test_get_credentials_raises_when_no_token(manager):
    with pytest.raises(GoogleNotConnectedError):
        await manager.get_credentials()


async def test_get_credentials_returns_valid_creds_without_refresh(manager, monkeypatch):
    _write_token(manager)

    def _no_refresh(self, request):  # a valid token must not hit the network
        raise AssertionError("refresh must not be called for a valid token")

    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(Credentials, "refresh", _no_refresh)
    creds = await manager.get_credentials()
    assert creds.token == "access-token-abc"


async def test_get_credentials_refreshes_expired_and_saves(manager, monkeypatch):
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_token(manager, expiry=expired)

    from google.oauth2.credentials import Credentials

    def _fake_refresh(self, request):
        self.token = "refreshed-token"

    monkeypatch.setattr(Credentials, "refresh", _fake_refresh)
    creds = await manager.get_credentials()
    assert creds.token == "refreshed-token"
    # The refreshed token was persisted, and the account email survived.
    on_disk = json.loads(manager.token_path.read_text(encoding="utf-8"))
    assert on_disk["token"] == "refreshed-token"
    assert on_disk["_account_email"] == "user@example.com"


async def test_refresh_failure_degrades_not_crashes(manager, monkeypatch):
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_token(manager, expiry=expired)

    from google.oauth2.credentials import Credentials

    def _failing_refresh(self, request):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", _failing_refresh)
    with pytest.raises(GoogleNotConnectedError):
        await manager.get_credentials()
    # Never destructive on ambiguity: the token file stays for diagnosis /
    # a transient-network retry.
    assert manager.token_path.exists()


# ------------------------------------------------- refresh circuit breaker
#
# A dead Google token used to cost a real network round trip on EVERY call.
# MEASURED 2026-08-06: 53 consecutive failures in one log, two of them inside
# the slowest chat turn on record (20.8s to first token). The breaker skips
# only work we have just watched fail; the caller's outcome is unchanged.

async def _fail_n_times(manager, monkeypatch) -> dict:
    """Drive refreshes until the breaker opens. Returns a call counter."""
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_token(manager, expiry=expired)
    from google.oauth2.credentials import Credentials

    calls = {"n": 0}

    def _failing_refresh(self, request):
        calls["n"] += 1
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", _failing_refresh)
    return calls


async def test_repeated_refresh_failures_stop_hitting_the_network(manager, monkeypatch):
    """The incident: every call re-attempted a refresh that had just failed.

    After the threshold the network is not touched again during the cooldown —
    and the caller still gets the SAME exception, so nothing downstream can
    tell the difference except in latency.
    """
    calls = await _fail_n_times(manager, monkeypatch)

    for _ in range(10):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()

    assert calls["n"] == 2, (
        f"expected the breaker to stop after 2 real attempts, got {calls['n']} "
        "network round trips - a dead token is taxing every call again"
    )


async def test_an_open_breaker_never_deletes_the_token(manager, monkeypatch):
    """Failing fast must stay as non-destructive as failing slowly was."""
    await _fail_n_times(manager, monkeypatch)
    for _ in range(5):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()
    assert manager.token_path.exists()


async def test_a_successful_refresh_resets_the_breaker(manager, monkeypatch):
    """After a recovery, the failure COUNT must start again from zero.

    ⚠️ An earlier version of this test failed only once before succeeding — one
    failure is below the threshold, so the breaker never opened and the reset
    was never load-bearing. It passed against code with the reset deleted,
    which is no test at all. The sequence below drives the count to the
    threshold first, so a missing reset is observable: a single later blip
    would re-open the breaker on a connection that has since proven healthy.
    """
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_token(manager, expiry=expired)
    from google.oauth2.credentials import Credentials

    state = {"fail": True, "n": 0}

    def _refresh(self, request):
        state["n"] += 1
        if state["fail"]:
            raise RuntimeError("temporary network glitch")
        self.token = "refreshed-token"

    monkeypatch.setattr(Credentials, "refresh", _refresh)

    # 1. Outage: fail to the threshold, so the breaker opens.
    for _ in range(2):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()
    assert state["n"] == 2

    # 2. Cooldown elapses; the half-open probe succeeds -> recovered.
    manager._refresh_blocked_until = 0.0
    state["fail"] = False
    assert (await manager.get_credentials()).token == "refreshed-token"
    assert state["n"] == 3

    # 3. A single later blip on a healthy connection must NOT re-open the
    #    breaker — which is only true if the count went back to zero.
    state["fail"] = True
    _write_token(manager, expiry=expired)
    with pytest.raises(GoogleNotConnectedError):
        await manager.get_credentials()
    assert state["n"] == 4

    state["fail"] = False
    _write_token(manager, expiry=expired)
    assert (await manager.get_credentials()).token == "refreshed-token"
    assert state["n"] == 5, (
        "the call after a single blip was refused, so the failure count "
        "survived a successful refresh - the breaker was not reset"
    )


async def test_the_cooldown_lets_exactly_one_probe_through(manager, monkeypatch):
    """Half-open: when the cooldown expires, ONE attempt is allowed. If it
    fails the breaker re-opens immediately rather than allowing another full
    run of N failures."""
    calls = await _fail_n_times(manager, monkeypatch)
    for _ in range(5):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()
    assert calls["n"] == 2

    manager._refresh_blocked_until = 0.0             # simulate cooldown elapsed
    for _ in range(5):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()
    assert calls["n"] == 3, "the probe must be a single attempt, not a new run"


async def test_disconnect_clears_the_breaker(manager, monkeypatch):
    """The user's own action must never be second-guessed by state from before
    it — a reconnect after a disconnect starts clean."""
    await _fail_n_times(manager, monkeypatch)
    for _ in range(3):
        with pytest.raises(GoogleNotConnectedError):
            await manager.get_credentials()
    assert manager._refresh_failures >= 2

    monkeypatch.setattr(google_auth, "_revoke_token", _async_false)
    await manager.disconnect()
    assert manager._refresh_failures == 0


async def _async_false(_token: str) -> bool:
    return False


async def test_get_credentials_missing_scope_raises(manager):
    _write_token(manager, scopes=list(SCOPES)[:-1])
    with pytest.raises(GoogleNotConnectedError):
        await manager.get_credentials()


# ================================================================ connect

async def test_start_connect_not_configured(manager, unconfigured):
    assert await manager.start_connect() == "not_configured"


async def test_start_connect_already_connected(manager, configured):
    _write_token(manager)
    assert await manager.start_connect() == "already_connected"


async def test_start_connect_dedupes_concurrent_flows(manager, configured, monkeypatch):
    import threading

    thread_release = threading.Event()

    def _blocking_flow():
        thread_release.wait(timeout=5)

    monkeypatch.setattr(manager, "_run_flow_sync", _blocking_flow)
    assert await manager.start_connect() == "pending"
    assert await manager.start_connect() == "in_progress"
    thread_release.set()
    await manager._flow_task
    # NOTE: the real _run_flow_sync clears _connecting in its finally block;
    # the stub doesn't, so reset for hygiene.
    with manager._state_lock:
        manager._connecting = False


async def test_start_connect_pending_then_connected(manager, configured, monkeypatch):
    def _fake_flow():
        try:
            _write_token(manager)
        finally:
            with manager._state_lock:
                manager._connecting = False

    monkeypatch.setattr(manager, "_run_flow_sync", _fake_flow)
    assert await manager.start_connect() == "pending"
    await manager._flow_task
    assert manager.status()["connected"] is True


# ============================================================= disconnect

async def test_disconnect_deletes_token_and_revokes(manager, configured, monkeypatch):
    _write_token(manager)
    revoked_tokens = []

    async def _fake_revoke(token: str) -> bool:
        revoked_tokens.append(token)
        return True

    monkeypatch.setattr(google_auth, "_revoke_token", _fake_revoke)
    result = await manager.disconnect()
    assert result == {"disconnected": True, "revoked": True}
    assert not manager.token_path.exists()
    assert revoked_tokens == ["refresh-token-xyz"]


async def test_disconnect_deletes_locally_even_if_revoke_fails(manager, configured, monkeypatch):
    _write_token(manager)

    async def _failing_revoke(token: str) -> bool:
        return False

    monkeypatch.setattr(google_auth, "_revoke_token", _failing_revoke)
    result = await manager.disconnect()
    assert result["disconnected"] is True
    assert result["revoked"] is False
    assert not manager.token_path.exists()


async def test_disconnect_without_token_file(manager):
    result = await manager.disconnect()
    assert result == {"disconnected": True, "revoked": False}


# ======================================================== service factories

async def test_gmail_factory_override(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(google_services, "GMAIL_SERVICE_FACTORY", lambda: sentinel)
    assert await google_services.get_gmail_service() is sentinel


async def test_calendar_factory_supports_async(monkeypatch):
    sentinel = object()

    async def _factory():
        return sentinel

    monkeypatch.setattr(google_services, "CALENDAR_SERVICE_FACTORY", _factory)
    assert await google_services.get_calendar_service() is sentinel


async def test_real_factory_raises_when_not_connected():
    # The conftest fixture points the default manager at an empty tmp token
    # path, so the real path degrades to the clean not-connected error.
    with pytest.raises(GoogleNotConnectedError):
        await google_services.get_gmail_service()
    with pytest.raises(GoogleNotConnectedError):
        await google_services.get_calendar_service()
