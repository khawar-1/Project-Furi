"""
Deterministic plan rendering — the completion text carries the results.

Post-Phase-4 live-bug fix (2026-07-09): a background task for "how many
files are in phase3test folder and their names" finished as
"Done — 1 step(s) completed." with the answer nowhere — background
completions never get an LLM summary, so the deterministic text is the
only channel the answer can arrive by. completed_results_text renders
each completed step's REAL output in code (per-tool formatters matching
the tools' own result shapes); no LLM ever runs in the background runner.
"""
from app.agents.rendering import (
    completed_results_text,
    deterministic_plan_text,
    steps_for_summary,
)
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult


def done_step(
    tool: str,
    output,
    description: str = "a step",
    permission: PermissionLevel = PermissionLevel.READ,
    status: StepStatus = StepStatus.COMPLETED,
) -> PlanStep:
    return PlanStep(
        description=description,
        tool=tool,
        parameters={},
        permission_level=permission,
        requires_approval=permission != PermissionLevel.READ,
        status=status,
        result=ToolResult(
            success=status == StepStatus.COMPLETED,
            output=output,
            error=None if status == StepStatus.COMPLETED else "boom",
            permission_level=permission,
        ),
    )


def completed_plan(*steps: PlanStep, message=None) -> AgentPlan:
    return AgentPlan(
        goal="g", steps=list(steps), status=PlanStatus.COMPLETED, message=message
    )


# ------------------------------------------------------- the live regression

def test_list_directory_answer_is_in_the_completion_text():
    """The exact live payload shape: the user must get the count AND the
    names, not 'Done — 1 step(s) completed.'"""
    plan = completed_plan(done_step("list_directory", {
        "path": "C:\\Users\\DELL\\Desktop\\phase3test",
        "entries": [
            {"name": "firstname.txt", "type": "file"},
            {"name": "fisrtname.tmp", "type": "file"},
            {"name": "fstname.tmp", "type": "file"},
        ],
        "count": 3,
        "truncated": False,
    }))
    text = deterministic_plan_text(plan)
    assert "Done — 1 step(s) completed." in text
    assert "3 file(s)" in text
    assert "phase3test" in text
    for name in ("firstname.txt", "fisrtname.tmp", "fstname.tmp"):
        assert name in text


def test_list_directory_folders_and_empty():
    folders = completed_results_text(completed_plan(done_step("list_directory", {
        "path": "C:\\x",
        "entries": [
            {"name": "a.txt", "type": "file"},
            {"name": "sub", "type": "directory"},
        ],
        "count": 2,
    })))
    assert "1 folder(s) and 1 file(s)" in folders
    assert "- Folders: sub" in folders
    assert "- Files: a.txt" in folders

    empty = completed_results_text(completed_plan(
        done_step("list_directory", {"path": "C:\\x", "entries": [], "count": 0})
    ))
    assert "`C:\\x` is empty." in empty


def test_search_results_rendered_grouped_by_folder():
    plan = completed_plan(done_step("search_files", {
        "matches": [
            {"path": "C:\\a\\one.tmp", "type": "file"},
            {"path": "C:\\b\\two.tmp", "type": "file"},
            {"path": "C:\\b\\proj", "type": "folder"},
        ],
        "count": 3,
        "truncated": False,
    }))
    text = completed_results_text(plan)
    assert "Found 3 match(es)" in text
    assert "- In `C:\\a`: one.tmp" in text
    assert "- In `C:\\b`: two.tmp, proj (folder)" in text
    assert "truncated" not in text  # complete results are never called truncated

    none = completed_results_text(completed_plan(
        done_step("search_files", {"matches": [], "count": 0})
    ))
    assert "No matches found." in none


def test_read_file_content_and_command_stdout_shown():
    text = completed_results_text(completed_plan(
        done_step("read_file", {"path": "C:\\n.txt", "content": "the real notes"}),
        done_step(
            "run_command",
            {"stdout": "42 files", "stderr": "", "exit_code": 0},
            permission=PermissionLevel.DESTRUCTIVE,
        ),
    ))
    assert "Contents of `C:\\n.txt`:" in text
    assert "```\nthe real notes\n```" in text   # fenced — never parsed as markup
    assert "```\n42 files\n```" in text


