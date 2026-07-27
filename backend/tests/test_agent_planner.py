"""
Part 4 — LangGraph agent planner.
A scripted FakeProvider drives every graph path deterministically:
READ-only run-through, approval pause, resume/cancel, pre-approval
refinement of PENDING placeholders, unknown-tool retry, unachievable
goals, failure replanning with fresh re-approval, and the replan limit.
Real registered tools + tmp_path make these end-to-end below the LLM.
"""
import json
import sys
from typing import AsyncIterator, List, Optional

import pytest
from sqlalchemy import select

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store
from app.agents.planner import AgentPlanner, MAX_QUESTIONS, MAX_REPLANS
from app.agents.schemas import AgentPlan, PlanStatus, StepStatus
from app.core.base_tool import PermissionLevel
from app.db.models import ActivityLog
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)


class FakeProvider(LLMProvider):
    """Returns scripted responses in order; fails loudly if over-called."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.prompts: List[str] = []  # first message of every call, in order

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.calls += 1
        self.prompts.append(messages[0].content)
        if not self._responses:
            raise AssertionError(f"FakeProvider exhausted after {self.calls - 1} responses")
        return LLMResponse(
            content=self._responses.pop(0), model="fake-model", provider="fake",
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def step(description: str, tool: str, **parameters) -> dict:
    return {"description": description, "tool": tool, "parameters": parameters}


def plan_json(
    steps: list, reason: Optional[str] = None, question: Optional[dict] = None
) -> str:
    return json.dumps(
        {"steps": steps, "unachievable_reason": reason, "question": question}
    )


async def _activity_rows(db_session) -> list[ActivityLog]:
    result = await db_session.execute(select(ActivityLog))
    return list(result.scalars().all())


# ------------------------------------------------------------ action detail

def test_action_detail_is_code_derived_never_llm():
    """The LLM's description cannot hide the real action: action_detail is
    rendered in code from the parameters for every non-READ tool."""
    from app.agents.schemas import PlanDraft

    draft = PlanDraft.model_validate({"steps": [
        step("Tidy up the desktop", "run_command", command="del /q *.*",
             working_directory="C:\\Users\\x\\Desktop"),
        step("Organize a document", "delete_file", path="C:\\docs\\a.txt"),
        step("Check the folder", "list_directory", path="C:\\docs"),
    ]})
    steps, error = AgentPlanner._draft_to_steps(draft)
    assert error is None
    # The innocent description stands, but the verbatim command is exposed
    assert steps[0].action_detail == "$ del /q *.*   (in C:\\Users\\x\\Desktop)"
    assert steps[1].action_detail == "C:\\docs\\a.txt → moved to trash (~/.jarvis/trash)"
    assert steps[2].action_detail is None  # READ steps carry no detail line


# ---------------------------------------------------------- READ-only flows

async def test_read_only_plan_runs_to_completion(db_session, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(steps)])  # plan only

    planner = AgentPlanner(db_session, provider, session_id="s-read")
    plan = await planner.start("list my files")

    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[0].result.output["count"] == 1
    # ONE call: the reflection round trip is skipped for an all-READ draft
    # (latency, 2026-07-13) — write plans still reflect, see
    # test_write_step_pauses_before_executing_anything (calls == 2).
    assert provider.calls == 1


async def test_empty_goal_fails_without_llm_calls(db_session):
    provider = FakeProvider([])
    plan = await AgentPlanner(db_session, provider).start("   ")
    assert plan.status == PlanStatus.FAILED
    assert provider.calls == 0


async def test_unachievable_goal_fails_with_reason(db_session):
    provider = FakeProvider([plan_json([], reason="No email tool is available")])
    plan = await AgentPlanner(db_session, provider).start("email my report to Ali")
    assert plan.status == PlanStatus.FAILED
    assert "email" in plan.message.lower()
    assert provider.calls == 1  # failed at plan; reflect never ran


# ---------------------------------------------------------- approval gating

async def test_write_step_pauses_before_executing_anything(db_session, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    plan = await AgentPlanner(db_session, provider).start("create notes.txt")

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert not target.exists()  # NOTHING executed
    assert plan.steps[0].status == StepStatus.PENDING
    assert plan.steps[0].requires_approval is True
    assert plan.steps[0].permission_level == PermissionLevel.WRITE
    assert provider.calls == 2  # no refine: no completed steps to refine from


async def test_approved_plan_executes_and_audits(db_session, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider, session_id="s-appr")

    plan = await planner.start("create notes.txt")
    plan = await planner.resume(plan, approved=True)

    assert plan.status == PlanStatus.COMPLETED
    assert target.read_text() == "hi"
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert provider.calls == 2  # resume needed no LLM

    rows = await _activity_rows(db_session)
    assert any(r.tool_name == "create_file" and r.success for r in rows)


async def test_cancelled_plan_executes_nothing(db_session, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("create notes.txt")
    plan = await planner.resume(plan, approved=False)

    assert plan.status == PlanStatus.CANCELLED
    assert plan.steps[0].status == StepStatus.SKIPPED
    assert not target.exists()


async def test_resume_ignores_non_awaiting_plans(db_session):
    provider = FakeProvider([])
    done = AgentPlan(goal="g", status=PlanStatus.COMPLETED)
    result = await AgentPlanner(db_session, provider).resume(done, approved=True)
    assert result.status == PlanStatus.COMPLETED
    assert provider.calls == 0


async def test_destructive_permission_derived_from_registry_not_llm(db_session, tmp_path):
    """The LLM cannot downgrade a permission level — it's not even read."""
    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    llm_step = step("Tidy up", "delete_file", path=str(victim))
    llm_step["permission_level"] = "read"  # extractor-style lie — ignored
    llm_step["requires_approval"] = False
    provider = FakeProvider([plan_json([llm_step]), plan_json([llm_step])])

    plan = await AgentPlanner(db_session, provider).start("tidy up")

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert plan.steps[0].permission_level == PermissionLevel.DESTRUCTIVE
    assert plan.steps[0].requires_approval is True
    assert victim.exists()


# -------------------------- READ prefix + deterministic placeholder expansion

