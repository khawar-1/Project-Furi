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
    _fmt_browse_commit,
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


def test_empty_search_names_where_it_looked():
    """An empty result that does not say WHERE it looked is undiagnosable.
    Live 2026-07-30: "move all the pdf files from downloads" searched the empty
    C:\\Users\\DELL\\Downloads while 85 PDFs sat in D:\\Downloads, and the
    outcome read as a flat "you have no PDFs" — neither the user nor the revise
    LLM could see which Downloads had been searched. The roots are in the
    tool's own output; report them."""
    text = completed_results_text(completed_plan(
        done_step("search_files", {
            "matches": [], "count": 0, "truncated": False,
            "searched_in": ["C:\\Users\\DELL\\Downloads"],
        })
    ))
    assert "No matches found in `C:\\Users\\DELL\\Downloads`." in text

    multi = completed_results_text(completed_plan(
        done_step("search_files", {
            "matches": [], "count": 0,
            "searched_in": ["C:\\a", "D:\\b"],
        })
    ))
    assert "`C:\\a`" in multi and "`D:\\b`" in multi


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
    assert "(clipped for length)" in text  # the cap did fire on this listing
    # ...and it must NOT say "truncated": that word is reserved for a tool
    # reporting it did not fetch everything (2026-07-29 — a clip marker on a
    # COMPLETE result was read back as data loss and derailed the plan).
    assert "truncated" not in text
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


# ------------------------- 2026-07-22: the clean-window media hand-off framing
def test_browse_clean_window_handoff_is_honest_about_the_play_button():
    """A watch/play goal handed off to a normal ad-blocked window says it opened it
    there AND that a custom player may need one click — the honest CDP trade-off."""
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "playing": True,
            "handoff": "clean_window",
            "url": "https://anikoto.cz/watch/123",
            "title": "Ep 12",
            "rendered": "",
        },
    )))
    assert "ad-free" in text
    assert "anikoto.cz/watch/123" in text
    assert "press play" in text


def test_browse_media_handoff_never_dumps_the_page_element_list():
    """The 'wall of text' live miss (2026-07-25): a play/handoff 'done' notification
    fenced the whole final observation — 161 DOM elements (a site's entire episode
    index + A-Z footer). A media outcome's head line IS the answer; the element dump
    is pure noise, so it must be suppressed for playing/handoff results (it survives
    only for the read-a-fact fallback — see the book-price test)."""
    dump = "ELEMENTS (96 of 161 shown):\n[1] link -> /home\n" + "\n".join(
        f'[{i}] link "11{i:02d}" -> https://anikoto.cz/watch/one-piece-odmau/ep-11{i:02d}'
        for i in range(1, 90)
    )
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "playing": True,
            "handoff": "clean_window",
            "url": "https://anikoto.cz/watch/one-piece-odmau/ep-1170",
            "title": "Anime One Piece Episode 1170 Watch Online Free - Anikoto",
            "rendered": dump,
        },
    )))
    # The headline outcome is present; the DOM dump is gone.
    assert "ad-free" in text
    assert "ep-1170" in text
    assert "ELEMENTS (" not in text
    assert "/watch/one-piece-odmau/ep-1150" not in text


def test_browse_media_handoff_that_could_not_open_tells_the_user_to_open_it():
    """The clean window couldn't open (no system browser) — the summary is honest:
    it found the video but the user must open the link themselves."""
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "playing": False,
            "handoff": "none",
            "url": "https://anikoto.cz/watch/123",
            "title": "Ep 12",
            "rendered": "",
        },
    )))
    assert "couldn't open" in text
    assert "anikoto.cz/watch/123" in text


def test_browse_extracted_data_is_surfaced_in_the_summary():
    """A list/compare browse goal that gathered records with `extract` renders them
    into the summary (the answer to 'the 3 cheapest phones'), grounded in the copied
    page values — not the raw element dump."""
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "playing": False,
            "handoff": "",
            "url": "https://shop.test/phones",
            "title": "Phones",
            "done_reason": "compared them",
            "rendered": "",
            "extracted": [
                {"name": "Phone A", "price": "$100", "rating": "4.5"},
                {"name": "Phone B", "price": "$200", "rating": "4.8"},
            ],
        },
    )))
    assert "Gathered from the page (2 item(s))" in text
    assert "name: Phone A, price: $100, rating: 4.5" in text
    assert "name: Phone B, price: $200, rating: 4.8" in text


def test_browse_with_no_extracted_data_renders_no_gathered_block():
    """A plain browse (no extract) never grows an empty 'Gathered' section."""
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {"playing": False, "handoff": "", "url": "https://x.test/", "title": "X",
         "done_reason": "here", "rendered": "", "extracted": []},
    )))
    assert "Gathered from the page" not in text