def test_every_search_match_is_rendered_not_the_first_dozen():
    """The 2026-07-10 live bug: a 52-match D:\\Downloads search (30 audio
    files walking first, then the PDFs/exe) reached the user as ~11 matches
    of escaped JSON. Every name must survive rendering, PDFs included."""
    matches = [
        {"path": f"D:\\Downloads\\audio{i}.wav", "type": "file"} for i in range(30)
    ] + [
        {"path": "D:\\Downloads\\khawar-resume.pdf", "type": "file"},
        {"path": "D:\\Downloads\\module10.pdf", "type": "file"},
        {"path": "D:\\Downloads\\Claude Setup.exe", "type": "file"},
        {"path": "D:\\Downloads\\F25-116-D-LexConnect-code", "type": "folder"},
    ]
    plan = completed_plan(done_step("search_files", {
        "matches": matches, "count": len(matches), "truncated": False,
    }))
    text = completed_results_text(plan)
    assert "Found 34 match(es)" in text
    for name in ("khawar-resume.pdf", "module10.pdf", "Claude Setup.exe",
                 "F25-116-D-LexConnect-code (folder)", "audio29.wav"):
        assert name in text
    assert "truncated" not in text
    # Grouped: the parent path appears once as a heading, not per file
    assert text.count("D:\\Downloads\\") <= 1


def test_summary_llm_input_is_readable_text_never_json():
    """The inline summary LLM can only re-present what it is given — so it
    is given the code-rendered text, never json.dumps of the tool output
    (it used to paste escaped JSON into the chat, live bug 2026-07-10)."""
    plan = completed_plan(done_step("search_files", {
        "matches": [
            {"path": "D:\\Downloads\\a.pdf", "type": "file",
             "size_bytes": 90210, "created": "2026-06-11T15:06:00",
             "modified": "2026-06-11T15:06:00"},
        ],
        "count": 1,
        "truncated": False,
    }, description="Search Downloads for recent files"))
    text = steps_for_summary(plan)
    assert "ACTION: Search Downloads for recent files" in text
    assert "RESULT:" in text
    assert "a.pdf" in text
    assert "{" not in text and "}" not in text
    assert "size_bytes" not in text


def test_memory_tool_results_rendered():
    text = completed_results_text(completed_plan(
        done_step("recall_memory", {
            "memories": [{"content": "Loves fishing"}],
            "episodes": [{"title": "Trip", "summary": "Went to the lake"}],
            "count": 2,
        }),
        done_step("lookup_contact", {
            "status": "resolved",
            "contact": {"name": "Jamil Ali", "relationship": "friend"},
        }),
    ))
    assert "Loves fishing" in text
    assert "Trip — Went to the lake" in text
    assert "Contact found: Jamil Ali (friend)" in text


def test_write_steps_render_their_approved_description():
    """Write/destructive outputs are bookkeeping — the user-approved
    description is the record of what happened."""
    text = completed_results_text(completed_plan(done_step(
        "delete_file",
        {"backed_up_to": "C:\\trash\\a.tmp"},
        description="Delete junk1.tmp from the Desktop",
        permission=PermissionLevel.DESTRUCTIVE,
    )))
    assert "Done: Delete junk1.tmp from the Desktop" in text
    assert "backed_up_to" not in text


def test_failed_and_pending_steps_render_nothing():
    failed = done_step(
        "list_directory", None, status=StepStatus.FAILED
    )
    pending = PlanStep(
        description="later", tool="read_file", parameters={},
        permission_level=PermissionLevel.READ, requires_approval=False,
    )
    plan = completed_plan(failed, pending)
    assert completed_results_text(plan) == ""
    # No results → the base line alone, no trailing blank block
    assert deterministic_plan_text(plan) == "Done — 0 step(s) completed."


def test_plan_message_is_kept_and_results_appended():
    plan = completed_plan(
        done_step("search_files", {"matches": [], "count": 0}),
        message="No .tmp files were found",
    )
    text = deterministic_plan_text(plan)
    assert text.startswith("No .tmp files were found")
    assert "No matches found." in text