async def test_read_prefix_runs_then_placeholders_expand_in_code(db_session, tmp_path):
    """'Delete all .tmp files': the search runs pre-approval, then the vague
    delete template EXPANDS in code into one concrete delete per found file —
    no LLM refine call is spent on the placeholder mechanism working as
    designed (2026-07-10: two placeholder 'failures' burned two replans and
    the second died on the provider's daily rate limit)."""
    junk1 = tmp_path / "junk1.tmp"
    junk2 = tmp_path / "junk2.tmp"
    junk1.write_text("a")
    junk2.write_text("b")

    draft = [
        step("Find all .tmp files", "search_files", directory=str(tmp_path), file_type="tmp"),
        step("Delete each found file", "delete_file", path="PENDING: paths from step 1"),
    ]
    provider = FakeProvider([plan_json(draft), plan_json(draft)])
    planner = AgentPlanner(db_session, provider, session_id="s-tmp")

    plan = await planner.start("delete all tmp files in my folder")

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert provider.calls == 2  # plan, reflect — expansion needed no LLM
    # The search already ran; the user sees CONCRETE deletes
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[0].result.output["count"] == 2
    pending = plan.pending_steps()
    assert [s.parameters["path"] for s in pending] == [str(junk1), str(junk2)]
    assert all(s.permission_level == PermissionLevel.DESTRUCTIVE for s in pending)
    # The expanded steps carry code-derived descriptions and action detail
    assert pending[0].description == f"Delete junk1.tmp from {tmp_path}"
    assert str(junk1) in pending[0].action_detail
    assert junk1.exists() and junk2.exists()  # nothing deleted yet

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert not junk1.exists() and not junk2.exists()
    assert provider.calls == 2  # resume needed no LLM either


async def test_search_with_no_matches_skips_the_template_honestly(db_session, tmp_path):
    """Zero matches expand to zero steps: the template is visibly SKIPPED
    (never a red 'failed'), the message says why, and no LLM replan runs."""
    draft = [
        step("Find .tmp files", "search_files", directory=str(tmp_path), file_type="tmp"),
        step("Delete them", "delete_file", path="PENDING: found paths"),
    ]
    provider = FakeProvider([plan_json(draft), plan_json(draft)])

    plan = await AgentPlanner(db_session, provider).start("delete all tmp files")

    assert plan.status == PlanStatus.COMPLETED
    assert provider.calls == 2  # nothing-found is an outcome, not a replan
    assert "No matching files were found" in plan.message
    assert plan.steps[0].status == StepStatus.COMPLETED  # the search
    assert plan.steps[1].status == StepStatus.SKIPPED  # visible, not removed
    assert plan.pending_steps() == []


# -------------------------------------------------------- validation + retry

async def test_unknown_tool_fed_back_and_corrected(db_session, tmp_path):
    bad = [step("Wave the wand", "magic_wand", spell="ls")]
    good = [step("List files", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(bad), plan_json(good)])

    plan = await AgentPlanner(db_session, provider).start("list my files")

    assert plan.status == PlanStatus.COMPLETED
    assert provider.calls == 2  # plan (invalid), plan retry; all-READ → no reflect


async def test_broken_reflection_keeps_draft_plan(db_session, tmp_path):
    # A WRITE step, so the reflection round actually runs (all-READ drafts
    # skip it since 2026-07-13) — this test is about reflection resilience.
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = FakeProvider([
        plan_json(steps),
        "this is not json at all",  # reflect attempt 1
        "still not json",           # reflect retry
    ])

    plan = await AgentPlanner(db_session, provider).start("create notes.txt")

    # Draft survived the broken reflection and paused for its approval
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert plan.pending_steps()[0].parameters["path"] == str(target)
    assert provider.calls == 3


# ------------------------------------------------------------- failure paths

async def test_failed_step_replans_and_requires_fresh_approval(db_session, tmp_path):
    """A replanned WRITE step has a new signature the user never approved —
    the plan must pause again instead of executing it."""
    existing = tmp_path / "exists.txt"
    existing.write_text("old")
    fresh = tmp_path / "fresh.txt"

    first = [step("Create exists.txt", "create_file", path=str(existing), content="new")]
    replanned = [step("Create fresh.txt instead", "create_file", path=str(fresh), content="new")]
    provider = FakeProvider([plan_json(first), plan_json(first), plan_json(replanned)])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("save my note")
    assert plan.status == PlanStatus.AWAITING_APPROVAL

    plan = await planner.resume(plan, approved=True)  # create fails → replan
    assert provider.calls == 3
    assert plan.status == PlanStatus.AWAITING_APPROVAL  # fresh approval needed
    # The failed step is still visible — never silently skipped
    assert plan.steps[0].status == StepStatus.FAILED
    assert "overwrite" in plan.steps[0].result.error.lower()
    assert plan.pending_steps()[0].parameters["path"] == str(fresh)
    assert not fresh.exists()
    assert existing.read_text() == "old"

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert fresh.read_text() == "new"


async def test_replan_gives_up_after_limit(db_session, tmp_path):
    """READ steps fail without approval pauses; the replan limit must end it.
    The failures here are NOT the recoverable not-found class (read_file on a
    directory), so the cap still FAILS the plan instead of asking (the
    not-found class asks — see the ask-not-fail tests below)."""
    def dir_read(n: int) -> list:
        folder = tmp_path / f"folder{n}"
        folder.mkdir(exist_ok=True)
        return [step(f"Read folder {n}", "read_file", path=str(folder))]

    provider = FakeProvider([
        plan_json(dir_read(1)),  # plan
        plan_json(dir_read(1)),  # reflect
        plan_json(dir_read(2)),  # replan 1
        plan_json(dir_read(3)),  # replan 2
    ])

    plan = await AgentPlanner(db_session, provider).start("read my folder file")

    assert plan.status == PlanStatus.FAILED
    assert f"{MAX_REPLANS} replan attempts" in plan.message
    assert provider.calls == 4
    failed = [s for s in plan.steps if s.status == StepStatus.FAILED]
    assert len(failed) == 3  # every attempt visible, none skipped


async def test_replanner_can_declare_goal_impossible(db_session, tmp_path):
    """A non-recoverable failure (read_file on a directory) may still be
    declared impossible by the replanner — that honest failure stands."""
    folder = tmp_path / "ghost.txt"
    folder.mkdir()  # a DIRECTORY named like a file — reading it can't work
    provider = FakeProvider([
        plan_json([step("Read it", "read_file", path=str(folder))]),
        plan_json([step("Read it", "read_file", path=str(folder))]),
        plan_json([], reason="ghost.txt is a directory, not a readable file"),
    ])

    plan = await AgentPlanner(db_session, provider).start("read ghost.txt")

    assert plan.status == PlanStatus.FAILED
    assert "is a directory" in plan.message
    assert plan.steps[0].status == StepStatus.FAILED


