"""
Action-approval hand-off (2026-07-22).

A READ browse must never SEND / POST / SUBMIT / UPLOAD / LIKE / DELETE / BUY on a
live site without the user's yes (the LinkedIn message-send incident: a send in a
READ browse). The loop STOPS at a world-acting gesture and returns
action_approval_required; the planner turns that into an AWAITING_CHOICE pause,
and only a clear "yes" resumes the browse carrying THAT gesture's permit. These
pin the planner wiring end to end.

THE PERMIT, not a blanket yes (2026-07-26). The approval used to be a run-wide
boolean: `action_approved=True` lifted the gate for EVERY world-acting gesture in
the resumed run, so a yes to "send this message" also authorised any buy, delete or
post the loop chose next. It is now a fingerprint of the exact control on the exact
site (browser.loop.gesture_fingerprint), consumed when that gesture fires — the
`arm_commit` one-shot permit discipline, applied to a gesture.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import planner as planner_mod
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult

from tests.test_agent_planner import FakeProvider, plan_json, step


def _action_result() -> ToolResult:
    return ToolResult(
        success=False,
        output={
            "action_approval_required": True,
            "action_description": 'send "hi anas this is furi"',
            "action_site": "linkedin.com",
            "action_fingerprint": _FINGERPRINT,
        },
        error="needs your approval to act on this page",
        permission_level=PermissionLevel.READ,
    )


def _success_result() -> ToolResult:
    return ToolResult(
        success=True,
        output={
            "url": "https://www.linkedin.com/messaging/thread/1/",
            "title": "Messaging",
            "rendered": "sent",
            "goal_reached": True,
        },
        error="",
        permission_level=PermissionLevel.READ,
    )


def _browse_step() -> dict:
    return step(
        "Message Anas", "browse",
        goal="message anas mubashar hi",
        start_url="https://www.linkedin.com/messaging/",
        allowed_origins=["linkedin.com"],
        keep_open=True,
    )


_FINGERPRINT = "abc123def4567890"

_GOAL = "go to linkedin and message anas mubashar 'hi anas this is furi'"


async def test_a_world_acting_gesture_pauses_the_plan(db_session, monkeypatch):
    """The tool's action signal → AWAITING_CHOICE (kind action_approval), not a
    failed/replanned step. The browse step is left PENDING for the resume."""
    calls = {"n": 0}

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls["n"] += 1
        assert tool == "browse"
        return _action_result()

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])

    plan = await AgentPlanner(db_session, provider, session_id="s-act").start(_GOAL)

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert plan.question.kind == "action_approval"
    assert "hi anas this is furi" in plan.question.text
    assert calls["n"] == 1
    assert all(s.status != StepStatus.COMPLETED for s in plan.steps)


async def test_yes_resumes_with_the_gate_lifted_and_completes(db_session, monkeypatch):
    """A clear 'yes' resumes the browse carrying the PERMIT for that one gesture,
    injected into the step's parameters, and it completes."""
    seq = [_action_result(), _success_result()]
    seen: list[tuple] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append((tool, str(params.get("approved_gesture") or "")))
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-act2")

    plan = await planner.start(_GOAL)
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "yes")

    assert resumed.status == PlanStatus.COMPLETED
    # First run carries NO permit; the resume carries exactly the one the loop
    # asked about — not a boolean that would cover any gesture at all.
    assert seen == [("browse", ""), ("browse", _FINGERPRINT)]
    assert resumed.approved_action_fingerprint == _FINGERPRINT


async def test_no_cancels_without_acting(db_session, monkeypatch):
    """Anything but a clear yes (fail-closed) stops the plan without acting —
    nothing sent, posted, or submitted."""

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        # Must never run with a permit after a decline.
        assert not params.get("approved_gesture")
        return _action_result()

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-act3")

    plan = await planner.start(_GOAL)
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "no")

    assert resumed.status == PlanStatus.CANCELLED
    assert "nothing was sent" in (resumed.message or "").lower()
    assert all(s.status != StepStatus.COMPLETED for s in resumed.steps)


async def test_a_plan_parked_before_the_permit_existed_approves_nothing(db_session, monkeypatch):
    """FAIL CLOSED on the upgrade. A pause that carries no fingerprint (an outcome
    from before this change, or a malformed one) must approve NOTHING rather than
    fall back to a blanket yes — the loop then pauses again, which is the safe way
    to be wrong."""
    stale = ToolResult(
        success=False,
        output={
            "action_approval_required": True,
            "action_description": 'send "hi"',
            "action_site": "linkedin.com",
            # no action_fingerprint at all
        },
        error="needs your approval to act on this page",
        permission_level=PermissionLevel.READ,
    )
    seq = [stale, _success_result()]
    seen: list[str] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append(str(params.get("approved_gesture") or ""))
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-act4")

    plan = await planner.start(_GOAL)
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "yes")
    assert seen == ["", ""]                     # never a permit
    assert resumed.approved_action_fingerprint == ""
