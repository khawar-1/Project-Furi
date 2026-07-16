"""
Screen-aware chat — the chat-injection half. screen_context_for_chat's labeled
block rides into the system prompt via the best-effort _screen_note (the
_affective_note pattern): gated on master + screen_ocr + screen_in_chat, "" on
any failure, and framed context-only AFTER the honesty rules — with the
source-acknowledgement instruction ("Based on what's on your screen…") and the
never-invent rule, neither of which may override honesty.
"""
from app.api.chat import _build_system_prompt, _screen_note
from app.core import context_store
from app.core.app_settings import ContextConfig, set_context_config
from app.core.context_store import record_ocr_summary


def _config(**overrides) -> ContextConfig:
    return ContextConfig(
        enabled=overrides.get("enabled", True),
        device_sensing=True,
        screen_ocr=overrides.get("screen_ocr", True),
        ocr_interval_seconds=30,
        idle_threshold_seconds=300,
        affective_sensing=False,
        screen_in_chat=overrides.get("screen_in_chat", True),
    )


# ------------------------------------------------------------ prompt block

def test_build_prompt_includes_screen_block():
    # "SCREEN CONTEXT (" is the block header; the bare words also appear in the
    # static CAPABILITIES screen-awareness rule, so key on the paren form.
    p = _build_system_prompt(screen_note='CURRENT SCREEN (~3s ago, in Code.exe):\ndef foo(): ...')
    assert "SCREEN CONTEXT (" in p and "def foo(): ..." in p
    # It comes after the honesty/persona rules — never rewrites them.
    assert p.index("RESPONSE STYLE") < p.index("SCREEN CONTEXT (")


def test_screen_block_framing_is_context_only_and_source_acknowledging():
    p = _build_system_prompt(screen_note="CURRENT SCREEN: x")
    assert "never invent screen contents" in p
    assert "acknowledge the source" in p
    assert "may or may not be asking about it" in p
    assert "Do NOT narrate the screen unprompted" in p


def test_build_prompt_omits_screen_block_by_default():
    # The block header is absent without a note (the CAPABILITIES rule may
    # still NAME the block — that reference is exactly what keeps the LLM
    # honest when the block is missing).
    assert "SCREEN CONTEXT (" not in _build_system_prompt()


# ------------------------------------------------------------- _screen_note

async def test_screen_note_returns_block_when_fully_opted_in(db_session):
    await set_context_config(db_session, _config())
    record_ocr_summary("condensed", full_text="the article body text")
    note = await _screen_note(db_session)
    assert "CURRENT SCREEN" in note
    assert "the article body text" in note


async def test_screen_note_empty_when_any_flag_off(db_session):
    record_ocr_summary("condensed", full_text="secret")
    for off in ("enabled", "screen_ocr", "screen_in_chat"):
        await set_context_config(db_session, _config(**{off: False}))
        assert await _screen_note(db_session) == "", f"{off}=False must gate"


async def test_screen_note_honest_without_captures(db_session):
    """Opted-in + no capture → the no-capture note rides into the prompt (the
    LLM must explain honestly, never invent a 'take a screenshot' ritual)."""
    await set_context_config(db_session, _config())
    note = await _screen_note(db_session)
    assert "NO FRESH SCREEN CAPTURE" in note
    assert "take a screenshot" in note


def test_capabilities_forbid_screenshot_magic_words():
    """The static CAPABILITIES prompt kills the fabricated trigger phrase even
    when screen awareness is fully off (no screen_note at all)."""
    p = _build_system_prompt()
    assert "SCREEN AWARENESS" in p
    assert "take a screenshot" in p
    assert "captured automatically" in p
    assert "Settings" in p


async def test_screen_note_empty_on_failure(db_session, monkeypatch):
    """Best-effort: a context-store failure degrades to '' — a chat turn is
    never broken by the screen path."""
    await set_context_config(db_session, _config())
    record_ocr_summary("condensed", full_text="t")

    async def _boom(_db):
        raise RuntimeError("context store down")

    monkeypatch.setattr(context_store, "screen_context_for_chat", _boom)
    assert await _screen_note(db_session) == ""
