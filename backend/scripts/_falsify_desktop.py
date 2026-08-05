"""Falsification harness for the Desktop control round (Feature 2).

Every behavioural change must be proven to FAIL when reverted IN PLACE — never
`git show :file` (2026-08-01: in a tree with a large uncommitted baseline that
is not "the code before this change"). The correct signature is:

    behavioural test FAILS   +   regression test PASSES

Lessons this harness encodes, each learned the hard way in this project:
  - the anchor must be a WHOLE LINE including its exact indentation, and UNIQUE
    (an indentation-mismatched revert produced an IndentationError and every
    test failed for the wrong reason, which reads identical to success);
  - re-read the patched file to confirm the revert actually landed before
    trusting any result (three lying falsifications so far);
  - read pytest's EXIT CODE, not its summary text — 5 means nothing was
    collected, i.e. a typo'd test name, which scores the same as a failure;
  - a falsification must remove the GUARANTEE, not one of several copies of it;
  - restore under EVERY exit path, or the harness becomes the bug it hunts;
  - a guard test that calls the function DIRECTLY proves the function works and
    says nothing about whether the planner calls it — cases 1 and 3 below drive
    the real graph for exactly that reason (Feature 1 shipped two such tests and
    both came back GREEN when reverted).

Run:  venv\\Scripts\\python scripts\\_falsify_desktop.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
T = "tests/test_desktop_tools.py"


def _run(test_expr: str) -> tuple[bool, str]:
    """(passed, tail). Exit code 5 = nothing collected — never a pass."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_expr.split(), "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 5:
        return False, "NO TESTS COLLECTED (bad test id) — result is meaningless"
    tail = (proc.stdout or "").strip().splitlines()
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
        # ⚠️ Verify the revert LANDED before trusting anything below it. An
        # ADDITIVE revert legitimately leaves the anchor on disk, so only the
        # replacement's presence is checked.
        on_disk = io.open(path, encoding="utf-8").read()
        for _anchor, replacement in edits:
            if replacement.strip() and replacement not in on_disk:
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
            "1. the window-handle guard sits in the planner's reject chain",
            "app/agents/planner.py",
            [("                             lambda: _window_handle_violation(steps, window_handles or set())),",
              "                             lambda: None),")],
            f"{T}::test_the_planner_refuses_a_draft_that_invents_a_window_handle",
            f"{T}::test_an_invented_handle_is_rejected",
        ),
        (
            "2. an ungrounded handle is refused",
            "app/agents/planner.py",
            [("        if value not in handles:", "        if False:")],
            f"{T}::test_an_invented_handle_is_rejected",
            f"{T}::test_a_pending_placeholder_is_not_checked",
        ),
        (
            "3. the approval card names the real window (wired into the PAUSE)",
            "app/agents/planner.py",
            [("                _enrich_window_action_detail(plan, step)",
              "                pass  # reverted")],
            f"{T}::test_the_approval_card_names_the_window_when_the_plan_pauses",
            f"{T}::test_a_pending_handle_fills_from_a_read_that_pins_one_window",
        ),
        (
            "4. a PENDING handle fills in code",
            "app/agents/placeholder_resolver.py",
            [("        window_key = _window_placeholder_key(template)\n"
              "        if window_key is not None:\n"
              "            return _substitute_window_handle(template, window_key, completed)",
              "        window_key = None\n"
              "        if window_key is not None:\n"
              "            return _substitute_window_handle(template, window_key, completed)")],
            f"{T}::test_the_two_field_window_placeholder_is_recognized",
            f"{T}::test_code_never_picks_between_several_plausible_windows",
        ),
        (
            "5. the title is filled from the SAME row as the handle",
            "app/agents/placeholder_resolver.py",
            [("    if title and (not existing_title or _PLACEHOLDER_MARK in existing_title.upper()):",
              "    if False:")],
            f"{T}::test_a_pending_handle_fills_from_a_read_that_pins_one_window",
            f"{T}::test_code_never_picks_between_several_plausible_windows",
        ),
        (
            "6. short title tokens never manufacture a false single match",
            "app/agents/placeholder_resolver.py",
            [("        len(tok) >= 3 and re.search(rf\"\\b{re.escape(tok)}\", placeholder_text)",
              "        len(tok) >= 0 and re.search(rf\"\\b{re.escape(tok)}\", placeholder_text)")],
            f"{T}::test_short_title_tokens_do_not_manufacture_a_false_match",
            f"{T}::test_a_pending_handle_fills_from_a_read_that_pins_one_window",
        ),
        (
            "7. close_window refuses when the handle now names another window",
            "app/tools/desktop_tools.py",
            [("            if not _titles_match(expected, live.title):",
              "            if False:")],
            f"{T}::test_close_refuses_when_the_handle_now_names_another_window",
            f"{T}::test_close_tolerates_the_unsaved_marker",
        ),
        (
            "8. close_window requires the title it was approved for",
            "app/tools/desktop_tools.py",
            [("        if not expected:", "        if False:")],
            f"{T}::test_close_requires_a_title",
            f"{T}::test_close_tolerates_the_unsaved_marker",
        ),
        (
            "9. launch_app asks rather than guessing between two matches",
            "app/tools/desktop_tools.py",
            [("        if match.candidates:", "        if False:")],
            f"{T}::test_launch_app_asks_rather_than_guessing_between_two_editors",
            f"{T}::test_launch_app_resolves_against_the_registry",
        ),
        (
            "10. an app that is not installed cannot be launched",
            "app/core/desktop.py",
            [("    if best_score < APP_MIN_SCORE:", "    if False:")],
            f"{T}::test_launch_app_refuses_an_app_that_is_not_installed",
            f"{T}::test_launch_app_resolves_against_the_registry",
        ),
        (
            "11. an absurd volume fails rather than clamping",
            "app/tools/desktop_tools.py",
            [("            if not (VOLUME_MIN <= level <= VOLUME_MAX):",
              "            if False:")],
            f"{T}::test_set_volume_refuses_an_absurd_level_rather_than_clamping",
            f"{T}::test_media_key_aliases_and_refuses_the_unknown",
        ),
        (
            "12. the master switch is checked before the controller is touched",
            "app/tools/desktop_tools.py",
            [("    if not config.enabled:", "    if False:")],
            f"{T}::test_every_tool_refuses_while_desktop_control_is_off",
            f"{T}::test_list_windows_returns_handles_and_filters",
        ),
        (
            "13. each sub-toggle gates its own capability",
            "app/tools/desktop_tools.py",
            [("    if permission and not getattr(config, permission, False):",
              "    if False:")],
            f"{T}::test_a_sub_toggle_refuses_its_own_capability",
            f"{T}::test_every_tool_refuses_while_desktop_control_is_off",
        ),
        (
            "14. the screenshot sweep only ever deletes the tool's own files",
            "app/core/desktop.py",
            [('    for path in directory.glob("screen-*.png"):',
              '    for path in directory.glob("*"):')],
            f"{T}::test_the_screenshot_sweep_deletes_only_old_ones_and_only_ours",
            f"{T}::test_the_sweep_never_raises_on_a_missing_directory",
        ),
        (
            "15. an installed app name outranks the website reading (audit tier)",
            "app/api/task_router.py",
            [("    if _is_desktop_intent(text):\n        return \"desktop_intent\"\n"
              "    # A named website to navigate to / operate — general, no per-site list.\n"
              "    if _is_browse_intent(text):\n        return \"browse_intent\"",
              "    # A named website to navigate to / operate — general, no per-site list.\n"
              "    if _is_browse_intent(text):\n        return \"browse_intent\"\n"
              "    if _is_desktop_intent(text):\n        return \"desktop_intent\"")],
            f"{T}::test_an_installed_app_name_outranks_the_website_reading",
            f"{T}::test_the_ordering_never_costs_recall",
        ),
        (
            "16. the launch prefilter keeps the Start Menu off the chat path",
            "app/api/task_router.py",
            [("    match = _LAUNCH_VERB_RE.match(text or \"\")\n    if not match:\n        return False",
              "    match = _LAUNCH_VERB_RE.match(text or \"\")\n    if False:\n        return False")],
            f"{T}::test_the_launch_prefilter_keeps_the_registry_off_the_chat_path",
            f"{T}::test_open_an_installed_app_is_its_own_gate_tier",
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
