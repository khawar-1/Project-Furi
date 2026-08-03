"""The routing audit trail (2026-08-03) — the record of what Jarvis DIDN'T do.

Routing fails OPEN by design, so a message that should have become a task and
instead became a chat reply used to leave zero evidence anywhere. On 2026-07-17 a
live miss genuinely could not be root-caused because of it.

The property that matters most here is the one these tests are built around:
THE THREE CAUSES OF A CHAT OUTCOME ARE TOLD APART. "the gate never fired", "the
model judged it conversation" and "the model call failed and CHAT is the fail-open
default" are identical from the user's seat and need completely different fixes.
"""
import asyncio

import pytest
from sqlalchemy import select

from app.api import task_router
from app.core import routing_trace as rt
from app.db.models import RoutingDecision


# ------------------------------------------------------------------ fakes
class FakeProvider:
    """One scripted reply, and a call counter — the counter is what proves the
    'costs no LLM call' claims rather than merely asserting the outcome."""

    provider_name = "fake"
    model_name = "fake-model-v1"

    def __init__(self, reply: str = "TASK DELEGATE", raises: Exception | None = None):
        self.reply = reply
        self.raises = raises
        self.calls = 0

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self._Reply(self.reply)


@pytest.fixture(autouse=True)
def _clean_trace():
    rt.reset()
    yield
    rt.reset()


# =================================================== the three CHAT causes
async def test_a_closed_gate_is_recorded_and_costs_no_llm_call():
    provider = FakeProvider()
    rt.begin("s1", "how are you today")
    decision = await task_router.decide_route("how are you today", "", provider)

    assert not decision.routed
    assert decision.fail_open_reason == rt.FAIL_GATE_CLOSED
    assert decision.trace.gate_fired is False
    assert decision.trace.gate_reason == ""
    # A gate miss cannot be fixed downstream — the classifier is never asked.
    assert provider.calls == 0
    assert decision.trace.classifier_ms is None


async def test_a_classifier_chat_verdict_is_recorded_as_a_judgement():
    provider = FakeProvider(reply="CHAT")
    rt.begin("s1", "i sent him the files yesterday")
    decision = await task_router.decide_route(
        "i sent him the files yesterday", "", provider
    )

    assert not decision.routed
    assert decision.fail_open_reason == rt.FAIL_CLASSIFIER_CHAT
    assert decision.trace.gate_fired is True
    assert provider.calls == 1
    # A real judgement carries NO error — that is what separates it from below.
    assert decision.trace.classifier_error is None


async def test_a_classifier_failure_is_told_apart_from_a_chat_verdict():
    """THE DISTINCTION THIS TABLE EXISTS FOR. Both produce an ordinary chat
    reply; one is the model reading the room and the other is the model being
    unreachable, and until this row existed they were indistinguishable."""
    provider = FakeProvider(raises=RuntimeError("connection reset"))
    rt.begin("s1", "delete my temp files")
    decision = await task_router.decide_route("delete my temp files", "", provider)

    assert not decision.routed
    assert decision.fail_open_reason == rt.FAIL_CLASSIFIER_ERROR
    assert decision.fail_open_reason != rt.FAIL_CLASSIFIER_CHAT
    assert "RuntimeError" in (decision.trace.classifier_error or "")
    assert "connection reset" in (decision.trace.classifier_error or "")
    # The cost is still recorded — a slow failure is a different problem again.
    assert decision.trace.classifier_ms is not None


async def test_an_unrecognized_reply_is_a_failure_not_a_chat_verdict():
    """A blank or garbled reply wears the same fail-open clothes as a CHAT
    verdict. It is a FAILURE — on a thinking model an empty reply once made
    EVERY message fall open to chat and Jarvis silently stopped doing tasks
    (2026-07-13). A rising rate of these is a provider problem, not a prompt one."""
    provider = FakeProvider(reply="")
    rt.begin("s1", "delete my temp files")
    decision = await task_router.decide_route("delete my temp files", "", provider)

    assert decision.fail_open_reason == rt.FAIL_CLASSIFIER_ERROR
    assert "unrecognized reply" in (decision.trace.classifier_error or "")


async def test_a_parked_question_defers_before_the_classifier_is_paid_for():
    """Position is load-bearing: after the classifier this would spend an LLM
    call on a turn that is owed to a question somewhere else."""
    provider = FakeProvider()
    rt.begin("s1", "delete my temp files")
    decision = await task_router.decide_route(
        "delete my temp files", "", provider, defer_check=lambda: True
    )

    assert decision.fail_open_reason == rt.FAIL_PARKED_QUESTION
    assert provider.calls == 0


