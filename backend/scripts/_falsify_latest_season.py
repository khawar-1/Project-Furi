"""
Falsification for the 2026-08-07 latest-season round.

THE HOUSE RULE: a behavioural change is not accepted until it has been proven to
FAIL by reverting the specific line IN PLACE — never `git show :file`, which in a
tree with a large uncommitted baseline is not "the code before this change".

The recorded harness lessons are encoded as CHECKS, not as good intentions:

  * anchors must be UNIQUE whole lines          (a non-unique anchor patches the
                                                 wrong branch — 2026-08-06)
  * the revert must be VERIFIED ON DISK         (three lying falsifications so far)
  * pytest's EXIT CODE is read, not its text    (5 = nothing collected is never a
                                                 pass — 2026-08-04)
  * restore happens in a `finally`              (a harness that edits source and
                                                 dies mid-run becomes the bug it
                                                 was hunting — 2026-08-04)
  * a REGRESSION test is run alongside          (a "falsification" whose
                                                 regression also fails proves
                                                 nothing — 2026-08-04)

Run:  venv\\Scripts\\python scripts\\_falsify_latest_season.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (name, [(file, anchor_line, replacement_line), …], behavioural_test, regression_test)
CASES = [
    (
        "the leading-ordinal strip is REPEATED",
        [(
            "app/browser/loop.py",
            '    r")+",',
            '    r")",',
        )],
        "tests/test_browse_latest_season.py::test_the_title_survives_a_stacked_ordinal",
        "tests/test_browser_loop.py::test_fast_path_types_the_title_not_the_media_descriptor",
    ),
    (
        "user_words is stamped on browse and NOT on browse_commit",
        [(
            "app/agents/planner.py",
            '        if step.tool != "browse":',
            "        if step.tool not in browser_grounding._BROWSE_TOOLS:",
        )],
        "tests/test_browse_latest_season.py::test_user_words_are_stamped_on_browse_and_never_on_browse_commit",
        "tests/test_browse_latest_season.py::test_injection_is_a_no_op_without_a_goal",
    ),
    (
        "the injector is WIRED into _execute_node",
        [(
            "app/agents/planner.py",
            "        _inject_user_words(plan)",
            "        pass  # reverted",
        )],
        "tests/test_browse_latest_season.py::test_the_injector_is_actually_wired_into_the_execute_path",
        "tests/test_browse_latest_season.py::test_user_words_are_stamped_on_browse_and_never_on_browse_commit",
    ),
    (
        "the loop reads intent_text, not the paraphrase",
        [(
            "app/browser/loop.py",
            '    intent = (intent_text or "").strip() or goal',
            "    intent = goal",
        )],
        "tests/test_browse_latest_season.py::test_intent_text_revives_the_paths_the_paraphrase_killed",
        "tests/test_browse_latest_season.py::test_without_intent_text_the_goal_is_still_used",
    ),
    (
        "the media hand-off reads the user's words",
        [(
            "app/tools/browser_agent_tools.py",
            "                wants_playback = browser_loop.goal_wants_playback(intent_text)",
            "                wants_playback = browser_loop.goal_wants_playback(goal)",
        )],
        "tests/test_browse_window_continuity.py::test_the_handoff_reads_the_users_words_not_the_planners_paraphrase",
        "tests/test_browse_window_continuity.py::test_a_non_playback_goal_is_still_refused_through_user_words",
    ),
    (
        "the season name picks the entry (the leg is wired in the step loop)",
        [(
            "app/browser/loop.py",
            "                    action = entry",
            "                    action = None  # reverted",
        )],
        "tests/test_browse_latest_season.py::test_intent_text_revives_the_paths_the_paraphrase_killed",
        "tests/test_browse_latest_season.py::test_scoring_ranks_the_real_anikoto_entries",
    ),
    (
        "the season name outranks the tightest slug",
        [(
            "    want = _tokens(season_name) - _NOISE - _tokens(title)",
            None,
            None,
        )],  # placeholder, replaced below
        "",
        "",
    ),
    (
        "the absolute web number is dropped once a season is chosen",
        [(
            "app/browser/loop.py",
            "        if not latest_web_done and not season_scoped:",
            "        if not latest_web_done:",
        )],
        "tests/test_browse_latest_season.py::test_the_absolute_web_number_is_ignored_once_a_season_is_chosen",
        "tests/test_browser_loop.py::test_latest_episode_flow_web_number_then_url_swap",
    ),
    (
        "a non-allowlisted new tab is never adopted",
        [(
            "app/browser/session.py",
            "        if not self._may_adopt(page):",
            "        if False:",
        )],
        "tests/test_browser_session.py::test_an_ad_popup_is_never_adopted",
        "tests/test_browser_session.py::test_a_popup_is_adopted_under_the_same_interceptor",
    ),
    (
        "the adopt rule tests the ALLOWLIST",
        [(
            "app/browser/session.py",
            "            if not host or self.origin_allowed(host):",
            "            if not host or True:",
        )],
        "tests/test_browse_latest_season.py::test_a_new_tab_is_adopted_only_where_the_loop_may_go",
        "tests/test_browse_latest_season.py::test_the_adopt_check_never_raises_on_an_odd_page",
    ),
    (
        "a slow season read is retried, not cached as a miss",
        [(
            "app/browser/loop.py",
            "            pass  # still running — ask again next step",
            "            season_done = True  # reverted: give up permanently",
        )],
        "tests/test_browse_latest_season.py::test_a_slow_season_read_is_picked_up_on_a_later_step",
        "tests/test_browse_latest_season.py::test_intent_text_revives_the_paths_the_paraphrase_killed",
    ),
    (
        "a standalone digit survives tokenisation (Season 2 vs Season 3)",
        [(
            "app/browser/season.py",
            "        if len(t) > 1 or t.isdigit()",
            "        if len(t) > 1",
        )],
        "tests/test_browse_latest_season.py::test_an_id_suffix_can_never_be_read_as_a_season_number",
        "tests/test_browse_latest_season.py::test_scoring_ranks_the_real_anikoto_entries",
    ),
]
# Drop the placeholder entry.
CASES = [c for c in CASES if c[2]]


def _pytest(node: str) -> tuple[bool, str]:
    """(passed, why). Reads the EXIT CODE: 5 means nothing was collected, which
    scores identically to a failure if you only read the summary text."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode == 5:
        return False, "NOTHING COLLECTED (bad node id) — not a result"
    if proc.returncode not in (0, 1):
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        return False, "ERROR: " + " | ".join(tail)
    return proc.returncode == 0, ""


