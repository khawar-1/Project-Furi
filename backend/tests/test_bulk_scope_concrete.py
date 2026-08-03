"""
The bulk-mutation scope rules, for a file list the MODEL wrote itself.

FOUND BY scripts/plan_bench.py, 2026-08-03, and REPRODUCED 5/5 runs — not
flaky. "move all the pdf files in downloads into pdfs" moved ALL 14 matches,
including two inside `downloads/project-src/assets`, with no top-level
partition, no truncated-source refusal, and no "excluded N nested files" note
on the approval card.

ROOT CAUSE: the two rules lived inside `placeholder_resolver._file_pool`,
which only `resolve()` calls — i.e. only while the step still carries a
`PENDING:` placeholder. A revise/refine round that fills in the CONCRETE list
reaches execution with no placeholder at all (the live logs show
`_revise_node … (refine)` and never "Placeholder resolved in code"), so
nothing ever consulted them.

THIRD INSTANCE OF ONE DEFECT CLASS: 2026-07-30 the rules keyed on the SINGULAR
tool names while rule 4 had just started steering the planner to the plural
ones; 2026-08-01 folder_resolver's `_DIR_KEY` covered reads but not writes.
And every test in test_bulk_file_ops.py drives a PLACEHOLDER, which is exactly
why 37 green bulk tests could not see this one.
"""
import json

import pytest

import app.tools  # noqa: F401 — registers the real file tools
from app.agents import placeholder_resolver
from app.agents.planner import _step_action_detail
from app.agents.schemas import AgentPlan, PlanStep, PlanStatus
from app.core.base_tool import PermissionLevel
from app.tools.registry import mutates, registry
from tests.test_bulk_file_ops import DEST, DOWNLOADS, flat, move_files_template, search_step

GOAL = "move all the pdf files in downloads into pdfs"
NESTED = [
    DOWNLOADS + r"\project-src\assets\vendored.pdf",
    DOWNLOADS + r"\project-src\assets\logo.pdf",
]


def plan_with(*steps: PlanStep, goal: str = GOAL) -> AgentPlan:
    plan = AgentPlan(goal=goal)
    plan.steps = list(steps)
    return plan


def concrete_move(paths: list[str], destination: str = DEST) -> PlanStep:
    """What a refine round leaves behind: move_files carrying real paths."""
    params = {"sources": list(paths), "destination": destination}
    return PlanStep(
        description=f"Move {len(paths)} file(s) into {destination}",
        tool="move_files",
        parameters=params,
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
        action_detail=_step_action_detail("move_files", params),
    )


# =============================================================== THE INCIDENT

def test_the_incident_a_concrete_list_is_still_scoped_to_top_level():
    top = flat(12)
    plan = plan_with(search_step(top + NESTED), concrete_move(top + NESTED))

    scope = placeholder_resolver.scope_concrete_list(plan, 1)

    assert scope is not None, "a model-written list skipped the scope rules"
    assert scope.kept == top
    assert scope.deferred == NESTED
    assert scope.refuse == ""


def test_a_concrete_list_built_from_a_truncated_search_is_refused():
    """Acting on a knowingly partial set and reporting success must not depend
    on HOW the list got written."""
    paths = flat(12)
    plan = plan_with(search_step(paths, truncated=True), concrete_move(paths))

    scope = placeholder_resolver.scope_concrete_list(plan, 1)

    assert scope is not None and scope.refuse
    assert "only part" in scope.refuse


# ================================================== idempotence and the card

def test_scoping_a_settled_list_is_a_no_op_so_the_exclusion_note_survives():
    """`_execute_node` runs this on EVERY pass. By the second pass the nested
    files are out of the list, so a re-stamp could no longer derive them —
    and would silently delete the sentence telling the user what was left
    out."""
    top = flat(12)
    plan = plan_with(search_step(top + NESTED), concrete_move(top + NESTED))
    step = plan.steps[1]

    placeholder_resolver.apply_list_scope(
        step, placeholder_resolver.scope_concrete_list(plan, 1)
    )
    described = step.description
    assert "subfolders" in described

    assert placeholder_resolver.scope_concrete_list(plan, 1) is None
    assert step.description == described


def test_the_scoped_step_refreshes_its_whole_contract():
    """List, description and action_detail move together — a card naming files
    the step will not touch is the 2026-08-01 defect."""
    top = flat(3)
    plan = plan_with(search_step(top + NESTED), concrete_move(top + NESTED))
    step = plan.steps[1]

    placeholder_resolver.apply_list_scope(
        step, placeholder_resolver.scope_concrete_list(plan, 1)
    )

    assert step.parameters["sources"] == top
    assert "vendored.pdf" not in step.action_detail
    assert "vendored.pdf" not in step.description
    assert "2 more sit inside subfolders" in step.description
    # …and the list stays LAST, so planner._missing_target still finds
    # `destination` rather than the first of N sources.
    assert list(step.parameters) == ["destination", "sources"]


def test_the_destructive_twin_is_scoped_the_same_way():
    """delete_files, not just move_files — the rules read the REGISTRY rather
    than a tool name, and a fix fitted to one tool is how this defect class
    reached its third instance."""
    top = flat(12)
    plan = plan_with(
        search_step(top + NESTED),
        PlanStep(
            description=f"Delete {len(top + NESTED)} file(s)",
            tool="delete_files",
            parameters={"paths": top + NESTED},
            permission_level=PermissionLevel.DESTRUCTIVE,
            requires_approval=True,
        ),
        goal="delete all the pdf files in downloads",
    )
    step = plan.steps[1]

    scope = placeholder_resolver.scope_concrete_list(plan, 1)

    assert scope is not None and scope.kept == top and scope.deferred == NESTED
    placeholder_resolver.apply_list_scope(step, scope)
    assert step.parameters["paths"] == top
    assert "vendored.pdf" not in step.action_detail
    assert "2 more sit inside subfolders" in step.description