# ---------------------------- 15.5: a multi-commit flow quotes EACH response
def test_multi_commit_completion_quotes_every_server_response():
    """A flow that submitted several forms ("apply to 3 jobs") renders ONE grounded
    block per commit — each site's OWN response, not just the last. Without this
    the flow-level summary would drop the earlier submits' confirmations."""
    text = completed_results_text(completed_plan(done_step(
        "browse_commit",
        {
            "submitted": True,
            "url": "https://jobs.example.com/apply/3",
            "title": "Applied",
            "response_text": "Application 3 received",
            "window_open": True,
            "commit_history": [
                {"n": 1, "url": "https://jobs.example.com/apply/1", "title": "Applied",
                 "response_text": "Application 1 received"},
                {"n": 2, "url": "https://jobs.example.com/apply/2", "title": "Applied",
                 "response_text": "Application 2 received"},
                {"n": 3, "url": "https://jobs.example.com/apply/3", "title": "Applied",
                 "response_text": "Application 3 received"},
            ],
        },
        permission=PermissionLevel.DESTRUCTIVE,
    )))
    assert "Submitted 3 forms" in text
    # Every commit's destination AND its own server response is quoted.
    for i in (1, 2, 3):
        assert f"apply/{i}" in text
        assert f"Application {i} received" in text
    assert "window is left open" in text          # the final form's kept-open note


def test_single_entry_commit_history_renders_the_single_form_path():
    """A one-commit flow (history length 1) is NOT a multi-form render — it stays
    the ordinary single grounded confirmation (backwards compatible)."""
    text = completed_results_text(completed_plan(done_step(
        "browse_commit",
        {
            "submitted": True,
            "url": "https://example.com/contact",
            "title": "Sent",
            "response_text": "Thanks!",
            "commit_history": [
                {"n": 1, "url": "https://example.com/contact", "title": "Sent",
                 "response_text": "Thanks!"},
            ],
        },
        permission=PermissionLevel.DESTRUCTIVE,
    )))
    assert "Submitted the form" in text          # single-form wording, not "Submitted N forms"
    assert "Submitted 1 forms" not in text
    assert "Thanks!" in text


# ------------------- a FAILED plan keeps its completed evidence (2026-07-21)

def test_failed_plan_text_carries_the_completed_steps_results():
    """The live incident: a browse plan's last step failed, and the failure text
    listed only step DESCRIPTIONS — the price sitting in a completed step's
    output was thrown away, so 'what was the price?' was unanswerable one turn
    later. The FAILED branch now renders the completed results too."""
    plan = AgentPlan(
        goal="read the book price then go back",
        status=PlanStatus.FAILED,
        message="the back step could not run",
        steps=[
            done_step("browse", {
                "url": "https://books.toscrape.com/catalogue/its-only-the-himalayas_981/",
                "title": "It's Only the Himalayas",
                "page_excerpt": "It's Only the Himalayas £45.17 In stock",
                "done_reason": "the details and price are displayed",
                "rendered": "It's Only the Himalayas £45.17 In stock (19 available)",
                "goal_reached": True,
            }, description="Open the top travel book"),
            done_step("browse", None, description="Go back to the category list",
                      status=StepStatus.FAILED),
        ],
    )
    text = deterministic_plan_text(plan)
    assert "I couldn't finish that." in text
    assert "Open the top travel book" in text            # the description line
    assert "£45.17" in text                              # the EVIDENCE survives


def test_failed_plan_with_no_completed_steps_is_unchanged():
    plan = AgentPlan(
        goal="g", status=PlanStatus.FAILED, message="nothing ran",
        steps=[done_step("browse", None, status=StepStatus.FAILED)],
    )
    text = deterministic_plan_text(plan)
    assert text == "I couldn't do that. nothing ran"


def test_browse_commit_names_the_url_that_actually_carried_the_submission():
    """2026-07-26: a form's declared action is not reliably its endpoint (Shopify
    posts /cart/add.js for action="/cart/add"). When they differ, the grounded
    confirmation names the request that actually delivered the approved
    contract — the audit record should not read as if the action url was used."""
    text = _fmt_browse_commit(
        {
            "url": "https://shop.test/products/janan-sport",
            "submitted_url": "https://shop.test/cart/add.js",
            "title": "JANAN SPORT",
            "response_text": "Added to cart",
        }
    )
    assert "https://shop.test/cart/add.js" in text
    assert "Added to cart" in text


def test_browse_commit_stays_quiet_when_the_action_url_was_used():
    """The common case gains no noise — the clause appears only on a difference."""
    text = _fmt_browse_commit(
        {"url": "https://shop.test/thanks", "title": "Thanks", "response_text": "Done"}
    )
    assert "Sent as" not in text


# ==================================== destination-only browse (2026-08-01)
#
# The wall of text, second time. The 2026-07-25 round above suppressed the DOM
# dump for a MEDIA outcome; a browse whose goal was only to ARRIVE somewhere
# was not covered and fell to the informational branch. Live: "open youtube"
# finished correctly in one second and then replied with the entire YouTube
# homepage — 108 element lines plus every video title on it, 8,609 characters.

