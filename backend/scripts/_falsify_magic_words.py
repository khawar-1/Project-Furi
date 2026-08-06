"""Falsification harness for the magic-word round (2026-08-06).

Every behavioural change must be proven to FAIL when reverted IN PLACE — never
`git show :file` (2026-08-01: in a tree with a large uncommitted baseline that
is not "the code before this change"). The correct signature is:

    behavioural test FAILS   +   regression test PASSES

Machinery is `_falsify_open_folder.py`'s, unchanged, and so are the lessons it
encodes: a UNIQUE whole-line anchor including its indentation; re-read the
patched file before trusting a result; read pytest's EXIT CODE (5 = nothing
collected, which scores identically to a failure); remove the GUARANTEE rather
than one of several copies of it; restore under every exit path.

⚠️ Two anchors here are deliberately MULTI-LINE. `if attempt == 2:` appears
twice in `_classify_message` — once on the exception path and once on the
recovered-verdict log — and a non-unique anchor would either patch the wrong
branch or be skipped, both of which read like success.

Run:  venv\\Scripts\\python scripts\\_falsify_magic_words.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
T = "tests/test_task_router.py"

ROUTER = "app/api/task_router.py"
CHAT = "app/api/chat.py"


def _run(test_expr: str) -> tuple[bool, str]:
    """(passed, tail). Exit code 5 = nothing collected — never a pass."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_expr.split(), "-q", "--no-header",
         "-p", "no:logging", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 5:
        return False, "NO TESTS COLLECTED (bad test id) — result is meaningless"
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    return proc.returncode == 0, tail[-1] if tail else "(no output)"


def falsify(label, rel, edits, behavioural, regression) -> bool:
    path = ROOT / rel
    original = io.open(path, encoding="utf-8").read()
    patched = original
    for anchor, replacement in edits:
        count = patched.count(anchor)
        if count != 1:
            print(f"  [SKIP] anchor not unique ({count} matches): {anchor.strip()[:64]}\n")
            return False
        patched = patched.replace(anchor, replacement)

    io.open(path, "w", encoding="utf-8").write(patched)
    try:
        # ⚠️ Verify the revert LANDED before trusting anything below it. A
        # SUBTRACTIVE revert has an empty replacement, so for those the check
        # is that the anchor is gone.
        on_disk = io.open(path, encoding="utf-8").read()
        for anchor, replacement in edits:
            landed = (replacement in on_disk) if replacement.strip() else (anchor not in on_disk)
            if not landed:
                print("  [INVALID] revert did not land on disk\n")
                return False
        b_pass, b_tail = _run(behavioural)
        r_pass, r_tail = _run(regression) if regression else (True, "(none)")
    finally:
        io.open(path, "w", encoding="utf-8").write(original)
        assert io.open(path, encoding="utf-8").read() == original, "RESTORE FAILED"

    ok = (not b_pass) and r_pass
    print(f"  behavioural: {'FAIL (correct)' if not b_pass else 'PASSED (INVALID)'}  | {b_tail}")
    print(f"  regression : {'PASS (correct)' if r_pass else 'FAILED (INVALID)'}  | {r_tail}")
    print(f"  => {'VALID' if ok else 'INVALID — this change is not proven'}\n")
    return ok