async def test_revision_repeating_failed_step_rejected_structurally(db_session, tmp_path):
    """'NEVER repeat a step that will fail the same way' is enforced in code,
    not just the prompt (live failure 2026-07-10: an identical doomed
    search_files call was re-issued on both replan rounds until the cap).
    The revision's first attempt repeats the failed step verbatim — rejected
    with retry feedback before any tool runs; the second attempt's fix
    completes the plan. Only ONE failed execution ever happens."""
    folder = tmp_path / "ghost.txt"
    folder.mkdir()  # read_file on a directory fails deterministically
    doomed = [step("Read it", "read_file", path=str(folder))]
    fixed = [step("List the folder instead", "list_directory", path=str(folder))]

    provider = FakeProvider([
        plan_json(doomed),  # plan (all-READ → no reflect round)
        plan_json(doomed),  # revise attempt 1 — verbatim repeat → rejected in code
        plan_json(fixed),   # revise attempt 2 — corrected after the feedback
    ])
    plan = await AgentPlanner(db_session, provider).start("read ghost.txt")

    assert plan.status == PlanStatus.COMPLETED
    assert provider.calls == 3
    failed = [s for s in plan.steps if s.status == StepStatus.FAILED]
    assert len(failed) == 1  # the repeat never executed a second time


async def test_repeat_allowed_after_state_changing_prerequisite(db_session, tmp_path):
    """'Create the missing folder, then retry the same step' is a legitimate
    replan — a repeated failed signature preceded by a write/destructive step
    is NOT rejected, because the world may have changed by the time it runs."""
    target = tmp_path / "newdir" / "note.txt"
    create = [step("Create note.txt", "create_file", path=str(target), content="x")]
    retry_after_mkdir = [
        step("Create the missing folder", "run_command",
             command=f'mkdir "{target.parent}"', working_directory=str(tmp_path)),
        step("Create note.txt", "create_file", path=str(target), content="x"),
    ]

    provider = FakeProvider([
        plan_json(create),             # plan — parent folder doesn't exist
        plan_json(create),             # reflect
        plan_json(retry_after_mkdir),  # revise: mkdir + the SAME create step
    ])
    plan = await AgentPlanner(db_session, provider).start("save a note in newdir")

    # Not rejected: the plan pauses for approval of the mkdir command
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert provider.calls == 3
    assert plan.pending_steps()[0].tool == "run_command"


async def test_placeholder_never_reaches_a_tool(db_session, tmp_path):
    """An approved step still carrying PENDING: must fail, not execute."""
    vague = [step("Read something", "read_file", path="PENDING: which file?")]
    provider = FakeProvider([
        plan_json(vague),
        plan_json(vague),
        plan_json([], reason="Cannot determine which file to read"),
    ])

    plan = await AgentPlanner(db_session, provider).start("read the file")

    assert plan.status == PlanStatus.FAILED
    assert plan.steps[0].status == StepStatus.FAILED
    assert "PENDING" in plan.steps[0].result.error
    rows = await _activity_rows(db_session)
    assert rows == []  # the tool was never invoked


# ------------------------------------------------- path guard + conversation

async def test_guessed_path_fails_before_approval_pause(db_session, tmp_path):
    """Live regression: asked to rename a file 'in the phase3test folder', the
    LLM invented C:\\Users\\DELL\\phase3test instead of searching first. A step
    whose source path does not exist must fail into the replan loop BEFORE the
    user is asked to approve an action that cannot succeed."""
    real = tmp_path / "firstname.tmp"
    real.write_text("data")
    ghost = tmp_path / "wrong-guess" / "firstname.tmp"

    guessed = [step("Rename firstname.tmp", "rename_file",
                    path=str(ghost), new_name="name_changed.tmp")]
    corrected = [step("Rename firstname.tmp", "rename_file",
                      path=str(real), new_name="name_changed.tmp")]
    provider = FakeProvider(
        [plan_json(guessed), plan_json(guessed), plan_json(corrected)]
    )
    planner = AgentPlanner(
        db_session, provider, session_id="s-guess",
        conversation=f"user: the phase3test folder is at {tmp_path}",
    )

    plan = await planner.start("rename firstname.tmp in phase3test to name_changed")

    # The guessed step failed deterministically: no approval was requested for
    # it and no tool ever ran — the failure is pre-flight, not a tool error.
    assert plan.steps[0].status == StepStatus.FAILED
    assert "does not exist" in plan.steps[0].result.error
    assert "never guess a path" in plan.steps[0].result.error
    assert await _activity_rows(db_session) == []
    # The replan (which sees the conversation) is what awaits approval now
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert plan.pending_steps()[0].parameters["path"] == str(real)
    assert "RECENT CONVERSATION" in provider.prompts[2]  # the revise prompt
    assert str(tmp_path) in provider.prompts[2]

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert (tmp_path / "name_changed.tmp").read_text() == "data"
    assert not real.exists()


async def test_replanned_search_still_lists_the_folder_before_completing(db_session, tmp_path):
    """Live regression (2026-07-09): 'how many files are in phase3test' — the
    drafted list_directory guessed a wrong path and failed, the replan searched
    and FOUND the folder, then the plan completed without ever looking inside:
    the user got 'Done — 1 step(s) completed.' and no file names. The revise
    prompt now demands the failed step's work be re-added after the
    prerequisite (PENDING placeholder), refuses to end on a search when the
    goal asks about contents, and the deterministic completion text carries
    the real results."""
    from app.agents.rendering import deterministic_plan_text

    folder = tmp_path / "phase3test"
    folder.mkdir()
    for name in ("firstname.txt", "fisrtname.tmp", "fstname.tmp"):
        (folder / name).write_text("x")
    ghost = tmp_path / "wrong-guess" / "phase3test"

    guessed = [step("List the files in phase3test", "list_directory", path=str(ghost))]
    searched = [
        step("Find the phase3test folder", "search_files",
             query="phase3test", directory=str(tmp_path), include_folders=True),
        step("List the files inside it", "list_directory",
             path="PENDING: folder path from the search"),
    ]
    concrete = [step("List the files in phase3test", "list_directory", path=str(folder))]
    provider = FakeProvider([
        plan_json(guessed), plan_json(guessed),  # plan, reflect
        plan_json(searched),                      # replan 1 after the failed guess
        plan_json(concrete),                      # replan 2 fills the placeholder
    ])

    plan = await AgentPlanner(db_session, provider, session_id="s-live").start(
        "tell me how many files are in phase3test folder and their names"
    )

    assert plan.status == PlanStatus.COMPLETED
    listed = [
        s for s in plan.steps
        if s.status == StepStatus.COMPLETED and s.tool == "list_directory"
    ]
    assert listed and listed[-1].result.output["count"] == 3
    # The revise prompts carry the never-stop-at-the-search rules
    assert "re-add a corrected version" in provider.prompts[2]
    assert "fully accomplish the USER GOAL" in provider.prompts[2]
    # And the deterministic completion text IS the answer
    text = deterministic_plan_text(plan)
    assert "3 file(s)" in text
    for name in ("firstname.txt", "fisrtname.tmp", "fstname.tmp"):
        assert name in text