def _youtube_homepage_dump() -> str:
    elements = "\n".join(
        f'[{i}] link "some video {i}" -> /watch?v=vid{i}' for i in range(1, 109)
    )
    prose = "Skip navigation\nHome\nShorts\n" + "\n".join(
        f"some video {i} 3.{i}M views" for i in range(1, 109)
    )
    return f"URL: https://www.youtube.com/\nTITLE: YouTube\n\nELEMENTS (108):\n{elements}\n\nPAGE TEXT:\n{prose}"


def test_a_destination_only_browse_reports_arriving_and_nothing_else():
    """The incident, frozen. The goal asked only to BE somewhere; nothing was
    read, so there is nothing to report but arriving."""
    dump = _youtube_homepage_dump()
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "url": "https://www.youtube.com/",
            "title": "YouTube",
            "done_reason": "YouTube is open — that was the whole goal.",
            "rendered": dump,
            "page_text": dump.split("PAGE TEXT:\n")[1],
            "destination_only": True,
        },
    )))
    assert "YouTube" in text and "https://www.youtube.com/" in text
    assert "that was the whole goal" in text
    assert "ELEMENTS (108)" not in text
    assert "some video 42" not in text
    assert len(text) < 400, f"expected a one-line answer, got {len(text)} chars"


def test_an_informational_browse_keeps_the_prose_and_drops_the_element_list():
    """The read-a-fact case still reports what it read — but the element list is
    the DECISION prompt's format (observe.render: "the observation as the LLM
    sees it"), scaffolding that exists so the loop can say "click 23". It answers
    no question and grounds no summary."""
    dump = (
        "URL: https://books.test/travel\nTITLE: Travel\n\n"
        "ELEMENTS (60):\n" + "\n".join(f'[{i}] link "book {i}"' for i in range(1, 61))
        + "\n\nPAGE TEXT:\nIt's Only the Himalayas\nPrice: £45.17\nIn stock"
    )
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "url": "https://books.test/travel",
            "title": "Travel",
            "done_reason": "read the price",
            "rendered": dump,
            "page_text": "It's Only the Himalayas\nPrice: £45.17\nIn stock",
            "destination_only": False,
        },
    )))
    assert "£45.17" in text, "the fact the browse went to read must survive"
    assert "ELEMENTS (60)" not in text
    assert '[7] link "book 7"' not in text


def test_a_page_with_no_prose_still_falls_back_to_the_full_render():
    """A page that is genuinely all controls must still show something."""
    dump = 'URL: https://app.test/\n\nELEMENTS (3):\n[1] button "Start"'
    text = completed_results_text(completed_plan(done_step(
        "browse",
        {
            "url": "https://app.test/",
            "done_reason": "opened the console",
            "rendered": dump,
            "page_text": "",
            "destination_only": False,
        },
    )))
    assert 'button "Start"' in text


# ==================================== the commit wall of text (2026-08-02)
#
# Live: an approved add-to-cart completed and the reply carried ~1500 chars of
# the product page — the whole navigation menu — as "the site's response". Third
# wall of text in this stack (the media dump 2026-07-25, the destination-only
# DOM dump 2026-08-01, this).
#
# Two claims were wrong at once: the prose was the page the form was submitted
# FROM rather than anything the submission produced, and the head asserted the
# site had "responded" with a title that was just where we still were.

_JJ_URL = (
    "https://www.junaidjamshed.com/collections/fragrances/products/janan-sport"
    "?variant=56957187915936"
)


def test_a_commit_confirmation_is_not_a_copy_of_the_page():
    """The reply the user got, rendered from what the tool now returns. It states
    the confirmed facts and stops."""
    text = _fmt_browse_commit({
        "submitted": True,
        "url": _JJ_URL,
        "submitted_url": "https://www.junaidjamshed.com/cart/add.js",
        "title": "JANAN SPORT - 100ml",
        "response_text": "",          # nothing the submission produced
        "page_changed": False,
        "window_open": True,
    })
    # MEASURED: the incident rendered 1832 chars.
    assert len(text) < 500, text
    assert "Submitted the form" in text
    assert "https://www.junaidjamshed.com/cart/add.js" in text   # what carried it
    assert "window is left open" in text


def test_a_commit_that_never_navigated_does_not_claim_the_site_responded():
    """"The site responded: X" is only true when the submission MOVED us. On an
    AJAX submit X is the page we were already on, and calling it a response is
    the same class of overclaim as reporting an unconfirmed submit as a failure."""
    stayed = _fmt_browse_commit(
        {"url": _JJ_URL, "title": "JANAN SPORT - 100ml", "page_changed": False}
    )
    assert "The site responded" not in stayed
    assert "stayed on" in stayed

    moved = _fmt_browse_commit(
        {"url": "https://shop.test/thanks", "title": "Thanks", "page_changed": True}
    )
    assert "The site responded: **Thanks**" in moved
