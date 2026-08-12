"""
Bulk file operations (2026-07-29 incident).

THE INCIDENT, verbatim: "hey create a folder 'pddf2' on desktop and move all
the pdf files that are in the downloads in that folder". Furi created the
folder, searched, found 85 PDFs — and reported "Done — 2 step(s) completed"
having moved ZERO files.

The chain, reproduced by the tests below:
  1. plan rule 4 said ONE file per step, so the move became a per-file
     template with a PENDING placeholder;
  2. placeholder_resolver capped expansion at MAX_PLAN_STEPS - len(steps) + 1
     = 28, and 85 > 28, so it returned None;
  3. the step failed "still contain unresolved 'PENDING:' placeholders";
  4. the revise LLM saw 1200 chars of raw JSON marked "(truncated)" and asked
     whether to search again — about a search that had found everything;
  5. the answered revision was all-duplicates, was rejected, and the plan
     "kept as-is" then reported COMPLETED with a FAILED write step in it.

Bulk file work is now ONE move_files/delete_files step carrying the explicit
list, a bulk MUTATION defaults to the searched folder's own files, and a plan
holding an unrouted failure can no longer call itself done.
"""
import asyncio
from pathlib import Path

import pytest

import app.tools  # noqa: F401 — registers the real file tools
from app.agents import placeholder_resolver
from app.agents.placeholder_resolver import (
    BATCH_EXPAND_MAX,
    partition_by_depth,
    resolve,
)
from app.agents.planner import (
    _nonexistent_path_error,
    _step_action_detail,
    _unrouted_failure,
)
from app.agents.rendering import completed_results_text
from app.agents.schemas import AgentPlan, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.tools.file_tools import BATCH_MAX_FILES, SEARCH_MAX_RESULTS
from app.tools.registry import execute_tool, registry

DOWNLOADS = r"D:\Downloads"
DEST = r"C:\Users\DELL\Desktop\pddf2"
INCIDENT_GOAL = (
    "hey create a folder 'pddf2' on desktop and move all the pdf files "
    "that are in the downloads in that folder"
)


# ------------------------------------------------------------------ helpers

def search_step(paths: list[str], *, roots=(DOWNLOADS,), truncated=False) -> PlanStep:
    s = PlanStep(
        description="Search for all PDF files in D:\\Downloads",
        tool="search_files",
        parameters={"directory": DOWNLOADS, "file_type": "pdf"},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
        status=StepStatus.COMPLETED,
    )
    s.result = ToolResult(
        success=True,
        output={
            "matches": [{"path": p, "type": "file"} for p in paths],
            "count": len(paths),
            "truncated": truncated,
            "searched_in": list(roots),
        },
        permission_level=PermissionLevel.READ,
    )
    return s


def move_template(destination: str = DEST) -> PlanStep:
    return PlanStep(
        description="Move each PDF file from Downloads into pddf2",
        tool="move_file",
        parameters={
            "source": "PENDING: each PDF file path from search results",
            "destination": destination,
        },
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )


def move_files_template(destination: str = DEST, *, as_list: bool = False) -> PlanStep:
    """The PLURAL shape — what the planner actually drafts now that rule 4
    tells it to. Every bulk test used to drive `move_template` (singular)
    only, which is exactly why the 2026-07-30 partition gap was invisible."""
    pending = "PENDING: all PDF file paths from the search step"
    return PlanStep(
        description="Move all found PDF files into pddf2",
        tool="move_files",
        parameters={
            "sources": [pending] if as_list else pending,
            "destination": destination,
        },
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )


def plan_with(*steps: PlanStep, goal: str = INCIDENT_GOAL) -> AgentPlan:
    plan = AgentPlan(goal=goal)
    plan.steps = list(steps)
    return plan


def flat(n: int, root: str = DOWNLOADS) -> list[str]:
    return [f"{root}\\report-{i:03}.pdf" for i in range(n)]


# =========================================================== THE REGRESSION

def test_the_incident_85_pdfs_become_one_batch_step_not_a_failure():
    """Step 2 of the chain: 85 > 28 used to return None. It must now produce
    exactly ONE move_files step carrying every top-level path."""
    paths = flat(85)
    plan = plan_with(search_step(paths), move_template())

    out = resolve(plan, 1, max_new=28)

    assert out is not None, "the pool blew the plan-size cap and died again"
    assert len(out) == 1
    step = out[0]
    assert step.tool == "move_files"
    assert step.parameters["sources"] == paths
    assert step.parameters["destination"] == DEST


