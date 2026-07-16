"""
Phase 8 Part 1 — the world model / context store: presence derivation,
staleness gating, master-switch gating, best-effort Google sections (fake
service factories — no network), recent-file focus, and the cheap status.
"""
import pytest

from app.core import context_store
from app.core.app_settings import ContextConfig, set_context_config
from app.core.context_store import (
    context_status,
    get_world_model,
    high_load,
    record_affective_signal,
    record_device_signal,
    record_ocr_summary,
)
from app.db.models import FileIndex


# ------------------------------------------------------------- fake Google

class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        return _Req({"items": self._items})


class FakeCalendar:
    def __init__(self, items):
        self._items = items

    def events(self):
        return _FakeEvents(self._items)


class _FakeMessages:
    def list(self, userId, q, maxResults):  # noqa: N803 (Google's param name)
        if "is:important" in q:
            return _Req({"messages": [{"id": "urgent"}]})
        return _Req({"messages": [{"id": "1"}, {"id": "2"}, {"id": "3"}]})


class _FakeGmailUsers:
    def messages(self):
        return _FakeMessages()


class FakeGmail:
    def users(self):
        return _FakeGmailUsers()


async def _enable(db, **overrides):
    cfg = ContextConfig(
        enabled=overrides.get("enabled", True),
        device_sensing=overrides.get("device_sensing", True),
        screen_ocr=overrides.get("screen_ocr", True),
        ocr_interval_seconds=overrides.get("ocr_interval_seconds", 30),
        idle_threshold_seconds=overrides.get("idle_threshold_seconds", 300),
        affective_sensing=overrides.get("affective_sensing", False),
    )
    await set_context_config(db, cfg)


# --------------------------------------------------------------- presence

async def test_presence_unknown_without_signal(db_session):
    await _enable(db_session)
    model = await get_world_model(db_session, use_cache=False)
    assert model.presence == "unknown"
    assert model.active_app is None
    assert model.sensing["enabled"] is True


async def test_presence_active(db_session):
    await _enable(db_session, idle_threshold_seconds=300)
    record_device_signal("Code.exe", "main.py — Jarvis", idle_seconds=5)
    model = await get_world_model(db_session, use_cache=False)
    assert model.presence == "active"
    assert model.active_app == "Code.exe"
    assert model.window_title == "main.py — Jarvis"
    assert model.sensing["device_fresh"] is True


async def test_presence_idle_over_threshold(db_session):
    await _enable(db_session, idle_threshold_seconds=60)
    record_device_signal("Slack", "general", idle_seconds=120)
    model = await get_world_model(db_session, use_cache=False)
    assert model.presence == "idle"


async def test_presence_away_when_signal_stale(db_session, monkeypatch):
    await _enable(db_session)
    record_device_signal("Code.exe", "x", idle_seconds=1)
    # Freshness window collapses → the just-stored signal is already stale.
    monkeypatch.setattr(context_store, "DEVICE_FRESH_SECONDS", -1.0)
    model = await get_world_model(db_session, use_cache=False)
    assert model.presence == "away"
    assert model.active_app is None       # stale signal is nulled
    assert model.sensing["device_fresh"] is False


# ------------------------------------------------------- master kill switch

async def test_master_off_returns_empty_model(db_session):
    await _enable(db_session, enabled=False)
    record_device_signal("Code.exe", "secret.txt", idle_seconds=1)
    record_ocr_summary("something on screen")
    model = await get_world_model(db_session, use_cache=False)
    assert model.presence == "unknown"
    assert model.active_app is None       # nothing surfaced while master is off
    assert model.on_screen_context is None
    assert model.sensing["enabled"] is False


# --------------------------------------------------------------- OCR section

async def test_on_screen_context_surfaced(db_session):
    await _enable(db_session, screen_ocr=True)
    record_ocr_summary("Inbox · Compose · 3 unread")
    model = await get_world_model(db_session, use_cache=False)
    assert model.on_screen_context == "Inbox · Compose · 3 unread"
    assert model.sensing["ocr_fresh"] is True


async def test_on_screen_context_nulled_when_stale(db_session, monkeypatch):
    await _enable(db_session, screen_ocr=True)
    record_ocr_summary("stale text")
    # Collapse both terms of the freshness window (max(min, interval×mult)).
    monkeypatch.setattr(context_store, "OCR_FRESH_MIN_SECONDS", -1.0)
    monkeypatch.setattr(context_store, "OCR_FRESH_INTERVAL_MULTIPLIER", -1)
    model = await get_world_model(db_session, use_cache=False)
    assert model.on_screen_context is None
    assert model.sensing["ocr_fresh"] is False


# ---------------------------------------------------- best-effort Google

async def test_google_sections_absent_when_not_connected(db_session):
    # No factory set + no token → GoogleNotConnectedError → sections drop, never raise.
    await _enable(db_session)
    model = await get_world_model(db_session, use_cache=False)
    assert model.next_calendar_event is None
    assert model.unread is None


