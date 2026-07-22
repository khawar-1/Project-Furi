"""
Empty-revision disambiguation (2026-07-21).

The live incident: a 3-step browse plan's last step (an impossible `back` in a
fresh session) failed; the replanner correctly returned an empty revision whose
reason read "The goal has been fully accomplished…" — and the plan FAILED
carrying that sentence, because `_revise_node` treated EVERY empty revision on
the failure path as surrender. The revise prompt says an empty steps array means
one of TWO opposite things (already accomplished / impossible), and prose cannot
be told apart in code — so the draft schema gained `goal_accomplished`, the
structural comparator.

These pin: flag + something completed → the plan COMPLETES with the failed step
still visible; no flag → the surrender path stands unchanged; a lying flag on a
plan with zero results is invalid output, never a completion.
"""
import json

from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus

from tests.test_agent_planner import FakeProvider, plan_json, step


def accomplished_json(reason: str) -> str:
    return json.dumps(
        {"steps": [], "goal_accomplished": True, "unachievable_reason": reason}
    )


async def test_an_accomplished_empty_revision_completes_the_plan(db_session, tmp_path):
    """The incident frozen: step 1 completed, step 2 failed, the revise says the
    executed results already accomplish the goal → COMPLETED (not the
    self-contradicting 'I couldn't finish that. The goal has been fully
    accomplished'), the failed step staying visible and honest."""
    real = tmp_path / "book.txt"
    real.write_text("It's Only the Himalayas — £45.17")
    ghost = tmp_path / "ghost.txt"
    ghost.mkdir()  # read_file on a directory fails deterministically

    provider = FakeProvider([
        plan_json([
            step("Read the book page", "read_file", path=str(real)),
            step("Go back to the list", "read_file", path=str(ghost)),
        ]),
        accomplished_json(
            "The goal has been fully accomplished — the book page was read; "
            "the go-back step is not needed to report the result."
        ),
    ])

    plan = await AgentPlanner(db_session, provider).start("read the book page")

    assert plan.status == PlanStatus.COMPLETED
    assert "fully accomplished" in (plan.message or "")
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[1].status == StepStatus.FAILED   # visible, never hidden


async def test_without_the_flag_the_surrender_path_stands(db_session, tmp_path):
    """An empty revision + reason WITHOUT goal_accomplished is still the honest
    'impossible' failure — the disambiguation must not soften real surrenders."""
    real = tmp_path / "a.txt"
    real.write_text("x")
    ghost = tmp_path / "ghost.txt"
    ghost.mkdir()

    provider = FakeProvider([
        plan_json([
            step("Read a", "read_file", path=str(real)),
            step("Read ghost", "read_file", path=str(ghost)),
        ]),
        plan_json([], reason="ghost.txt is a directory — the rest is impossible"),
    ])

    plan = await AgentPlanner(db_session, provider).start("read both files")

    assert plan.status == PlanStatus.FAILED
    assert "impossible" in (plan.message or "")


async def test_a_lying_flag_with_zero_results_is_invalid_output(db_session):
    """goal_accomplished=true on a plan where NOTHING has run cannot complete
    anything — it is rejected like invalid JSON (both attempts) and the plan
    fails as unplannable rather than 'completing' having done nothing."""
    provider = FakeProvider([
        json.dumps({"steps": [], "goal_accomplished": True}),
        json.dumps({"steps": [], "goal_accomplished": True}),
    ])

    plan = await AgentPlanner(db_session, provider).start("do the thing")

    assert plan.status == PlanStatus.FAILED
    assert not any(s.status == StepStatus.COMPLETED for s in plan.steps)