# ============================================================ routed turns
async def test_a_routed_turn_records_label_agent_and_execution():
    provider = FakeProvider(reply="TASK DELEGATE")
    rt.begin("s1", "delete my temp files")
    decision = await task_router.decide_route("delete my temp files", "", provider)

    assert decision.routed
    assert decision.trace.label == "TASK"
    assert decision.trace.mode == "DELEGATE"
    assert decision.trace.agent == "file"
    assert decision.trace.execution == "delegate"
    assert decision.trace.outcome == rt.OUTCOME_TASK_BACKGROUND
    assert decision.trace.fail_open_reason == ""
    assert decision.trace.gate_reason == "strong_domain"
    # Which model produced the verdict — so "did routing regress when we changed
    # models?" is answerable (deepseek-chat -> deepseek-v4-flash, 2026-07-24).
    assert decision.trace.classifier_model == "fake-model-v1"


async def test_an_inline_read_records_inline_execution():
    provider = FakeProvider(reply="TASK INLINE")
    rt.begin("s1", "list the files on my desktop")
    decision = await task_router.decide_route(
        "list the files on my desktop", "", provider
    )
    assert decision.trace.execution == "inline"
    assert decision.trace.outcome == rt.OUTCOME_TASK_INLINE


async def test_bare_navigation_is_recorded_as_decided_in_code():
    """The label column is deliberately NOT called classifier_label: this path
    decides BROWSE without asking the model at all. classifier_ms IS NULL is the
    precise test for 'no LLM call was made'."""
    provider = FakeProvider()
    rt.begin("s1", "open junaidjamshed.com")
    decision = await task_router.decide_route("open junaidjamshed.com", "", provider)

    assert decision.routed and decision.trace.label == "BROWSE"
    assert decision.trace.bare_navigation is True
    assert provider.calls == 0
    assert decision.trace.classifier_ms is None
    assert decision.trace.classifier_model is None


async def test_background_intent_is_recorded_and_the_goal_is_stripped():
    provider = FakeProvider(reply="TASK INLINE")
    goal = "delete the tmp files in my downloads and tell me when you're done"
    rt.begin("s1", goal)
    decision = await task_router.decide_route(goal, "", provider)

    assert decision.trace.background_intent is True
    assert "tell me when" not in decision.run_goal
    # Explicit background intent is a user override — it delegates whatever mode
    # the classifier returned.
    assert decision.trace.execution == "delegate"


# ================================================= the ContextVar mutation rule
async def test_a_stamp_inside_a_gather_reaches_the_parent_trace():
    """⚠️ THE RULE THE MODULE IS BUILT ON. _classify_message runs inside an
    asyncio.gather, and a child task gets a COPY of the context — so a
    ContextVar.set() there would not propagate back out, while MUTATING the
    object the var already points at does. If this ever fails, every
    classifier_* field silently stops being recorded on real turns while every
    other test here still passes."""
    trace = rt.begin("s1", "delete my temp files")

    async def _child():
        rt.note_classified("EMAIL", "INLINE", ms=42, error=None, model="m")

    async def _other():
        return "memory"

    await asyncio.gather(_child(), _other())

    assert trace.label == "EMAIL"
    assert trace.classifier_ms == 42


async def test_stamping_without_a_trace_is_a_no_op_not_a_crash():
    """Every router stays callable from tests, scripts and the non-streaming
    route with no fixture and no change."""
    rt.reset()
    rt.note_gate(True, "strong_domain")
    rt.note_classified("TASK", "DELEGATE", ms=1)
    rt.note_label("BROWSE", "DELEGATE")
    rt.note_bare_navigation()
    rt.note_background()
    rt.note_outcome(rt.OUTCOME_REMINDER)
    rt.note_fail_open(rt.FAIL_GATE_CLOSED)
    assert rt.current() is None


async def test_decide_route_makes_its_own_trace_when_called_outside_a_turn():
    """route_bench.py and tests call decide_route with no chat turn around it;
    the decision must still be fully populated."""
    rt.reset()
    provider = FakeProvider(reply="WEB INLINE")
    decision = await task_router.decide_route("who won the match today", "", provider)
    assert decision.trace is not None
    assert decision.label == "WEB"
    assert decision.trace.gate_reason == "external_question"