async def test_calendar_and_unread_populated(db_session, monkeypatch):
    await _enable(db_session)
    event = {
        "id": "e1",
        "summary": "Standup",
        "start": {"dateTime": "2026-07-20T10:00:00-07:00"},
        "end": {"dateTime": "2026-07-20T10:30:00-07:00"},
    }
    monkeypatch.setattr(
        "app.integrations.google_services.CALENDAR_SERVICE_FACTORY",
        lambda: FakeCalendar([event]),
    )
    monkeypatch.setattr(
        "app.integrations.google_services.GMAIL_SERVICE_FACTORY",
        lambda: FakeGmail(),
    )
    model = await get_world_model(db_session, use_cache=False)
    assert model.next_calendar_event is not None
    assert model.next_calendar_event["summary"] == "Standup"
    assert "when" in model.next_calendar_event
    assert model.unread == {"count": 3, "has_urgent": True}


# --------------------------------------------------------- recent-file focus

async def test_recent_file_focus(db_session):
    await _enable(db_session)
    db_session.add_all([
        FileIndex(path="/x/old.txt", folder_root="/x", filename="old.txt", mtime=100.0),
        FileIndex(path="/x/new.md", folder_root="/x", filename="new.md", mtime=999999.0),
    ])
    await db_session.commit()
    model = await get_world_model(db_session, use_cache=False)
    assert model.recent_file_focus is not None
    assert model.recent_file_focus["filename"] == "new.md"


# --------------------------------------------------------------- status

async def test_context_status_cheap(db_session):
    await _enable(db_session, screen_ocr=False)
    record_device_signal("App", "t", idle_seconds=1)
    status = await context_status(db_session)
    assert status["enabled"] is True
    assert status["device_sensing"] is True
    assert status["screen_ocr"] is False
    assert status["device_fresh"] is True


async def test_status_dark_when_master_off(db_session):
    await _enable(db_session, enabled=False)
    record_device_signal("App", "t", idle_seconds=1)
    status = await context_status(db_session)
    assert status["enabled"] is False
    assert status["device_fresh"] is False   # gated by the master switch


# --------------------------------------------------- affective (Phase 13)

async def test_user_state_absent_when_affective_off(db_session):
    # Master on, affective opt-in OFF → no load read even with a fresh signal.
    await _enable(db_session, affective_sensing=False)
    record_affective_signal(typing_cpm=300, backspace_rate=0.0, voice_energy=None)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state is None
    assert model.sensing["affective_sensing"] is False


async def test_user_state_none_without_any_signal(db_session):
    # Affective on but nothing posted and no device signal → honest None.
    await _enable(db_session, affective_sensing=True)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state is None
    assert model.sensing["affective_sensing"] is True


async def test_user_state_calm_when_quiet(db_session):
    await _enable(db_session, affective_sensing=True)
    record_affective_signal(typing_cpm=10, backspace_rate=0.0, voice_energy=None)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state is not None
    assert model.user_state["load"] == "calm"
    assert high_load(model.user_state) is False


async def test_user_state_busy_when_typing_fast(db_session):
    await _enable(db_session, affective_sensing=True)
    record_affective_signal(typing_cpm=400, backspace_rate=0.05, voice_energy=None)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state["load"] == "busy"
    assert model.user_state["signals"]["typing_cpm"] == 400.0
    assert high_load(model.user_state) is True


async def test_user_state_stressed_from_high_backspace(db_session):
    await _enable(db_session, affective_sensing=True)
    # Fast typing AND a high delete rate → effortful → stressed.
    record_affective_signal(typing_cpm=300, backspace_rate=0.6, voice_energy=None)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state["load"] == "stressed"
    assert high_load(model.user_state) is True


async def test_user_state_confidence_scales_with_sources(db_session):
    await _enable(db_session, affective_sensing=True)
    # Two source families (typing + voice) present → confidence ~2/3.
    record_affective_signal(typing_cpm=300, backspace_rate=0.1, voice_energy=0.7)
    model = await get_world_model(db_session, use_cache=False)
    assert model.user_state["confidence"] == pytest.approx(0.67, abs=0.01)


async def test_user_state_dark_when_master_off(db_session):
    await _enable(db_session, enabled=False, affective_sensing=True)
    record_affective_signal(typing_cpm=400, backspace_rate=0.5, voice_energy=0.9)
    model = await get_world_model(db_session, use_cache=False)
    # Master off → no load read at all (dark), even though the sub-toggle is on
    # (sensing echoes the configured flag, like device_sensing/screen_ocr do).
    assert model.user_state is None
    assert model.sensing["enabled"] is False
    assert model.sensing["affective_sensing"] is True


def test_high_load_predicate_gates_on_confidence():
    assert high_load(None) is False
    assert high_load({"load": "calm", "confidence": 1.0}) is False
    # Busy but too low-confidence to act on.
    assert high_load({"load": "busy", "confidence": 0.2}) is False
    assert high_load({"load": "busy", "confidence": 0.5}) is True
    assert high_load({"load": "stressed", "confidence": 0.34}) is True
