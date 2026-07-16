"""
Phase 13.2b — the chat brevity addendum. The World-Model load read (busy/
stressed, confidently) appends a best-effort ADAPTIVE NOTE steering the reply
shorter; it is off by default, never fabricated, and always sits AFTER the
honesty rules so it can't soften them.
"""
from app.api.chat import _BUSY_NOTE, _affective_note, _build_system_prompt
from app.core import context_store
from app.core.app_settings import ContextConfig, set_context_config


def test_build_prompt_includes_affective_note():
    p = _build_system_prompt(affective_note="Keep it short.")
    assert "ADAPTIVE NOTE" in p and "Keep it short." in p
    # It comes after the honesty/persona rules — never rewrites them.
    assert p.index("RESPONSE STYLE") < p.index("ADAPTIVE NOTE")


def test_build_prompt_omits_note_by_default():
    assert "ADAPTIVE NOTE" not in _build_system_prompt()


async def _enable_affective(db, on=True):
    await set_context_config(db, ContextConfig(
        enabled=True, device_sensing=True, screen_ocr=False,
        ocr_interval_seconds=30, idle_threshold_seconds=300,
        affective_sensing=on,
    ))


async def test_affective_note_fires_under_load(db_session):
    await _enable_affective(db_session)
    context_store.record_affective_signal(400, 0.6, None)  # busy + high strain
    assert await _affective_note(db_session) == _BUSY_NOTE


async def test_affective_note_empty_when_calm(db_session):
    await _enable_affective(db_session)
    context_store.record_affective_signal(10, 0.0, None)
    assert await _affective_note(db_session) == ""


async def test_affective_note_empty_when_affective_off(db_session):
    await _enable_affective(db_session, on=False)
    context_store.record_affective_signal(400, 0.6, None)
    assert await _affective_note(db_session) == ""