def test_the_batch_step_is_still_gated_by_the_approval_system():
    plan = plan_with(search_step(flat(85)), move_template())
    step = resolve(plan, 1, max_new=28)[0]
    # Level comes from the REGISTRY, never copied from the template.
    assert step.permission_level == registry.get("move_files").permission_level
    assert step.permission_level == PermissionLevel.WRITE
    assert step.requires_approval is True


def test_approval_binds_to_the_exact_file_list():
    """The whole list lives in `parameters`, so it is inside signature() —
    swapping a single path invalidates an approval given for the others."""
    plan = plan_with(search_step(flat(85)), move_template())
    step = resolve(plan, 1, max_new=28)[0]
    before = step.signature()

    step.parameters["sources"][7] = r"D:\Downloads\something-else.pdf"
    assert step.signature() != before


def test_the_approval_card_names_the_files_and_never_cuts_a_path():
    plan = plan_with(search_step(flat(85)), move_template())
    step = resolve(plan, 1, max_new=28)[0]
    detail = step.action_detail

    assert detail.startswith(f"move 85 file(s) → {DEST}")
    assert r"D:\Downloads\report-000.pdf" in detail
    assert "… and 65 more" in detail          # clipped BY ITEM
    for line in detail.splitlines()[1:]:
        assert line.startswith("  ")
        assert line.strip().endswith((".pdf", "more"))  # never mid-path


def test_the_description_carries_the_count_and_total_size():
    plan = plan_with(search_step(flat(85)), move_template())
    step = resolve(plan, 1, max_new=28)[0]
    assert "85 file(s)" in step.description
    assert DEST in step.description


@pytest.mark.asyncio
async def test_the_batch_step_cannot_run_without_approval(db_session):
    """The structural gate is untouched — a bulk move is a WRITE like any
    other, and execute_tool refuses it unapproved."""
    result = await execute_tool(
        "move_files", {"sources": [r"C:\x\a.pdf"], "destination": DEST},
        db_session, approved=False,
    )
    assert result.success is False
    assert "not approved" in (result.error or "").lower()


# ================================================= scope: top-level default

def test_nested_files_are_left_out_and_named():
    """The user chose: a bulk write acts on the folder's OWN files. One of the
    incident's 8 nested PDFs sat inside a source repo's src/Assets."""
    top = flat(20)
    nested = [
        r"D:\Downloads\Musanif-main\frontend\src\Assets\diagram.pdf",
        r"D:\Downloads\ilovepdf_converted\slides.pdf",
    ]
    plan = plan_with(search_step(top + nested), move_template())

    step = resolve(plan, 1, max_new=28)[0]

    assert step.parameters["sources"] == top
    for p in nested:
        assert p not in step.parameters["sources"]
    assert "2 more sit inside subfolders" in step.description
    assert "src\\Assets" in step.description or "Assets" in step.description


@pytest.mark.parametrize("as_list", [False, True])
def test_nested_files_are_left_out_when_the_planner_drafts_the_batch_tool(as_list):
    """THE 2026-07-30 REGRESSION. The mutation rules keyed on a hand-listed set
    of SINGULAR tool names, so a planner-drafted `move_files` skipped BOTH the
    top-level partition and the truncated-source refusal. Live, all 85 PDFs
    moved — 8 of them out of subfolders, one from inside a source repo's
    frontend/src/Assets. The guard applied to the shape the planner used before
    rule 4 and not to the one rule 4 asks for; every existing bulk test drove
    the singular shape, so nothing caught it."""
    top = flat(20)
    nested = [
        r"D:\Downloads\Musanif-main\frontend\src\Assets\diagram.pdf",
        r"D:\Downloads\ilovepdf_converted\slides.pdf",
    ]
    plan = plan_with(search_step(top + nested), move_files_template(as_list=as_list))

    out = resolve(plan, 1, max_new=28)

    assert out is not None and len(out) == 1
    step = out[0]
    assert step.tool == "move_files"
    assert step.parameters["sources"] == top
    for p in nested:
        assert p not in step.parameters["sources"]
    assert "2 more sit inside subfolders" in step.description


def test_the_batch_tool_also_refuses_a_truncated_source_search():
    """The other rule that switched off with it: acting on a knowingly partial
    set and reporting success is the defect this module exists to remove."""
    plan = plan_with(
        search_step(flat(30), truncated=True), move_files_template()
    )
    assert resolve(plan, 1, max_new=28) is None