def test_results_block_is_capped():
    """Dozens of steps with big outputs must not produce an unbounded toast/
    chat message — the block stays bounded and says where the rest lives. The
    budget is sized so a real 'show me the files' answer (~100 names) always
    fits — only genuinely huge multi-step dumps are cut.

    Bound raised with _RESULTS_TOTAL_CAP (8000 → 20000, 2026-07-16) when web
    evidence got its own per-tool budget. The block is bounded by n × each
    step's fair share, never by arrival order."""
    steps = [
        done_step("read_file", {"path": f"C:\\f{i}.txt", "content": "x" * 3000})
        for i in range(20)
    ]
    text = completed_results_text(completed_plan(*steps))
    assert len(text) < 22_000
    assert "Activity timeline" in text


def test_every_step_is_represented_never_dropped_by_position():
    """Fair-share allocation: with many big steps, the LAST step's results are
    still present. The old accumulate-and-break dropped whichever steps came
    last — live bug 2026-07-16: a three-question turn drafted three web
    searches and the FIFA question was third, so the moment evidence got bulky
    the third question's record would vanish entirely and the summary LLM was
    handed nothing about the very thing it had to answer."""
    steps = [
        done_step("read_file", {"path": f"C:\\f{i}.txt", "content": f"MARKER{i} " + "x" * 3000})
        for i in range(20)
    ]
    text = completed_results_text(completed_plan(*steps))
    # Every step, including the last, appears — none omitted for being late.
    for i in range(20):
        assert f"MARKER{i}" in text, f"step {i} was dropped from the record"
    assert "(further step results omitted)" not in text


def test_small_steps_are_never_clipped_and_need_no_pointer():
    """The common case is untouched: a handful of modest steps renders in full
    with no truncation marker and no timeline pointer."""
    steps = [
        done_step("read_file", {"path": f"C:\\f{i}.txt", "content": f"body {i}"})
        for i in range(3)
    ]
    text = completed_results_text(completed_plan(*steps))
    assert "truncated" not in text
    assert "Activity timeline" not in text
    for i in range(3):
        assert f"body {i}" in text


def test_name_lists_are_clipped_by_item_never_mid_name():
    """A pathological folder (hundreds of entries) clips at whole names with
    an honest count — never a name cut in half by a character cap."""
    entries = [{"name": f"file{i:03}.txt", "type": "file"} for i in range(200)]
    text = completed_results_text(completed_plan(done_step(
        "list_directory",
        {"path": "C:\\big", "entries": entries, "count": 200, "truncated": False},
    )))
    assert "file119.txt" in text          # 120 shown
    assert "file120.txt" not in text
    assert "… and 80 more" in text


# --------------------------------------------- size/date aggregates (2026-07-12)

def test_search_aggregates_answer_largest_newest_total():
    """The live regression: 'find all PDF files in downloads ... what the
    largest one is called'. The raw matches carry size_bytes/modified, but
    the rendered record used to drop them — neither the summary LLM nor the
    deterministic fallback could name the largest file without inventing it.
    The aggregates are computed IN CODE, never in the LLM's head."""
    plan = completed_plan(done_step("search_files", {
        "matches": [
            {"path": "D:\\Downloads\\small.pdf", "type": "file",
             "size_bytes": 1024, "modified": "2026-07-01T10:00:00"},
            {"path": "D:\\Downloads\\big.pdf", "type": "file",
             "size_bytes": 5 * 1024 * 1024, "modified": "2026-06-01T10:00:00"},
            {"path": "D:\\Downloads\\recent.pdf", "type": "file",
             "size_bytes": 2048, "modified": "2026-07-11T09:30:00"},
        ],
        "count": 3,
        "truncated": False,
    }))
    text = completed_results_text(plan)
    assert "Largest: big.pdf (5.0 MB)" in text
    assert "Smallest: small.pdf (1.0 KB)" in text
    assert "Newest: recent.pdf (modified 2026-07-11)" in text
    assert "across 3 file(s)" in text
    # Per-item sizes ride along in the grouped list
    assert "small.pdf (1.0 KB)" in text
    assert "big.pdf (5.0 MB)" in text