def build_cases():
    return [
        (
            "1. an EMPTY classifier reply is retried (the incident's root cause)",
            ROUTER,
            [("    for attempt in (1, 2):", "    for attempt in (1,):")],
            f"{T}::test_an_empty_classifier_reply_is_retried_not_taken_as_chat",
            f"{T}::test_a_clean_chat_verdict_is_never_retried",
        ),
        (
            # ⚠️ MULTI-LINE: `if attempt == 2:` also appears on the
            # recovered-verdict log a few lines below. Reverting only the
            # exception branch is what isolates it from case 1.
            "2. a RAISED classifier call is retried too",
            ROUTER,
            [("            if attempt == 2:\n"
              "                return _stamp(\"CHAT\", \"DELEGATE\", error=last_error)\n"
              "            continue",
              "            return _stamp(\"CHAT\", \"DELEGATE\", error=last_error)")],
            f"{T}::test_a_raising_classifier_is_retried_too",
            f"{T}::test_a_clean_chat_verdict_is_never_retried",
        ),
        (
            "3. a clean CHAT is a JUDGEMENT and is never retried",
            ROUTER,
            [("        if reply.startswith(\"CHAT\"):\n"
              "            return _stamp(\"CHAT\", \"DELEGATE\")\n",
              "")],
            f"{T}::test_a_clean_chat_verdict_is_never_retried",
            f"{T}::test_an_empty_classifier_reply_is_retried_not_taken_as_chat",
        ),
        (
            "4. the classifier budget stays above the thinking floor",
            ROUTER,
            [("_CLASSIFY_MAX_TOKENS = 1024", "_CLASSIFY_MAX_TOKENS = 8")],
            f"{T}::test_the_classifier_budget_stays_above_the_thinking_floor",
            f"{T}::test_the_retry_nudge_names_every_allowed_verdict",
        ),
        (
            "5. the retry nudge names every verdict the parser accepts",
            ROUTER,
            [("    \"\\n\\nYour previous reply was empty or did not begin with one of the \"\n"
              "    \"allowed words. Reply now with EXACTLY one of: TASK, EMAIL, CALENDAR, \"\n"
              "    \"WEB, HOME, DESKTOP, BROWSE, CHAT — optionally followed by INLINE or \"\n"
              "    \"DELEGATE. No explanation, no reasoning, nothing else.\"",
              "    \"\\n\\nYour previous reply was unusable. Try again.\"")],
            f"{T}::test_the_retry_nudge_names_every_allowed_verdict",
            f"{T}::test_the_classifier_budget_stays_above_the_thinking_floor",
        ),
        (
            "6. open_folder is described in the classifier's tool catalog",
            ROUTER,
            [("search/read/list files and folders, OPEN A FOLDER in a file-explorer window on screen (or show the user where a file lives), create/move",
              "search/read/list files and folders, create/move")],
            f"{T}::test_every_registered_tool_is_described_or_deliberately_absent",
            f"{T}::test_the_classifier_budget_stays_above_the_thinking_floor",
        ),
        (
            # Found BY the coverage test on its first run — stop_media was
            # routable ("stop the music" is deliberately kept out of the
            # interrupt router so it stays a stop_media task) and the catalog
            # never mentioned it.
            "6b. stop_media is described too (found by the walk itself)",
            ROUTER,
            [(", or stop something it is already playing", "")],
            f"{T}::test_every_registered_tool_is_described_or_deliberately_absent",
            f"{T}::test_the_classifier_budget_stays_above_the_thinking_floor",
        ),
        (
            # ⚠️ Cases 7 and 8 revert the SAME branch but prove two different
            # properties of it: that the turn gets rescued at all, and that the
            # demand never reaches the screen. Either could regress alone.
            "7. a non-web dead end is rescued (the reported sentence)",
            CHAT,
            [("    r\"|(?:say|saying|rephrase|rephrasing|phrase|phrasing|word|wording|\"\n"
              "    r\"re-?say|ask(?:ing)? (?:me|it)|put)\"\n"
              "    r\"[^\\n]{0,24}?as (?:a|one|the)\\s+\"\n"
              "    r\"(?:single |simple |plain |clear |explicit )?direct\\s+\"\n"
              "    r\"(?:instruction|question|request|command)\"\n",
              "")],
            f"{T}::test_a_folder_dead_end_is_rescued_the_way_a_web_one_always_was",
            f"{T}::test_dead_end_offer_is_rescued_into_a_real_search",
        ),
        (
            "8. the magic-word demand itself never reaches the user",
            CHAT,
            [("    r\"|(?:say|saying|rephrase|rephrasing|phrase|phrasing|word|wording|\"\n"
              "    r\"re-?say|ask(?:ing)? (?:me|it)|put)\"\n"
              "    r\"[^\\n]{0,24}?as (?:a|one|the)\\s+\"\n"
              "    r\"(?:single |simple |plain |clear |explicit )?direct\\s+\"\n"
              "    r\"(?:instruction|question|request|command)\"\n",
              "")],
            f"{T}::test_the_magic_word_demand_never_reaches_the_user",
            f"{T}::test_an_offer_chat_can_keep_is_still_not_a_dead_end",
        ),
        (
            # ⚠️ THE COVERAGE TEST FOR D3. Cases 7 and 8 prove the branch
            # rescues a turn; this proves the WALK over the live prompt would
            # have caught its absence in the first place — i.e. that the test
            # protecting this round can itself see the defect.
            "8b. the prompt-walk coverage test detects an uncovered rule",
            CHAT,
            [("    r\"|(?:say|saying|rephrase|rephrasing|phrase|phrasing|word|wording|\"\n"
              "    r\"re-?say|ask(?:ing)? (?:me|it)|put)\"\n"
              "    r\"[^\\n]{0,24}?as (?:a|one|the)\\s+\"\n"
              "    r\"(?:single |simple |plain |clear |explicit )?direct\\s+\"\n"
              "    r\"(?:instruction|question|request|command)\"\n",
              "")],
            f"{T}::test_every_rephrase_demand_in_the_live_prompt_trips_the_guard",
            f"{T}::test_an_offer_chat_can_keep_is_still_not_a_dead_end",
        ),
        (
            "9. our own deterministic text carries no magic-word demand",
            CHAT,
            [("    \"changed. Ask me again and I will carry it out for real — plain wording is \"\n"
              "    \"fine, there is no particular phrase you need to use.\"",
              "    \"changed. To actually do this, say it as a direct instruction, e.g. \"\n"
              "    '\"delete the .txt files in my Downloads folder\".'")],
            f"{T}::test_no_deterministic_text_trips_the_dead_end_guard",
            f"{T}::test_an_offer_chat_can_keep_is_still_not_a_dead_end",
        ),
        (
            "10. the rescue-failed text does not claim a search happened",
            CHAT,
            [("    \"\\n\\nI tried to handle that directly and the attempt itself failed, so I \"\n"
              "    \"have no result for you rather than a guessed one. Worth trying again in \"\n"
              "    \"a moment.\"",
              "    \"\\n\\nI tried to look that up and the search itself failed, so I have no \"\n"
              "    \"answer for you rather than a guessed one.\"")],
            f"{T}::test_the_rescue_failure_text_does_not_claim_a_search_happened",
            f"{T}::test_no_deterministic_text_trips_the_dead_end_guard",
        ),
        (
            "11. the prompt stops narrating routing plumbing at the user",
            CHAT,
            [(" Never explain the routing, the tool system, or why the request did not reach it: that is internal plumbing, it means nothing to the user, and it is not their problem to work around.",
              "")],
            f"{T}::test_the_prompt_never_tells_the_user_to_work_around_the_plumbing",
            f"{T}::test_an_offer_chat_can_keep_is_still_not_a_dead_end",
        ),
        (
            "12. the old rescue name still resolves to the same function",
            ROUTER,
            [("rescue_web_turn = rescue_unrouted_turn", "rescue_web_turn = None")],
            f"{T}::test_the_rescue_is_the_same_function_under_both_names",
            f"{T}::test_the_classifier_budget_stays_above_the_thinking_floor",
        ),
    ]


def main() -> int:
    cases = build_cases()
    print(f"Falsifying {len(cases)} behavioural changes (revert in place)\n")
    results = []
    for label, rel, edits, behavioural, regression in cases:
        print(f"[{label}]")
        results.append(falsify(label, rel, edits, behavioural, regression))
    valid = sum(results)
    print(f"{valid}/{len(results)} proven")
    return 0 if valid == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