def test_mutation_rules_are_read_from_the_registry_not_a_name_list():
    """The fix's actual shape: permission comes from the registry, so a new or
    renamed batch tool cannot silently fall outside the rules again."""
    from app.agents.placeholder_resolver import _mutates

    assert _mutates("move_file") and _mutates("move_files")
    assert _mutates("delete_file") and _mutates("delete_files")
    assert _mutates("rename_file")
    assert not _mutates("read_file")
    assert not _mutates("search_files")
    assert _mutates("a_tool_that_does_not_exist"), "unknown must fail safe"


def test_the_user_asking_for_subfolders_overrides_the_default():
    top, nested = flat(20), [r"D:\Downloads\sub\deep.pdf"]
    plan = plan_with(search_step(top + nested), move_template())

    step = resolve(plan, 1, max_new=28, grounding="yes include the subfolders too")[0]

    assert step.parameters["sources"] == top + nested
    assert "subfolders" not in step.description


def test_a_pool_that_is_entirely_nested_is_not_narrowed_to_nothing():
    """A search scoped so that everything found is nested was scoped that way
    on purpose — narrowing to zero is worse than the default. (Two files stay
    under BATCH_EXPAND_MAX, so this is the per-file shape; what matters is
    that BOTH survive.)"""
    nested = [r"D:\Downloads\a\1.pdf", r"D:\Downloads\b\2.pdf"]
    plan = plan_with(search_step(nested), move_template())
    out = resolve(plan, 1, max_new=28)
    assert [s.parameters["source"] for s in out] == nested


def test_a_large_all_nested_pool_still_batches_everything():
    nested = [f"D:\\Downloads\\sub\\f{i}.pdf" for i in range(20)]
    plan = plan_with(search_step(nested), move_template())
    step = resolve(plan, 1, max_new=28)[0]
    assert step.parameters["sources"] == nested
    assert "subfolders" not in step.description


def test_partition_by_depth_without_roots_calls_nothing_nested():
    pool = [r"D:\x\a.pdf", r"D:\x\y\b.pdf"]
    assert partition_by_depth(pool, []) == (pool, [])


def test_partition_by_depth_is_case_insensitive_on_windows_paths():
    top, nested = partition_by_depth(
        [r"D:\Downloads\a.pdf", r"D:\Downloads\sub\b.pdf"], [r"d:\downloads"]
    )
    assert top == [r"D:\Downloads\a.pdf"]
    assert nested == [r"D:\Downloads\sub\b.pdf"]


# ============================================= a partial set is never acted on

def test_a_truncated_search_refuses_to_become_a_bulk_write():
    """Acting on a knowingly-partial set and reporting success is the same
    defect in different clothes."""
    plan = plan_with(search_step(flat(100), truncated=True), move_template())
    assert resolve(plan, 1, max_new=28) is None


def test_a_truncated_search_also_refuses_the_per_file_expansion():
    """This was live BEFORE the batch tools existed: paths_from_step never
    looked at `truncated`, so a capped search expanded into per-file deletes
    over a subset and completed 'successfully'."""
    template = PlanStep(
        description="delete each",
        tool="delete_file",
        parameters={"path": "PENDING: each file"},
        permission_level=PermissionLevel.DESTRUCTIVE,
        requires_approval=True,
    )
    plan = plan_with(search_step(flat(3), truncated=True), template)
    assert resolve(plan, 1, max_new=28) is None


def test_a_batch_can_always_carry_a_whole_search_result():
    """The cap invariant: moving one number without the other would silently
    clip evidence at the boundary."""
    assert BATCH_MAX_FILES >= SEARCH_MAX_RESULTS


# ================================================== small sets stay per-file

def test_a_handful_of_files_still_expands_into_per_file_steps():
    paths = flat(BATCH_EXPAND_MAX)
    plan = plan_with(search_step(paths), move_template())
    out = resolve(plan, 1, max_new=28)
    assert len(out) == BATCH_EXPAND_MAX
    assert all(s.tool == "move_file" for s in out)


def test_one_more_than_the_threshold_batches():
    plan = plan_with(search_step(flat(BATCH_EXPAND_MAX + 1)), move_template())
    out = resolve(plan, 1, max_new=28)
    assert len(out) == 1 and out[0].tool == "move_files"


