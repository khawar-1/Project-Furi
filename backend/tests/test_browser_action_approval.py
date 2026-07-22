"""
Action-approval hand-off (2026-07-22).

A READ browse must never SEND / POST / SUBMIT / UPLOAD / LIKE / DELETE / BUY on a
live site without the user's yes (the LinkedIn message-send incident: a send in a
READ browse). The loop STOPS at a world-acting gesture and returns
action_approval_required; the planner turns that into an AWAITING_CHOICE pause,
and only a clear "yes" resumes the browse with action_approved lifting the gate
for that one run. These pin the planner wiring end to end.
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
    """A clear 'yes' resumes the browse with action_approved=True injected into
    the step's parameters (the gate lift), and it completes."""
    seq = [_action_result(), _success_result()]
    seen: list[tuple] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append((tool, bool(params.get("action_approved"))))
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-act2")

    plan = await planner.start(_GOAL)
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "yes")

    assert resumed.status == PlanStatus.COMPLETED
    # first run: gate NOT lifted; resume: action_approved injected in code.
    assert seen == [("browse", False), ("browse", True)]


async def test_no_cancels_without_acting(db_session, monkeypatch):
    """Anything but a clear yes (fail-closed) stops the plan without acting —
    nothing sent, posted, or submitted."""

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        # Must never run with the gate lifted after a decline.
        assert not params.get("action_approved")
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