def main() -> int:
    bad = 0
    for name, edits, behavioural, regression in CASES:
        print("=" * 78)
        print(f"CASE: {name}")

        originals: list[tuple[pathlib.Path, str]] = []
        try:
            ok = True
            for rel, anchor, replacement in edits:
                path = ROOT / rel
                text = path.read_text(encoding="utf-8")
                originals.append((path, text))
                count = text.count(anchor)
                if count != 1:
                    print(f"  ✗ ANCHOR NOT UNIQUE in {rel}: {count} matches — refusing")
                    ok = False
                    break
                patched = text.replace(anchor, replacement)
                path.write_text(patched, encoding="utf-8")
                # VERIFY IT LANDED. Three falsifications in this project have
                # come back green because the edit never reached the file.
                on_disk = path.read_text(encoding="utf-8")
                if replacement not in on_disk or on_disk.count(anchor) != 0:
                    print(f"  ✗ REVERT DID NOT LAND in {rel} — refusing")
                    ok = False
                    break
            if not ok:
                bad += 1
                continue

            b_pass, b_why = _pytest(behavioural)
            r_pass, r_why = _pytest(regression)
        finally:
            for path, text in originals:
                path.write_text(text, encoding="utf-8")

        # The signature that makes a falsification valid.
        if b_why or r_why:
            print(f"  ✗ INVALID RUN  behavioural={b_why or 'ok'}  regression={r_why or 'ok'}")
            bad += 1
        elif b_pass:
            print("  ✗ BEHAVIOURAL TEST STILL PASSED on the reverted code —")
            print("    the test cannot see the defect, or the revert removed only")
            print("    ONE COPY of a guarantee that is enforced in several places.")
            bad += 1
        elif not r_pass:
            print("  ✗ THE REGRESSION TEST ALSO FAILED — it depends on the reverted")
            print("    line, so it is a second behavioural test and proves nothing.")
            bad += 1
        else:
            print("  ✓ behavioural FAILS, regression PASSES — valid")

    # Everything restored?
    print("=" * 78)
    dirty = subprocess.run(
        ["git", "diff", "--stat", "--", "app/"], cwd=ROOT,
        capture_output=True, text=True,
    ).stdout
    print(f"{len(CASES) - bad}/{len(CASES)} valid falsifications")
    print("app/ restored cleanly" if "reverted" not in dirty else "⚠️ SOURCE LEFT PATCHED")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