def test_zero_matches_still_skip_honestly_and_never_become_an_empty_batch():
    plan = plan_with(search_step([]), move_template())
    assert resolve(plan, 1, max_new=28) == []


# ====================================== the LLM drafting the batch tool itself

def test_a_pending_list_on_move_files_resolves_instead_of_failing():
    """`move_files(sources=["PENDING: ..."])` is invisible to the single-string
    rules and vetoed by _nested_placeholder — it used to fail with the exact
    incident error."""
    template = PlanStep(
        description="Move the PDFs",
        tool="move_files",
        parameters={"sources": ["PENDING: the pdf paths"], "destination": DEST},
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )
    plan = plan_with(search_step(flat(30)), template)

    out = resolve(plan, 1, max_new=28)

    assert out is not None and len(out) == 1
    assert out[0].parameters["sources"] == flat(30)


def test_a_list_mixing_real_paths_with_a_placeholder_stays_ambiguous():
    """The _nested_placeholder veto is bypassed only for a list that is ENTIRELY
    placeholder text; anything else still goes to the LLM."""
    template = PlanStep(
        description="Move some",
        tool="move_files",
        parameters={
            "sources": [r"D:\Downloads\real.pdf", "PENDING: the rest"],
            "destination": DEST,
        },
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )
    plan = plan_with(search_step(flat(30)), template)
    assert resolve(plan, 1, max_new=28) is None


# ===================================================== goal-fidelity survives

def test_an_invented_extension_cannot_narrow_a_universal_batch():
    """The 2026-07-10 rule, still true through the batch path: memory held
    '.txt' facts and the model narrowed 'all files'."""
    goal = "delete all files in phase3test"
    mixed = [r"D:\Downloads\a.txt", r"D:\Downloads\b.md", r"D:\Downloads\c.pdf"] * 4
    template = PlanStep(
        description="delete each",
        tool="delete_file",
        parameters={"path": "PENDING: .txt file paths"},
        permission_level=PermissionLevel.DESTRUCTIVE,
        requires_approval=True,
    )
    plan = plan_with(search_step(mixed), template, goal=goal)

    out = resolve(plan, 1, max_new=28)

    assert len(out) == 1 and out[0].tool == "delete_files"
    assert out[0].parameters["paths"] == mixed   # all 12, not the .txt subset


# ================================================ the pre-flight guard on lists

def test_the_path_guard_does_not_fail_a_batch_over_one_vanished_file(tmp_path):
    real = tmp_path / "a.pdf"
    real.write_text("x")
    error = _nonexistent_path_error("move_files", {
        "sources": [str(real), str(tmp_path / "gone.pdf")],
        "destination": str(tmp_path),
    })
    assert error is None


def test_the_path_guard_still_catches_a_wholly_invented_list(tmp_path):
    error = _nonexistent_path_error("move_files", {
        "sources": [str(tmp_path / "nope1.pdf"), str(tmp_path / "nope2.pdf")],
        "destination": str(tmp_path),
    })
    assert error is not None and "does not exist" in error


def test_the_path_guard_ignores_an_unresolved_placeholder_list(tmp_path):
    assert _nonexistent_path_error("move_files", {
        "sources": ["PENDING: the paths"], "destination": str(tmp_path),
    }) is None


# ============================================== the tools themselves, for real

@pytest.mark.asyncio
async def test_move_files_moves_everything_and_reports_both_halves(tmp_path, db_session):
    src, dest = tmp_path / "from", tmp_path / "to"
    src.mkdir()
    dest.mkdir()
    files = []
    for i in range(12):
        p = src / f"f{i}.pdf"
        p.write_text("x" * (i + 1))
        files.append(str(p))
    (dest / "f3.pdf").write_text("already here")   # forces ONE collision

    result = await execute_tool(
        "move_files", {"sources": files, "destination": str(dest)}, db_session, approved=True,
    )

    assert result.success is True
    out = result.output
    assert out["moved_count"] == 11
    assert out["failed_count"] == 1
    assert "f3.pdf" in out["failed"][0]["path"]
    assert "overwrite" in out["failed"][0]["error"].lower()
    # The 11 really moved, and the collision was left alone — never rolled back.
    assert not (src / "f0.pdf").exists() and (dest / "f0.pdf").exists()
    assert (src / "f3.pdf").read_text() == "x" * 4


