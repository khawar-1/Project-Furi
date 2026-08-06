"""Falsification harness for the open_folder round (2026-08-06).

Every behavioural change must be proven to FAIL when reverted IN PLACE — never
`git show :file` (2026-08-01: in a tree with a large uncommitted baseline that
is not "the code before this change"). The correct signature is:

    behavioural test FAILS   +   regression test PASSES

The machinery is `_falsify_desktop.py`'s, unchanged, and so are the lessons it
encodes: a UNIQUE whole-line anchor including its indentation; re-read the
patched file before trusting a result; read pytest's EXIT CODE (5 = nothing
collected, which scores identically to a failure); remove the GUARANTEE rather
than one of several copies of it; restore under every exit path.

⚠️ Two anchors here are deliberately MULTI-LINE. `if not path.exists():`,
`if reason := _blocked_reason(path):` and `return PermissionLevel.WRITE` each
appear four or more times in file_tools.py, and a non-unique anchor would
either patch the wrong tool or be skipped — both of which read like success.

Run:  venv\\Scripts\\python scripts\\_falsify_open_folder.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
T = "tests/test_open_folder.py"


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
            "1. a FILE resolves to its parent — the never-execute invariant",
            "app/tools/file_tools.py",
            [("    return path if path.is_dir() else path.parent",
              "    return path")],
            f"{T}::test_a_file_opens_its_containing_folder_never_the_file",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            "2. an executable is revealed, never run (same invariant, real names)",
            "app/tools/file_tools.py",
            [("    return path if path.is_dir() else path.parent",
              "    return path")],
            f"{T}::test_an_executable_file_is_still_only_ever_revealed",
            # NOT test_folder_to_show_is_the_whole_invariant: that test drives
            # the very line being reverted, so it is a second behavioural test,
            # not a regression. A "regression" that also fails proves nothing.
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            "3. a path that does not exist is refused",
            "app/tools/file_tools.py",
            [("        if not path.exists():\n"
              "            return _fail(\n"
              "                self,\n"
              "                f\"'{path}' does not exist, so there is no folder to open. \"",
              "        if False:\n"
              "            return _fail(\n"
              "                self,\n"
              "                f\"'{path}' does not exist, so there is no folder to open. \"")],
            f"{T}::test_a_path_that_does_not_exist_fails_and_says_how_to_find_it",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            # ⚠️ BOTH guards are reverted together, and the first attempt here
            # got this wrong: reverting only the one in execute() left the test
            # PASSING, because a protected DIRECTORY is caught again by the
            # folder guard in _open (for a directory the two see the same
            # path). That is the harness's own rule — remove the GUARANTEE, not
            # one of several copies of it. Case 5 then proves the second copy
            # is independently load-bearing for the case the first cannot see.
            "4. a protected system directory is refused (the guarantee, both copies)",
            "app/tools/file_tools.py",
            [("        if reason := _blocked_reason(path):\n"
              "            return _fail(self, reason)\n"
              "        return await asyncio.to_thread(self._open, path)",
              "        if False:\n"
              "            return _fail(self, reason)\n"
              "        return await asyncio.to_thread(self._open, path)"),
             ("        if reason := _blocked_reason(folder):", "        if False:")],
            f"{T}::test_a_protected_system_directory_is_refused",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            "5. the folder we would actually open is guarded too",
            "app/tools/file_tools.py",
            [("        if reason := _blocked_reason(folder):", "        if False:")],
            f"{T}::test_the_folder_the_window_would_open_is_guarded_too",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            # ⚠️ The first version of the paired test only asserted "did not
            # raise", and PASSED against `raise` — because `safe_execute`
            # catches everything anyway. The handler's real value is the
            # message, so that is what the test now pins.
            "6. a launcher failure names the folder rather than the generic wrapper",
            "app/tools/file_tools.py",
            [("            return _fail(self, f\"Could not open '{folder}': {type(e).__name__}: {e}\")",
              "            raise")],
            f"{T}::test_a_launcher_failure_names_the_folder_it_could_not_open",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            "7. WRITE level — the approval gate applies",
            "app/tools/file_tools.py",
            [("        # WRITE, not DESTRUCTIVE: it puts a window on screen showing files the\n"
              "        # user already has access to, and changes nothing. It is not READ\n"
              "        # either — it acts on the world outside the chat, so it passes the\n"
              "        # approval gate like any other write.\n"
              "        return PermissionLevel.WRITE",
              "        return PermissionLevel.READ")],
            f"{T}::test_unapproved_open_is_structurally_blocked",
            f"{T}::test_a_directory_opens_itself",
        ),
        (
            "8. the pre-flight guard keeps a guessed folder off the approval card",
            "app/agents/planner.py",
            [('    "open_folder": "path",\n', "")],
            f"{T}::test_a_guessed_folder_never_reaches_the_approval_card",
            f"{T}::test_approving_the_card_opens_exactly_that_folder",
        ),
        (
            "9. the which-drive guard covers it (registry-walking coverage test)",
            "app/agents/folder_resolver.py",
            [('    "open_folder": ("path", "self"),\n', "")],
            "tests/test_folder_resolver.py::test_every_path_param_is_covered_or_exempt",
            f"{T}::test_the_pre_flight_guard_requires_the_path_to_exist",
        ),
        (
            "10. it has a spoken form (registry-walking coverage test)",
            "app/agents/spoken.py",
            [('    "open_folder": lambda p: f"open the {_base(p.get(\'path\'))} folder on screen",\n',
              "")],
            "tests/test_spoken_approval.py::test_every_non_read_tool_has_a_spoken_form",
            f"{T}::test_registered_as_a_write_tool",
        ),
        (
            "11. the agent that gets the goal can actually see the tool",
            "app/agents/agent_registry.py",
            [('         "delete_file", "delete_files", "open_folder", "run_command", "execute_script"},',
              '         "delete_file", "delete_files", "run_command", "execute_script"},')],
            f"{T}::test_both_agents_that_can_be_asked_to_open_a_folder_can_see_it",
            f"{T}::test_registered_as_a_write_tool",
        ),
        (
            "12. plan rule 25 no longer steers a folder to the shell",
            "app/agents/planner.py",
            [("To open a FOLDER, or to show the user where a file lives, use open_folder (rule 9) — NOT launch_app, and never a shell command.",
              "use run_command for anything else.")],
            f"{T}::test_the_plan_rules_point_at_the_tool_and_no_longer_at_the_shell",
            f"{T}::test_registered_as_a_write_tool",
        ),
        (
            "13. the approval card names the folder",
            "app/agents/planner.py",
            [('    if tool == "open_folder":\n'
              '        return f"open in the file explorer: {p(\'path\')}"\n', "")],
            f"{T}::test_the_approval_card_names_the_folder",
            f"{T}::test_registered_as_a_write_tool",
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
