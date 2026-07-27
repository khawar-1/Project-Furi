"""
_provider_history (app/api/chat.py) — the capped conversation window sent to
the LLM.

The frontend sends the FULL session history every turn, so a long chat grew
the provider prompt linearly until every reply was noticeably slow (live
complaint 2026-07-13). Long-range recall belongs to the memory engine, not
the raw transcript — the provider gets a bounded recent window, oldest
trimmed first, the latest message always kept.
"""
from types import SimpleNamespace

from app.api.chat import _HISTORY_MAX_CHARS, _HISTORY_MAX_MESSAGES, _provider_history
from app.providers.base import LLMMessage


def _msg(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content)


def _convo(n: int, size: int = 10) -> list[SimpleNamespace]:
    return [
        _msg("user" if i % 2 == 0 else "assistant", f"m{i:03d}" + "x" * size)
        for i in range(n)
    ]


def test_short_history_passes_through_unchanged():
    history = _convo(6)
    out = _provider_history(history)
    assert len(out) == 6
    assert [m.role for m in out] == [m.role for m in history]
    assert out[-1].content == history[-1].content
    assert all(isinstance(m, LLMMessage) for m in out)


def test_long_history_keeps_only_the_newest_window():
    history = _convo(100)
    out = _provider_history(history)
    assert len(out) == _HISTORY_MAX_MESSAGES
    assert out[-1].content == history[-1].content          # newest kept
    assert out[0].content == history[-_HISTORY_MAX_MESSAGES].content


def test_char_budget_trims_oldest_first():
    # Five messages of 10k chars blow the 24k budget — the oldest go.
    history = [_msg("user", f"m{i}" + "y" * 10_000) for i in range(5)]
    out = _provider_history(history)
    assert sum(len(m.content) for m in out) <= _HISTORY_MAX_CHARS
    assert out[-1].content == history[-1].content
    assert len(out) < 5


def test_latest_message_survives_even_when_oversized():
    history = [_msg("user", "old"), _msg("user", "z" * (_HISTORY_MAX_CHARS + 5))]
    out = _provider_history(history)
    assert len(out) == 1
    assert out[0].content.startswith("z")


def test_empty_history_is_fine():
    assert _provider_history([]) == []


# ---- Empty-content entries (the PlanCard-host message) — live bug 2026-07-24 --
# The frontend hosts an approval / clarifying-question PlanCard as an assistant
# message with no text. Such an entry must (1) pass request validation instead of
# 422-ing the whole conversation, and (2) never reach the provider.

def test_empty_content_messages_are_dropped():
    history = [
        _msg("user", "hey"),
        _msg("assistant", ""),        # a pushed PlanCard host — no text
        _msg("assistant", "   "),     # whitespace-only counts as empty
        _msg("user", "what's the progress?"),
    ]
    out = _provider_history(history)
    assert [m.content for m in out] == ["hey", "what's the progress?"]
    assert all(m.content.strip() for m in out)


def test_history_of_only_empty_messages_yields_nothing():
    assert _provider_history([_msg("assistant", ""), _msg("assistant", "  ")]) == []


def test_chat_message_schema_tolerates_empty_content():
    """The schema must NOT reject empty content — one empty PlanCard-host entry
    in the history can no longer 422 the turn and lock the session."""
    from app.db.schemas import ChatMessage

    m = ChatMessage(role="assistant", content="")
    assert m.content == ""
    # A missing content field also defaults to empty rather than erroring.
    assert ChatMessage(role="assistant").content == ""