@pytest.mark.asyncio
async def test_move_files_refuses_a_destination_that_is_not_a_folder(tmp_path, db_session):
    f = tmp_path / "notafolder.txt"
    f.write_text("x")
    result = await execute_tool(
        "move_files", {"sources": [str(f)], "destination": str(f)}, db_session, approved=True,
    )
    assert result.success is False
    assert "not a folder" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_move_files_reports_a_missing_destination_in_recoverable_words(tmp_path, db_session):
    """The wording must match planner._MISSING_PATH_ERROR_RE so the
    _missing_target recovery still engages."""
    result = await execute_tool(
        "move_files",
        {"sources": [str(tmp_path / "a.pdf")], "destination": str(tmp_path / "nope")},
        db_session, approved=True,
    )
    assert result.success is False
    assert "does not exist" in (result.error or "")


@pytest.mark.asyncio
async def test_delete_files_backs_every_file_up_to_the_trash(tmp_path, monkeypatch, db_session):
    from app.tools import file_tools

    trash = tmp_path / "trash"
    monkeypatch.setattr(file_tools, "TRASH_DIR", trash)

    victims = []
    for i in range(5):
        p = tmp_path / f"v{i}.tmp"
        p.write_text("bye")
        victims.append(str(p))

    result = await execute_tool("delete_files", {"paths": victims}, db_session, approved=True)

    assert result.success is True
    assert result.output["deleted_count"] == 5
    assert all(not Path(v).exists() for v in victims)
    assert len(list(trash.iterdir())) == 5      # recoverable by hand


@pytest.mark.asyncio
async def test_a_batch_where_nothing_succeeds_fails_but_keeps_the_record(tmp_path, db_session):
    result = await execute_tool(
        "delete_files",
        {"paths": [str(tmp_path / "ghost1"), str(tmp_path / "ghost2")]},
        db_session, approved=True,
    )
    assert result.success is False
    assert result.output is not None            # evidence survives the failure
    assert result.output["failed_count"] == 2


@pytest.mark.asyncio
async def test_a_batch_over_the_cap_is_refused_not_silently_truncated(db_session):
    result = await execute_tool(
        "delete_files",
        {"paths": [f"C:\\x\\{i}.tmp" for i in range(BATCH_MAX_FILES + 1)]},
        db_session, approved=True,
    )
    assert result.success is False
    assert str(BATCH_MAX_FILES) in (result.error or "")


@pytest.mark.asyncio
async def test_a_blocked_path_fails_only_itself(tmp_path, db_session):
    from app.tools import file_tools

    ok = tmp_path / "ok.tmp"
    ok.write_text("x")
    blocked = str(file_tools._PROTECTED[0] / "x.tmp") if file_tools._PROTECTED else "C:\\"

    result = await execute_tool(
        "delete_files", {"paths": [str(ok), blocked]}, db_session, approved=True,
    )
    assert result.output["deleted_count"] == 1
    assert result.output["failed_count"] == 1


# ============================================ a failed write is never "done"

def _failed(step: PlanStep, error: str) -> PlanStep:
    step.status = StepStatus.FAILED
    step.result = ToolResult(
        success=False, output=None, error=error,
        permission_level=step.permission_level,
    )
    return step


def test_an_unrouted_failure_is_detected():
    """Exactly the incident's end state: search + mkdir completed, the move
    failed, nothing ran after it."""
    move = _failed(
        move_template(),
        "Step parameters still contain unresolved 'PENDING:' placeholders",
    )
    plan = plan_with(search_step(flat(85)), move)
    assert _unrouted_failure(plan) is move


def test_a_failure_a_later_step_routed_around_is_not_unrouted():
    """CLAUDE.md's protected case: a failed step the replan worked around."""
    failed = _failed(move_template(), "nope")
    later = search_step(flat(2))            # COMPLETED, and AFTER the failure
    plan = plan_with(failed, later)
    assert _unrouted_failure(plan) is None


def test_an_opportunistic_evidence_read_never_fails_the_plan():
    """evidence_resolver splices these and continues past their failure on
    purpose — the goal never depended on them."""
    extra = _failed(
        PlanStep(
            description="read the top result",
            tool="read_webpage",
            parameters={"url": "https://example.com"},
            permission_level=PermissionLevel.READ,
            requires_approval=False,
        ),
        "403",
    )
    extra.auto_escalated = True
    plan = plan_with(search_step(flat(2)), extra)
    assert _unrouted_failure(plan) is None


def test_an_accomplished_verdict_overrides_the_positional_test():
    plan = plan_with(search_step(flat(2)), _failed(move_template(), "nope"))
    plan.goal_accomplished = True
    assert _unrouted_failure(plan) is None