def test_search_aggregates_exclude_folders():
    """Folders have no size; a folder match must never win 'largest' or
    'newest', and a lone file alongside folders needs no aggregate line
    (its size already shows inline)."""
    plan = completed_plan(done_step("search_files", {
        "matches": [
            {"path": "C:\\x\\only.pdf", "type": "file",
             "size_bytes": 10, "modified": "2026-07-01T00:00:00"},
            {"path": "C:\\x\\stuff", "type": "folder",
             "size_bytes": None, "modified": "2026-07-12T00:00:00"},
        ],
        "count": 2,
    }))
    text = completed_results_text(plan)
    assert "Largest:" not in text          # a single file → no aggregate line
    assert "only.pdf (10 B)" in text       # but its own size shows inline
    assert "stuff (folder)" in text        # folders never get a size


def test_search_without_sizes_degrades_gracefully():
    """Old-shape rows (no size_bytes/modified) must render exactly as before:
    bare names, no aggregate line, no crash."""
    plan = completed_plan(done_step("search_files", {
        "matches": [
            {"path": "C:\\a\\one.txt", "type": "file"},
            {"path": "C:\\a\\two.txt", "type": "file"},
        ],
        "count": 2,
    }))
    text = completed_results_text(plan)
    assert "- In `C:\\a`: one.txt, two.txt" in text
    assert "Largest:" not in text
    assert "Total:" not in text


def test_list_directory_aggregates_and_sizes():
    plan = completed_plan(done_step("list_directory", {
        "path": "C:\\proj",
        "entries": [
            {"name": "app.log", "type": "file",
             "size_bytes": 3 * 1024 * 1024 * 1024, "modified": "2026-05-01T00:00:00"},
            {"name": "readme.md", "type": "file",
             "size_bytes": 512, "modified": "2026-07-10T08:00:00"},
            {"name": "src", "type": "directory",
             "size_bytes": None, "modified": "2026-07-12T00:00:00"},
        ],
        "count": 3,
    }))
    text = completed_results_text(plan)
    assert "app.log (3.0 GB)" in text
    assert "Largest: app.log (3.0 GB)" in text
    assert "Smallest: readme.md (512 B)" in text
    assert "Newest: readme.md (modified 2026-07-10)" in text   # dirs excluded
    assert "Total: 3.0 GB across 2 file(s)" in text


def test_aggregates_survive_the_step_cap_on_huge_listings():
    """Live verify 2026-07-13: 80 sized names pushed the step render past the
    per-step cap and the clip cut through the trailing aggregate footer —
    the summary reported the largest file as a half-cut name. The aggregate
    line renders FIRST so a clip can only ever eat list tail, never the
    answer."""
    matches = [
        {"path": f"D:\\dl\\a-realistically-long-assignment-report-name-{i:03}.pdf",
         "type": "file", "size_bytes": 1000 + i, "modified": "2026-07-01T00:00:00"}
        for i in range(200)
    ]
    matches.append({"path": "D:\\dl\\the-biggest-file-of-all.pdf", "type": "file",
                    "size_bytes": 99 * 1024 * 1024, "modified": "2026-07-12T12:00:00"})
    text = completed_results_text(completed_plan(done_step("search_files", {
        "matches": matches, "count": len(matches),
    })))
    assert "(truncated)" in text  # the cap did fire on this listing
    assert "Largest: the-biggest-file-of-all.pdf (99.0 MB)" in text
    assert "Newest: the-biggest-file-of-all.pdf (modified 2026-07-12)" in text
    assert text.index("Largest:") < text.index("In `D:\\dl`")  # answer first


# ------------------------------------- the fan-out must not starve the record
#
# THE TRAP (2026-07-17). The original FIFA fabrication was content STARVATION:
# 300 chars of a page survived, cut exactly where the answer began, and the
# summary filled the hole with 112 invented countries. Widening web_search to
# fan a question out over several readings multiplies the rows competing for the
# same render budget — so a fan-out with the cap left at its old value would
# have re-created that exact bug, with MORE sources feeding it. These tests are
# the guard on the arithmetic.