# ==================================================================== writing
async def test_flush_writes_one_row_with_the_decision_on_it(db_session):
    trace = rt.begin("sess-1", "delete my temp files")
    provider = FakeProvider(reply="TASK DELEGATE")
    await task_router.decide_route("delete my temp files", "", provider)

    assert await rt.flush(db_session, trace) is True

    rows = (await db_session.execute(select(RoutingDecision))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id == "sess-1"
    assert row.outcome == rt.OUTCOME_TASK_BACKGROUND
    assert row.label == "TASK"
    assert row.gate_reason == "strong_domain"
    assert row.route_ms is not None


async def test_a_long_message_is_clipped_but_its_true_length_survives(db_session):
    long_message = "delete my files " + ("x" * 2000)
    trace = rt.begin("sess-1", long_message)
    await rt.flush(db_session, trace)

    row = (await db_session.execute(select(RoutingDecision))).scalars().one()
    assert len(row.message) == rt.ROUTING_MESSAGE_MAX_CHARS
    assert row.message_chars == len(long_message)


async def test_a_failed_write_rolls_back_and_never_raises(db_session, monkeypatch):
    """Observability must never be able to cost a turn. A failed INSERT that
    left the session in a failed-transaction state is the 2026-07-12 incident —
    one missing column silently disabled chat history AND background tasks."""
    trace = rt.begin("sess-1", "hello")

    async def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_session, "commit", _boom)
    rolled = {"n": 0}
    real_rollback = db_session.rollback

    async def _count_rollback():
        rolled["n"] += 1
        await real_rollback()

    monkeypatch.setattr(db_session, "rollback", _count_rollback)

    assert await rt.flush(db_session, trace) is False
    assert rolled["n"] == 1


async def test_flush_of_nothing_is_harmless(db_session):
    assert await rt.flush(db_session, None) is False


async def test_a_web_rescue_amends_the_row_it_already_wrote(db_session):
    """The row is written when routing decides — before the chat model has said
    anything. A fired rescue means routing MISSED this turn and the dead-end
    backstop caught it, which only becomes knowable later."""
    trace = rt.begin("sess-1", "which teams qualified")
    rt.note_fail_open(rt.FAIL_GATE_CLOSED)
    await rt.flush(db_session, trace)

    assert await rt.note_stream_outcome(
        db_session, trace, rescue_fired=True, rescue_ok=True
    ) is True

    row = (await db_session.execute(select(RoutingDecision))).scalars().one()
    assert row.rescue_fired is True
    assert row.rescue_ok is True
    assert row.outcome == rt.OUTCOME_CHAT_RESCUED
    # The reason it fell open is NOT overwritten — that is the diagnosis.
    assert row.fail_open_reason == rt.FAIL_GATE_CLOSED


async def test_an_impersonation_cut_amends_the_row(db_session):
    trace = rt.begin("sess-1", "please del all the txt files")
    await rt.flush(db_session, trace)
    await rt.note_stream_outcome(db_session, trace, impersonation_cut=True)

    row = (await db_session.execute(select(RoutingDecision))).scalars().one()
    assert row.impersonation_cut is True
    # An impersonation is not a rescue — the outcome must not be rewritten.
    assert row.outcome == rt.OUTCOME_CHAT


async def test_an_update_with_nothing_to_say_writes_nothing(db_session):
    trace = rt.begin("sess-1", "hello")
    await rt.flush(db_session, trace)
    assert await rt.note_stream_outcome(db_session, trace) is False


# ================================================================= retention
async def test_purge_drops_old_rows_and_keeps_recent_ones(db_session):
    from datetime import timedelta

    from app.db.models import utc_now

    old = RoutingDecision(session_id="s", message="old", outcome="chat")
    old.created_at = utc_now() - timedelta(days=rt.ROUTING_RETENTION_DAYS + 1)
    recent = RoutingDecision(session_id="s", message="recent", outcome="chat")
    recent.created_at = utc_now() - timedelta(days=1)
    db_session.add_all([old, recent])
    await db_session.commit()

    deleted = await rt.purge_old_decisions(db_session)

    assert deleted == 1
    rows = (await db_session.execute(select(RoutingDecision))).scalars().all()
    assert [r.message for r in rows] == ["recent"]


async def test_the_housekeeping_sweep_purges_routing_decisions(db_session, monkeypatch):
    """The sweep is where retention actually happens — a purge function nobody
    calls is the 'no-op that reports success' shape this codebase keeps hitting."""
    from datetime import timedelta

    from app.core import housekeeping
    from app.db.models import utc_now

    old = RoutingDecision(session_id="s", message="old", outcome="chat")
    old.created_at = utc_now() - timedelta(days=rt.ROUTING_RETENTION_DAYS + 1)
    db_session.add(old)
    await db_session.commit()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", _Factory())
    await housekeeping.run_housekeeping_pass()

    rows = (await db_session.execute(select(RoutingDecision))).scalars().all()
    assert rows == []