# ================================================== the cases it must NOT touch

def test_the_users_own_words_still_override_the_top_level_default():
    top = flat(3)
    plan = plan_with(
        search_step(top + NESTED),
        concrete_move(top + NESTED),
        goal="move all the pdf files in downloads including subfolders into pdfs",
    )
    assert placeholder_resolver.scope_concrete_list(plan, 1) is None


def test_a_hand_named_list_with_no_search_behind_it_is_untouched():
    """No completed search ⇒ no searched roots ⇒ nothing is 'nested'. A user
    who names files explicitly must not have them silently dropped."""
    plan = plan_with(
        concrete_move([DOWNLOADS + r"\a.pdf", DOWNLOADS + r"\sub\b.pdf"]),
        goal="move a.pdf and sub/b.pdf into pdfs",
    )
    assert placeholder_resolver.scope_concrete_list(plan, 0) is None


def test_an_all_nested_list_is_kept_rather_than_narrowed_to_nothing():
    """A search that returned only nested files was scoped that way on
    purpose — the documented partition_by_depth rule."""
    plan = plan_with(search_step(NESTED), concrete_move(NESTED))
    assert placeholder_resolver.scope_concrete_list(plan, 1) is None


def test_a_list_still_holding_a_placeholder_is_left_to_resolve():
    plan = plan_with(search_step(flat(3)), move_files_template(as_list=True))
    assert placeholder_resolver.scope_concrete_list(plan, 1) is None


def test_a_read_tools_string_list_is_never_scoped():
    step = PlanStep(
        description="search the web",
        tool="web_search",
        parameters={"queries": ["a", "b"]},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )
    assert placeholder_resolver.scope_concrete_list(plan_with(step), 0) is None


def test_scoping_never_raises_on_a_malformed_step():
    step = PlanStep(
        description="broken",
        tool="move_files",
        parameters={"sources": [None, 3], "destination": DEST},
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )
    assert placeholder_resolver.scope_concrete_list(plan_with(step), 0) is None


# ======================================================= the coverage invariant

def test_every_bulk_list_param_is_covered_or_exempt():
    """Walk the REGISTRY: a mutating tool taking a list of strings is either a
    bulk file list the scope rules know, or written down as exempt. Same shape
    as folder_resolver's coverage test, which found `read_file.path` on its
    first run — a new batch tool must be a decision, never a silent hole."""
    for definition in registry.definitions():
        props = (definition.parameters or {}).get("properties") or {}
        for name, spec in props.items():
            if not isinstance(spec, dict) or spec.get("type") != "array":
                continue
            if (spec.get("items") or {}).get("type") != "string":
                continue
            if not mutates(definition.name):
                continue
            covered = placeholder_resolver._LIST_PARAMS.get(definition.name) == name
            exempt = (
                definition.name,
                name,
            ) in placeholder_resolver._EXEMPT_LIST_PARAMS
            assert covered or exempt, (
                f"{definition.name}.{name} is a mutating list parameter that "
                f"neither _LIST_PARAMS nor _EXEMPT_LIST_PARAMS mentions"
            )


# =============================================================== end to end

@pytest.mark.asyncio
async def test_a_model_written_list_end_to_end_through_the_real_planner(
    db_session, tmp_path
):
    """The plan_bench case on a real filesystem: the planner drafts the
    CONCRETE list — no placeholder anywhere — and the two nested PDFs are
    still left where they are, with the approval card saying so."""
    from tests.test_placeholder_resolver import RecordingProvider
    from app.agents.planner import AgentPlanner

    downloads = tmp_path / "downloads"
    (downloads / "project-src" / "assets").mkdir(parents=True)
    dest = tmp_path / "pdfs"
    dest.mkdir()

    top = []
    for i in range(12):
        p = downloads / f"a{i}.pdf"
        p.write_text("x")
        top.append(p)
    nested = []
    for name in ("vendored.pdf", "logo.pdf"):
        p = downloads / "project-src" / "assets" / name
        p.write_text("keep me")
        nested.append(p)

    def draft(steps):
        return json.dumps(
            {"steps": steps, "unachievable_reason": None, "question": None}
        )

    steps = [
        {
            "description": "Find every PDF in downloads",
            "tool": "search_files",
            "parameters": {"directory": str(downloads), "file_type": "pdf"},
        },
        # The refine round's end state: every path spelled out, the nested
        # ones included, exactly as the live run produced.
        {
            "description": "Move the PDFs into pdfs",
            "tool": "move_files",
            "parameters": {
                "sources": [str(p) for p in top + nested],
                "destination": str(dest),
            },
        },
    ]
    provider = RecordingProvider([draft(steps), draft(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-concrete")
    plan = await planner.start(f"move all the pdf files in {downloads} into {dest}")

    assert plan.status == PlanStatus.AWAITING_APPROVAL, plan.message
    move = next(s for s in plan.pending_steps() if s.tool == "move_files")
    assert len(move.parameters["sources"]) == 12, "the nested PDFs were not dropped"
    assert "vendored.pdf" not in move.action_detail
    assert "subfolders" in move.description, "the card never said what it left out"

    plan = await planner.resume(plan, approved=True)

    assert plan.status == PlanStatus.COMPLETED, plan.message
    assert all(not p.exists() for p in top)
    assert all((dest / p.name).exists() for p in top)
    assert all(p.exists() for p in nested), "nested PDFs must be left alone"