def _fanout_search_output(n_rows: int, content_len: int) -> dict:
    from app.tools.browser_tools import FANOUT_MERGED_MAX
    assert n_rows <= FANOUT_MERGED_MAX, "the merge caps before rendering ever sees them"
    return {
        "query": "which teams are playing the 2026 World Cup final",
        "queries": ["which teams are playing the 2026 World Cup final",
                    "which teams qualified for the 2026 World Cup"],
        "results": [
            {"title": f"Source {i}", "url": f"https://src{i}.example/page",
             "snippet": "s", "content": f"EVIDENCE{i} " + ("x" * content_len),
             "truncated": False, "found_by": ["which teams are playing the 2026 World Cup final"]}
            for i in range(n_rows)
        ],
        "count": n_rows,
    }


def test_a_full_fanout_result_set_renders_without_starving_any_source():
    """Every merged row must survive to the record. If one is dropped, the
    summary answers from a partial record and cannot know it — which is the
    silence that certified the FIFA fragment as complete."""
    from app.tools.browser_tools import CONTENT_MAX_CHARS, FANOUT_MERGED_MAX

    text = steps_for_summary(completed_plan(done_step(
        "web_search", _fanout_search_output(FANOUT_MERGED_MAX, CONTENT_MAX_CHARS - 20),
    )))
    for i in range(FANOUT_MERGED_MAX):
        assert f"EVIDENCE{i}" in text, f"source {i} was starved out of the record"


def test_fanout_readings_are_named_in_the_summary_record():
    """The summary cannot offer the reading it did not lead with unless the
    record tells it the question was read more than one way."""
    from app.tools.browser_tools import CONTENT_MAX_CHARS, FANOUT_MERGED_MAX

    text = steps_for_summary(completed_plan(done_step(
        "web_search", _fanout_search_output(FANOUT_MERGED_MAX, CONTENT_MAX_CHARS - 20),
    )))
    assert "which teams qualified for the 2026 World Cup" in text


def test_web_search_step_cap_covers_a_full_merged_set():
    """The invariant stated in rendering.py, asserted rather than trusted to a
    comment: if FANOUT_MERGED_MAX or CONTENT_MAX_CHARS moves, this fails loudly
    instead of quietly clipping evidence."""
    from app.agents.rendering import _STEP_RESULT_CAPS
    from app.tools.browser_tools import CONTENT_MAX_CHARS, FANOUT_MERGED_MAX

    assert _STEP_RESULT_CAPS["web_search"] >= FANOUT_MERGED_MAX * CONTENT_MAX_CHARS


# ----------------------------------- 14.6: a submitted form's grounded result
def test_browse_commit_completion_is_grounded_in_the_server_response():
    """A commit had no formatter before 2026-07-18, so its completion fell to the
    generic path and read as an ungrounded 'All done'. Now it leads with the
    submit fact and QUOTES the site's own response — the fix for the 'did it
    really happen?' trust gap."""
    text = completed_results_text(completed_plan(done_step(
        "browse_commit",
        {
            "submitted": True,
            "url": "https://the-internet.herokuapp.com/upload",
            "title": "The Internet",
            "response_text": "File Uploaded!\ndummy_upload.txt",
            "window_open": True,
        },
        permission=PermissionLevel.DESTRUCTIVE,
    )))
    assert "Submitted the form" in text
    assert "https://the-internet.herokuapp.com/upload" in text
    assert "File Uploaded!" in text            # the server's own words, not the goal
    assert "dummy_upload.txt" in text
    assert "window is left open" in text       # the kept-open note


def test_browse_commit_completion_without_response_text_is_still_honest():
    """No readable response prose (e.g. a bare redirect) — the confirmation still
    states the submit fact, it just has nothing to quote. Never invents a page."""
    text = completed_results_text(completed_plan(done_step(
        "browse_commit",
        {"submitted": True, "url": "https://example.com/contact", "title": "", "response_text": ""},
        permission=PermissionLevel.DESTRUCTIVE,
    )))
    assert "Submitted the form" in text
    assert "https://example.com/contact" in text