# ================================================== the outcome text is honest

@pytest.mark.asyncio
async def test_the_whole_incident_end_to_end_through_the_real_planner(
    db_session, tmp_path
):
    """The full replay on a real filesystem: create the folder, find 40 PDFs
    (8 of them nested), pause ONCE for approval naming the real files, approve,
    and move all 32 top-level ones — leaving the nested ones where they are.

    Before this change the same plan reported "Done — 2 step(s) completed" and
    moved nothing."""
    import json as _json
    from tests.test_placeholder_resolver import RecordingProvider

    downloads = tmp_path / "Downloads"
    (downloads / "project" / "src").mkdir(parents=True)
    dest = tmp_path / "Desktop" / "pddf2"
    dest.parent.mkdir(parents=True)

    top = []
    for i in range(32):
        p = downloads / f"doc-{i:02}.pdf"
        p.write_text("x" * (i + 1))
        top.append(p)
    nested = []
    for i in range(8):
        p = downloads / "project" / "src" / f"asset-{i}.pdf"
        p.write_text("keep me")
        nested.append(p)

    def draft(steps):
        return _json.dumps(
            {"steps": steps, "unachievable_reason": None, "question": None}
        )

    steps = [
        {"description": "Find every PDF in Downloads", "tool": "search_files",
         "parameters": {"directory": str(downloads), "file_type": "pdf"}},
        {"description": "Create the pddf2 folder", "tool": "create_folder",
         "parameters": {"path": str(dest)}},
        {"description": "Move each PDF into pddf2", "tool": "move_file",
         "parameters": {"source": "PENDING: each PDF found",
                        "destination": str(dest)}},
    ]
    # draft + reflect + the pre-approval refine round (which has nothing left
    # to refine and is allowed to fail harmlessly)
    provider = RecordingProvider([draft(steps), draft(steps)])

    from app.agents.planner import AgentPlanner
    from app.agents.schemas import PlanStatus

    planner = AgentPlanner(db_session, provider, session_id="s-bulk")
    plan = await planner.start(
        "create a folder pddf2 on desktop and move all the pdf files in "
        f"{downloads} into it"
    )

    # Pause 1 — the folder. The move is still a template here: placeholders
    # resolve just-in-time, when their step is next in line.
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    plan = await planner.resume(plan, approved=True)

    # Pause 2 — the move, now a single batch step naming the REAL files. It
    # pauses again on purpose: resolving the placeholder minted a fresh
    # signature, so the earlier approval cannot cover it.
    assert plan.status == PlanStatus.AWAITING_APPROVAL, plan.message
    move = next(
        s for s in plan.pending_steps() if s.tool in ("move_file", "move_files")
    )
    assert move.tool == "move_files", "bulk work must be a single batch step"
    assert len(move.parameters["sources"]) == 32
    assert str(top[0]) in move.action_detail
    assert dest.is_dir(), "the folder step really ran"

    plan = await planner.resume(plan, approved=True)

    assert plan.status == PlanStatus.COMPLETED, plan.message
    assert all(not p.exists() for p in top), "the top-level PDFs did not move"
    assert all((dest / p.name).exists() for p in top)
    assert all(p.exists() for p in nested), "nested PDFs must be left alone"


def test_the_completion_text_reports_both_halves_of_a_partial_batch():
    step = PlanStep(
        description="Move 3 file(s)",
        tool="move_files",
        parameters={"sources": ["a", "b", "c"], "destination": DEST},
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
        status=StepStatus.COMPLETED,
    )
    step.result = ToolResult(
        success=True,
        output={
            "moved_count": 2, "failed_count": 1, "destination": DEST,
            "total_bytes": 2048,
            "moved": [
                {"moved_from": r"D:\a.pdf", "moved_to": DEST + r"\a.pdf"},
                {"moved_from": r"D:\b.pdf", "moved_to": DEST + r"\b.pdf"},
            ],
            "failed": [{"path": r"D:\c.pdf", "error": "Refusing to overwrite"}],
        },
        permission_level=PermissionLevel.WRITE,
    )
    plan = plan_with(step)
    from app.agents.schemas import PlanStatus
    plan.status = PlanStatus.COMPLETED

    text = completed_results_text(plan)

    assert "Moved 2 file(s)" in text
    assert "1 could NOT be done" in text
    assert "c.pdf" in text and "Refusing to overwrite" in text