async def test_path_guard_allows_paths_created_by_earlier_steps(db_session, tmp_path):
    """The guard checks just-in-time, step by step — a script that an earlier
    step creates must not be flagged when the plan is drafted."""
    script = tmp_path / "hello.py"
    steps = [
        step("Create hello.py", "create_file", path=str(script),
             content="print('hi from script')"),
        step("Run hello.py", "execute_script", script_path=str(script),
             interpreter=sys.executable),
    ]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("create and run hello.py")
    assert plan.status == PlanStatus.AWAITING_APPROVAL  # guard did NOT fail it

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert "hi from script" in plan.steps[1].result.output["stdout"]


async def test_conversation_context_reaches_every_planner_prompt(db_session, tmp_path):
    # A WRITE step keeps the reflect round alive (all-READ drafts skip it,
    # 2026-07-13) — this test must see the context in BOTH prompt kinds.
    steps = [step("Create a note", "create_file",
                  path=str(tmp_path / "note.txt"), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    convo = (
        f"user: where is my stuff?\nassistant: Your files are in {tmp_path}."
    )

    plan = await AgentPlanner(db_session, provider, conversation=convo).start(
        "save a note in that folder"
    )

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert len(provider.prompts) == 2  # plan + reflect
    for prompt in provider.prompts:
        assert "RECENT CONVERSATION" in prompt
        assert str(tmp_path) in prompt
        assert "never overrides the USER GOAL" in prompt  # context, not orders


def test_conversation_is_never_serialized_into_plan_responses():
    """The context rides along on the parked plan for replans, but it is
    planner input — it must not appear in API/SSE plan payloads."""
    plan = AgentPlan(goal="g", conversation="user: some private chat context")
    dumped = json.dumps(plan.model_dump(mode="json"), default=str)
    assert "private chat context" not in dumped


async def test_missing_parent_folder_fails_before_approval(db_session, tmp_path):
    """create_file into a guessed folder / move_file to a guessed destination
    can never succeed — both fail pre-flight, before any approval request."""
    src = tmp_path / "real.txt"
    src.write_text("x")
    bad_create = [step("Create notes", "create_file",
                       path=str(tmp_path / "no_such_dir" / "notes.txt"), content="hi")]
    bad_move = [step("Move real.txt", "move_file", source=str(src),
                     destination=str(tmp_path / "ghost_dir" / "real.txt"))]
    fixed = [step("Create notes", "create_file",
                  path=str(tmp_path / "notes.txt"), content="hi")]
    provider = FakeProvider([
        plan_json(bad_create), plan_json(bad_create),  # plan, reflect
        plan_json(bad_move),                            # replan 1 — also doomed
        plan_json(fixed),                               # replan 2 — good
    ])

    plan = await AgentPlanner(db_session, provider).start("save my notes")

    # Both doomed steps failed pre-flight; nothing ran, nothing was approved
    assert plan.steps[0].status == StepStatus.FAILED
    assert "does not exist" in plan.steps[0].result.error
    assert plan.steps[1].status == StepStatus.FAILED
    assert "ghost_dir" in plan.steps[1].result.error
    assert await _activity_rows(db_session) == []
    assert plan.status == PlanStatus.AWAITING_APPROVAL  # the fixed step
    assert plan.pending_steps()[0].parameters["path"] == str(tmp_path / "notes.txt")


async def test_move_into_existing_folder_passes_the_guard(db_session, tmp_path):
    """move_file's destination may be an existing folder (move into it) —
    the parent check must not flag that."""
    src = tmp_path / "doc.txt"
    src.write_text("content")
    dest = tmp_path / "archive"
    dest.mkdir()
    steps = [step("Move doc.txt into archive", "move_file",
                  source=str(src), destination=str(dest))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("archive my doc")
    assert plan.status == PlanStatus.AWAITING_APPROVAL  # guard did NOT fail it

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert (dest / "doc.txt").exists()


# ------------------------------------------------------ clarifying questions

def question_json(text: str, options: list) -> str:
    return plan_json([], question={"text": text, "options": options})


async def test_draft_question_pauses_without_executing(db_session):
    provider = FakeProvider([
        question_json("Did you mean March 4 or April 3?",
                      ["2026-03-04", "2026-04-03"]),
    ])
    plan = await AgentPlanner(db_session, provider).start(
        "delete files older than 03/04/2026"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.text.startswith("Did you mean")
    assert plan.question.options == ["2026-03-04", "2026-04-03"]
    assert plan.steps == []
    assert provider.calls == 1  # no reflect for a question pause
    assert await _activity_rows(db_session) == []
    # The question IS serialized (the UI renders it)…
    assert "March 4" in json.dumps(plan.model_dump(mode="json"), default=str)


async def test_answer_feeds_revise_and_completes(db_session, tmp_path):
    """The scenario 2 flow: search found several notes.txt, the planner asks,
    the user answers, the concrete read runs."""
    the_one = tmp_path / "docs_notes.txt"
    the_one.write_text("the real notes")
    other = tmp_path / "desktop_notes.txt"
    other.write_text("decoy")

    read_step = [step("Read the chosen file", "read_file", path=str(the_one))]
    provider = FakeProvider([
        question_json("Two files are named notes.txt — which one?",
                      [str(the_one), str(other)]),
        plan_json(read_step),  # revise after the answer
    ])
    planner = AgentPlanner(db_session, provider, session_id="s-choice")

    plan = await planner.start("read notes.txt")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    plan = await planner.answer(plan, str(the_one))
    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].result.output["content"] == "the real notes"
    assert plan.question is None
    # The revise prompt carried the user's answer, marked authoritative
    assert "THE USER'S ANSWER" in provider.prompts[1]
    assert str(the_one) in provider.prompts[1]


async def test_answer_leading_to_write_still_needs_approval(db_session, tmp_path):
    """Answering a question is NOT approval: a write step the answer produces
    pauses at the approval gate with a fresh signature."""
    victim = tmp_path / "old_notes.txt"
    victim.write_text("x")
    delete_step = [step("Delete old_notes.txt", "delete_file", path=str(victim))]
    provider = FakeProvider([
        question_json("Which file should I delete?", [str(victim)]),
        plan_json(delete_step),
    ])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("delete my old notes")
    plan = await planner.answer(plan, str(victim))

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert victim.exists()  # NOTHING ran
    assert await _activity_rows(db_session) == []

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED


async def test_cancel_works_on_a_question_and_approve_does_not(db_session):
    provider = FakeProvider([question_json("Which one?", ["a", "b"])])
    planner = AgentPlanner(db_session, provider)
    plan = await planner.start("read the file")

    # approve=True cannot answer a question — the plan stays paused
    same = await planner.resume(plan, approved=True)
    assert same.status == PlanStatus.AWAITING_CHOICE
    assert same.question is not None

    # Cancel works
    cancelled = await planner.resume(plan, approved=False)
    assert cancelled.status == PlanStatus.CANCELLED
    assert cancelled.question is None


async def test_question_budget_is_capped(db_session):
    """A model that keeps asking must fail honestly, not loop forever."""
    q = question_json("Which one?", ["a", "b"])
    provider = FakeProvider([q] * (MAX_QUESTIONS + 1))
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("do the thing")
    for i in range(MAX_QUESTIONS):
        assert plan.status == PlanStatus.AWAITING_CHOICE, f"round {i}"
        plan = await planner.answer(plan, "a")

    assert plan.status == PlanStatus.FAILED
    assert str(MAX_QUESTIONS) in plan.message


def test_answers_never_serialized_but_question_is():
    from app.agents.schemas import PlanQuestion

    plan = AgentPlan(
        goal="g",
        question=PlanQuestion(text="which file?", options=["a.txt"]),
        user_answers=["the private answer"],
    )
    dumped = json.dumps(plan.model_dump(mode="json"), default=str)
    assert "which file?" in dumped and "a.txt" in dumped
    assert "the private answer" not in dumped


# ----------------------------------- verified options + not-found recovery
# Live incident 2026-07-10 ("delete the files in phase3test folder that have
# .txt extension"): the draft asked a question offering two INVENTED paths,
# the user clicked one, the search inside it failed, and the replan gave up —
# then the user's correction ("its present in desktop") dead-ended in chat.
# These tests pin the structural fixes: options verified in code, a
# code-derived recovery instruction for not-found failures, and ask-not-fail
# instead of dead-ending.

def test_option_path_verification_only_applies_to_paths(tmp_path):
    from app.agents.planner import _option_is_dead_path

    assert _option_is_dead_path(str(tmp_path / "ghost")) is True   # invented
    assert _option_is_dead_path(str(tmp_path)) is False            # real path
    assert _option_is_dead_path("phase3test") is False             # bare name
    assert _option_is_dead_path("It's on my Desktop") is False     # plain text
    assert _option_is_dead_path("2026-03-04") is False             # a date


async def test_invented_path_options_reject_the_question_and_push_to_search(
    db_session, tmp_path
):
    """A question whose options are ALL invented paths never reaches the user:
    the retry feedback pushes the model to search, exactly like an invalid
    JSON output would be retried."""
    folder = tmp_path / "phase3test"
    folder.mkdir()
    search = [step("Find the phase3test folder", "search_files",
                   query="phase3test", directory=str(tmp_path),
                   include_folders=True)]
    provider = FakeProvider([
        question_json("What is the full path of the phase3test folder?",
                      [str(tmp_path / "wrong" / "phase3test"),
                       str(tmp_path / "also-wrong" / "phase3test")]),
        plan_json(search),  # retry after the structural rejection
    ])
    plan = await AgentPlanner(db_session, provider).start(
        "delete the txt files in phase3test"
    )

    # The fabricated question was never surfaced — the plan searched instead
    assert plan.status == PlanStatus.COMPLETED
    assert plan.question is None
    assert plan.steps[0].tool == "search_files"
    assert plan.steps[0].result.output["count"] == 1
    assert provider.calls == 2  # draft, structural-rejection retry; no reflect (all-READ)


async def test_question_drops_only_the_invented_path_options(db_session, tmp_path):
    """Mixed options: real candidates survive, invented ones are stripped."""
    real = tmp_path / "notes.txt"
    real.write_text("x")
    fake = str(tmp_path / "ghost" / "notes.txt")
    provider = FakeProvider([
        question_json("Which notes.txt?", [str(real), fake]),
    ])
    plan = await AgentPlanner(db_session, provider).start("read notes.txt")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.options == [str(real)]


async def test_persistently_invented_options_become_a_free_form_question(
    db_session, tmp_path
):
    """If the retry STILL fabricates paths, the question survives but the fake
    options do not — an honest free-form question beats invented buttons."""
    q = question_json("What is the full path?",
                      [str(tmp_path / "no1"), str(tmp_path / "no2")])
    provider = FakeProvider([q, q])
    plan = await AgentPlanner(db_session, provider).start("delete the thing")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.text == "What is the full path?"
    assert plan.question.options == []
    assert provider.calls == 2


async def test_missing_path_failure_injects_recovery_instruction(db_session, tmp_path):
    """A step that failed on a nonexistent path puts a code-derived recovery
    instruction (locate the target by name) into the revise prompt — recovery
    is never left to the model's judgment."""
    target = tmp_path / "phase3test"
    target.mkdir()
    (target / "firstname.txt").write_text("x")
    bad_dir = str(tmp_path / "wrong" / "phase3test")
    bad = [step("Search for txt files", "search_files",
                directory=bad_dir, file_type=".txt")]
    good = [step("Search for txt files", "search_files",
                 directory=str(target), file_type=".txt")]
    provider = FakeProvider([
        plan_json(bad), plan_json(bad),  # draft + reflect
        plan_json(good),                 # revise after the failure
    ])
    plan = await AgentPlanner(db_session, provider).start(
        "find the txt files in phase3test"
    )

    assert plan.status == PlanStatus.COMPLETED
    revise_prompt = provider.prompts[2]
    assert "SYSTEM RECOVERY INSTRUCTION" in revise_prompt
    assert 'query "phase3test"' in revise_prompt
    assert bad_dir in revise_prompt


async def test_not_found_dead_end_asks_where_instead_of_failing(db_session, tmp_path):
    """The replanner surrendering on a not-found target converts to a
    code-derived 'where is it?' question — and the user's answer flows back
    into the SAME plan (an open question owns the next message) instead of
    dead-ending in the chat path."""
    bad_dir = str(tmp_path / "missing" / "phase3test")
    bad = [step("Search", "search_files", directory=bad_dir, file_type=".txt")]
    target = tmp_path / "phase3test"
    target.mkdir()
    (target / "a.txt").write_text("x")
    good = [step("Search", "search_files", directory=str(target),
                 file_type=".txt")]
    provider = FakeProvider([
        plan_json(bad), plan_json(bad),                                # draft + reflect
        plan_json([], reason="The phase3test folder does not exist."),  # revise gives up
        plan_json(good),                                               # revise after the answer
    ])
    planner = AgentPlanner(db_session, provider, session_id="s-ask-not-fail")

    plan = await planner.start("find the txt files in phase3test")
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert "couldn't find 'phase3test'" in plan.question.text
    assert plan.question.options == []

    plan = await planner.answer(plan, f"its present in {tmp_path}")
    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[-1].result.output["count"] == 1


async def test_replan_cap_on_missing_path_asks_instead_of_failing(db_session, tmp_path):
    """Exhausting the replan budget on a not-found target also asks rather
    than failing — the user can still rescue the plan."""
    bad_dir = str(tmp_path / "missing" / "phase3test")
    bad = [step("Search", "search_files", directory=bad_dir, file_type=".txt")]
    provider = FakeProvider([plan_json(bad)] * (1 + MAX_REPLANS))

    plan = await AgentPlanner(db_session, provider).start(
        "find the txt files in phase3test"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert "couldn't find 'phase3test'" in plan.question.text
    assert provider.calls == 1 + MAX_REPLANS  # draft + replans; no reflect (all-READ)


# ----------------------------------------------------------------- plan store

async def test_plan_store_put_get_pop(db_session):
    plan = AgentPlan(goal="g", status=PlanStatus.AWAITING_APPROVAL)
    await plan_store.put_plan(db_session, plan)
    assert plan_store.get_plan(plan.id) is plan
    assert (await plan_store.pop_plan(db_session, plan.id)) is plan
    assert plan_store.get_plan(plan.id) is None
    assert (await plan_store.pop_plan(db_session, plan.id)) is None


async def test_plan_store_survives_cache_loss(db_session):
    """Phase 3.5: SQLite is the truth. A parked plan outlives the in-memory
    cache (restart / cache TTL) with its planner inputs intact, and popping
    it consumes the row — a second pop finds nothing."""
    plan = AgentPlan(
        goal="delete the draft", status=PlanStatus.AWAITING_APPROVAL,
        session_id="s-persist", conversation="user: the draft is on my desktop",
        memory_context="MEMORY: the user's drafts folder",
        user_answers=["the one from monday"], questions_asked=1,
    )
    await plan_store.put_plan(db_session, plan)
    plan_store._PENDING_PLANS.clear()  # simulate a backend restart
    assert plan_store.get_plan(plan.id) is None  # cache is gone

    restored = await plan_store.pop_plan(db_session, plan.id)
    assert restored is not None
    assert restored.goal == "delete the draft"
    assert restored.status == PlanStatus.AWAITING_APPROVAL
    # The excluded-from-API planner inputs made the round trip
    assert restored.conversation == "user: the draft is on my desktop"
    assert restored.memory_context == "MEMORY: the user's drafts folder"
    assert restored.user_answers == ["the one from monday"]
    assert restored.questions_asked == 1
    assert (await plan_store.pop_plan(db_session, plan.id)) is None  # consumed


async def test_plan_store_memory_cache_expiry_falls_back_to_db(db_session, monkeypatch):
    monkeypatch.setattr(plan_store, "PLAN_TTL_SECONDS", -1)
    plan = AgentPlan(goal="g", status=PlanStatus.AWAITING_APPROVAL)
    await plan_store.put_plan(db_session, plan)
    assert plan_store.get_plan(plan.id) is None  # cache peek misses
    restored = await plan_store.pop_plan(db_session, plan.id)  # DB still has it
    assert restored is not None and restored.goal == "g"


async def test_plan_store_db_expiry_is_final(db_session, monkeypatch):
    monkeypatch.setattr(plan_store, "PLAN_DB_TTL_SECONDS", -1)
    plan = AgentPlan(goal="g", status=PlanStatus.AWAITING_APPROVAL)
    await plan_store.put_plan(db_session, plan)
    plan_store._PENDING_PLANS.clear()
    assert (await plan_store.pop_plan(db_session, plan.id)) is None


# ---------------------------------------------------------- memory context

async def test_memory_context_reaches_every_planner_prompt(db_session, tmp_path):
    """Phase 3.5 "one brain": the rendered memory context is injected into the
    plan AND reflect prompts with data-never-instructions framing, and is
    stamped on the plan so post-approval replans keep it. A WRITE step keeps
    the reflect round alive (all-READ drafts skip it, 2026-07-13)."""
    steps = [step("Create a note", "create_file",
                  path=str(tmp_path / "note.txt"), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(
        db_session, provider, memory="Jamil Ali is the user's gym friend"
    )
    plan = await planner.start("save a note")

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert plan.memory_context == "Jamil Ali is the user's gym friend"
    assert len(provider.prompts) == 2  # draft + reflect
    for prompt in provider.prompts:
        assert "LONG-TERM MEMORY ABOUT THE USER" in prompt
        assert "Jamil Ali is the user's gym friend" in prompt
        assert "never overrides the USER GOAL" in prompt


async def test_no_memory_context_means_no_memory_block(db_session, tmp_path):
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider)
    plan = await planner.start("list my files")

    assert plan.status == PlanStatus.COMPLETED
    # rule 12 mentions the phrase — assert the BLOCK is absent, not the words
    assert all("LONG-TERM MEMORY ABOUT THE USER" not in p for p in provider.prompts)


async def test_choice_plan_lookup_survives_cache_loss(db_session):
    from app.agents.schemas import PlanQuestion

    plan = AgentPlan(
        goal="rename it", status=PlanStatus.AWAITING_CHOICE, session_id="s-choice",
        question=PlanQuestion(text="Which file?", options=["a.txt", "b.txt"]),
    )
    await plan_store.put_plan(db_session, plan)
    plan_store._PENDING_PLANS.clear()  # simulate a restart

    found = await plan_store.get_choice_plan_for_session(db_session, "s-choice")
    assert found is not None
    assert found.question is not None and found.question.text == "Which file?"
    assert (await plan_store.get_choice_plan_for_session(db_session, "s-other")) is None


# ------------------------- the 0-step "completed" hallucination (2026-07-13)
# Live incident: "find all the pdf files in downloads and tell me the name of
# the largest" — the draft asked "Which pdf file is the largest?" with the
# lone option "PENDING: largest pdf file path"; the click fed an empty
# revision, the plan COMPLETED with zero steps ever executed, and the summary
# LLM invented file1/2/3.pdf. Three structural gates now each stop it alone.

async def test_pending_placeholder_question_is_rejected_and_retried(
    db_session, tmp_path
):
    """A question carrying the planner's own 'PENDING:' syntax never reaches
    the user — the retry feedback pushes the model to return steps."""
    (tmp_path / "a.pdf").write_bytes(b"x" * 10)
    search = [step("Find the PDFs", "search_files", file_type=".pdf",
                   directory=str(tmp_path))]
    provider = FakeProvider([
        question_json("Which pdf file is the largest?",
                      ["PENDING: largest pdf file path"]),
        plan_json(search),  # retry after the structural rejection
    ])
    plan = await AgentPlanner(db_session, provider).start(
        "find all the pdf files and tell me the name of the largest"
    )

    assert plan.status == PlanStatus.COMPLETED
    assert plan.question is None
    assert plan.steps[0].tool == "search_files"
    assert plan.steps[0].status == StepStatus.COMPLETED
    # draft + rejection retry only; no reflect (all-READ). The retry feedback
    # wording is pinned by test_pending_in_question_text_is_rejected_too.
    assert provider.calls == 2


async def test_persistent_pending_question_fails_honestly(db_session):
    """A model that keeps asking the PENDING-question fails the plan instead
    of surfacing it — an honest failure beats a do-my-job-for-me question."""
    q = question_json("Which file is the largest?", ["PENDING: largest file"])
    provider = FakeProvider([q, q])
    plan = await AgentPlanner(db_session, provider).start("find the largest file")

    assert plan.status == PlanStatus.FAILED
    assert plan.question is None
    assert "Planning failed" in plan.message


def test_pending_in_question_text_is_rejected_too():
    from app.agents.schemas import QuestionDraft

    qdraft = QuestionDraft(
        text="Should I use PENDING: the found path?", options=["yes", "no"]
    )
    question, error = AgentPlanner._validated_question(qdraft, attempt=2)
    assert question is None
    assert "STEP PARAMETERS" in error


async def test_empty_revision_with_nothing_executed_is_rejected(
    db_session, tmp_path
):
    """An empty revision means 'the executed results already accomplish the
    goal' — with ZERO executed steps it is rejected like invalid JSON and the
    retry produces the real steps."""
    the_file = tmp_path / "notes.txt"
    the_file.write_text("the notes")
    read_step = [step("Read notes.txt", "read_file", path=str(the_file))]
    provider = FakeProvider([
        question_json("Which notes.txt?", [str(the_file)]),
        plan_json([]),        # post-answer revise returns EMPTY — rejected
        plan_json(read_step), # retry does the work
    ])
    planner = AgentPlanner(db_session, provider, session_id="s-empty-rev")

    plan = await planner.start("read notes.txt")
    assert plan.status == PlanStatus.AWAITING_CHOICE
    plan = await planner.answer(plan, str(the_file))

    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[0].result.output["content"] == "the notes"
    # draft-question + revise attempt 1 (empty, rejected) + retry = 3 calls
    assert provider.calls == 3


async def test_plan_never_completes_with_zero_executed_steps(db_session, tmp_path):
    """The hard backstop: a plan whose step list emptied out without ever
    executing anything FAILS honestly — never COMPLETED (the live incident's
    'Completed — 0 steps ran.')."""
    the_file = tmp_path / "notes.txt"
    the_file.write_text("x")
    provider = FakeProvider([
        question_json("Which notes.txt?", [str(the_file)]),
        plan_json([]),  # empty revision, attempt 1 — rejected
        plan_json([]),  # empty again, attempt 2 — revise output unusable
    ])
    planner = AgentPlanner(db_session, provider, session_id="s-zero-steps")

    plan = await planner.start("read notes.txt")
    plan = await planner.answer(plan, str(the_file))

    assert plan.status == PlanStatus.FAILED
    assert plan.status != PlanStatus.COMPLETED
    assert "nothing was done" in plan.message.lower() or "no step" in plan.message.lower()


async def test_summary_llm_never_sees_an_empty_record(db_session):
    """A completed plan with nothing rendered to report gets the
    deterministic text — the summary LLM is never invoked over an empty
    record it could fill with fiction."""
    from app.agents.summary import completed_plan_text

    streamed = {"calls": 0}

    class RecordingProvider(FakeProvider):
        # A stream exception would be swallowed by design (deterministic
        # fallback), so an exploding provider can't prove the guard — a
        # call COUNTER plus a poisoned payload can.
        async def stream_chat(self, messages, temperature=0.7, max_tokens=None):
            streamed["calls"] += 1
            yield "The largest PDF file is: file3.pdf"  # the hallucination

    plan = AgentPlan(goal="find the pdfs", status=PlanStatus.COMPLETED)
    plan.message = "No matching files were found — nothing to do."
    text = await completed_plan_text(RecordingProvider([]), plan)

    assert streamed["calls"] == 0  # the LLM was never asked
    assert "file3.pdf" not in text  # nothing invented
    assert "No matching files were found" in text


# ============================ domain-agent specialization (boss + agents model)

def test_tools_json_is_filtered_to_the_agent_subset():
    """A specialized planner shows the LLM only its agent's tools — the
    structural, zero-cost specialization (the model can't draft what it can't
    see). The email agent has send_email + shared reads, never delete_file."""
    from app.agents.planner import _tools_json
    from app.agents.agent_registry import agent_for_label

    email = agent_for_label("EMAIL")
    catalog = json.loads(_tools_json(email.tools))
    names = {t["name"] for t in catalog}
    assert names == set(email.tools)          # exactly the subset, nothing extra
    assert "send_email" in names
    assert "search_files" in names            # shared read — "tools not starved"
    assert "delete_file" not in names
    assert "browse_commit" not in names


def test_tools_json_unfiltered_is_the_full_registry():
    """The general agent (tools=None) sees every registered tool — the
    pre-agent behavior, so nothing regresses."""
    from app.agents.planner import _tools_json
    from app.tools.registry import registry

    names = {t["name"] for t in json.loads(_tools_json())}
    assert names == set(registry.names())


def test_plan_prompt_carries_the_agent_persona():
    from app.agents.planner import _build_plan_prompt
    from app.agents.agent_registry import agent_for_label

    browser = agent_for_label("BROWSE")
    prompt = _build_plan_prompt("x", tools=browser.tools, persona=browser.persona)
    assert "browser agent" in prompt.lower()

    # the general agent adds no persona header
    assert "browser agent" not in _build_plan_prompt("x").lower()


# ============================================ LLM transport failures (2026-07-26)
# Live incident: a machine-wide DNS outage killed junaidjamshed.com AND
# api.deepseek.com inside 25s. The step failed with a perfectly good code-authored
# reason ("Couldn't load www.junaidjamshed.com: it didn't respond in time."), the
# replan then died on DNS, and the user was told:
#     "Step '...' failed and replanning also failed: LLM call failed:
#      [Errno 11001] getaddrinfo failed"
# — the replan's own death overwrote the diagnosis, phrased as a dead end.


class RaisingProvider(FakeProvider):
    """FakeProvider that RAISES a scripted exception instead of answering, from
    the Nth call on — how a network outage mid-plan actually looks."""

    def __init__(self, responses: List[str], *, raise_from_call: int, exc: BaseException):
        super().__init__(responses)
        self._raise_from_call = raise_from_call
        self._exc = exc

    async def chat(self, messages, temperature: float = 0.7, max_tokens: Optional[int] = None):
        if self.calls + 1 >= self._raise_from_call:
            self.calls += 1
            raise self._exc
        return await super().chat(messages, temperature, max_tokens)


def _dns_outage() -> BaseException:
    """The incident's exact exception shape: an httpx transport error wrapping
    the OSError that carries WSAHOST_NOT_FOUND."""
    import httpx

    try:
        try:
            raise OSError(11001, "getaddrinfo failed")
        except OSError as inner:
            raise httpx.ConnectError("") from inner   # empty message, as it was live
    except BaseException as exc:
        return exc


async def test_replan_transport_failure_leads_with_the_step_reason(db_session, tmp_path):
    """THE INCIDENT, frozen. When the replan dies on a network error, the
    user-facing message must LEAD with the STEP's own reason — the one thing
    they can act on — not the replanner's errno."""
    folder = tmp_path / "ghost.txt"
    folder.mkdir()  # read_file on a directory: a non-recoverable failure
    doomed = [step("Read it", "read_file", path=str(folder))]
    provider = RaisingProvider(
        [plan_json(doomed)], raise_from_call=2, exc=_dns_outage()
    )

    plan = await AgentPlanner(db_session, provider).start("read ghost.txt")

    assert plan.status == PlanStatus.FAILED
    # the STEP's own diagnosis survives and comes first
    assert "is a directory" in plan.message
    assert plan.message.index("is a directory") < plan.message.index("Replanning also failed")
    # the network is named in plain language; the raw errno never reaches the user
    assert "network looks down" in plan.message
    assert "getaddrinfo" not in plan.message
    assert "11001" not in plan.message


async def test_replan_model_failure_still_reads_as_a_planning_failure(db_session, tmp_path):
    """The other class must stay distinguishable: when the MODEL failed (not the
    network), the message says so — a transport phrasing there would be a lie."""
    folder = tmp_path / "ghost.txt"
    folder.mkdir()
    doomed = [step("Read it", "read_file", path=str(folder))]
    provider = RaisingProvider(
        [plan_json(doomed)], raise_from_call=2, exc=ValueError("choices key missing")
    )

    plan = await AgentPlanner(db_session, provider).start("read ghost.txt")

    assert plan.status == PlanStatus.FAILED
    assert "is a directory" in plan.message          # still leads with the step
    assert "LLM call failed" in plan.message
    assert "ValueError" in plan.message               # never an empty reason
    assert "network looks down" not in plan.message


async def test_transport_failure_backs_off_but_a_model_failure_does_not(monkeypatch):
    """Measured in the incident: the two attempts landed 1.1s apart — the same
    failure twice, not a retry. A transport failure now waits; a model failure
    keeps the immediate retry it has always had (waiting buys nothing there)."""
    import asyncio as _asyncio

    from app.agents import planner as planner_mod

    slept: list = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", _record)

    async def _run(exc) -> list:
        slept.clear()
        provider = RaisingProvider([], raise_from_call=1, exc=exc)
        p = planner_mod.AgentPlanner(None, provider)
        steps, _reason, _q, error, _acc = await p._generate_steps(
            "prompt", allow_empty=False, goal="g"
        )
        assert steps is None and error
        assert provider.calls == 2      # still exactly two attempts, not a new budget
        return list(slept), error

    transport_sleeps, transport_error = await _run(_dns_outage())
    assert transport_sleeps == [planner_mod._LLM_RETRY_BACKOFF_SECONDS]
    assert transport_error == planner_mod._LLM_UNREACHABLE_MESSAGE

    model_sleeps, model_error = await _run(ValueError("bad json"))
    assert model_sleeps == []
    assert "LLM call failed" in model_error


def test_an_exception_with_no_message_still_logs_its_class():
    """Attempt 1 logged `Planner LLM call failed (attempt 1): ` — nothing after
    the colon — so half the failure was undiagnosable from the log."""
    from app.agents.planner import _exc_text

    assert _exc_text(ValueError("")) == "ValueError"
    assert _exc_text(ValueError("boom")) == "ValueError: boom"


def test_transport_classification_matrix():
    """A DNS/connect death is transport; a model or HTTP-status failure is not —
    a 400 must keep the legible body the 2026-07-24 round gave it."""
    import httpx

    from app.agents.planner import _is_transport_error

    assert _is_transport_error(_dns_outage())                       # wrapped OSError
    assert _is_transport_error(OSError(11001, "getaddrinfo failed"))
    assert _is_transport_error(httpx.ConnectTimeout("timed out"))
    assert _is_transport_error(RuntimeError("connection refused"))  # text marker
    assert not _is_transport_error(ValueError("choices key missing"))
    assert not _is_transport_error(RuntimeError("400 Bad Request: content risk"))
