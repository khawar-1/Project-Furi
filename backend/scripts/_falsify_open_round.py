"""
Falsification harness — "open donwloads" / "open folder 'fomi'" (2026-08-06).

A test that passes on the broken code is not a regression test. For every
behavioural change this reverts the specific line IN PLACE, re-runs the test
that is supposed to defend it, and requires it to FAIL — then restores and
requires it to pass again.

The runner is `_falsify_perf.py`'s, and every check in it exists because that
exact mistake produced a wrong verdict in this project before: never
`git show :file` (the tree carries a large uncommitted baseline); the anchor
must be a UNIQUE whole line including indentation; VERIFY the revert landed on
disk; read pytest's EXIT CODE (5 = nothing collected is never a pass); restore
in a `finally`; and a green result means the test cannot reach the change, or
the guarantee has a second copy — suspect the harness first.

Run from backend/:  venv\\Scripts\\python -u scripts\\_falsify_open_round.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BACKEND = Path(__file__).resolve().parent.parent
T = "tests/test_open_folder.py"

CASES = [
    {
        "name": "opening a folder needs no approval (READ, not WRITE)",
        "file": "app/tools/file_tools.py",
        # The bare `return PermissionLevel.READ` is NOT unique — three other
        # file tools return it — so the anchor carries the comment above it.
        "edits": [(
            '        # "shows you something you already have" vs "runs code".\n'
            "        return PermissionLevel.READ\n",
            '        # "shows you something you already have" vs "runs code".\n'
            "        return PermissionLevel.WRITE\n",
        )],
        "behavioural": f"{T}::test_opening_a_folder_asks_for_no_approval",
        # The never-execute invariant passes `approved=True`, so it holds at
        # either permission level — it defends a different property.
        "regression": f"{T}::test_a_file_opens_its_containing_folder_never_the_file",
    },
    {
        "name": "the placeholder resolver dispatches open_folder",
        "file": "app/agents/placeholder_resolver.py",
        "edits": [(
            "        if _OPEN_TARGET_PARAMS.get(template.tool) == key:\n"
            "            return _substitute_open_target(template, key, completed)\n",
            "        if False:\n"
            "            return _substitute_open_target(template, key, completed)\n",
        )],
        "behavioural": f"{T}::test_the_fomi_placeholder_resolves_in_code",
        # search_files' own directory placeholder goes through _DIR_PARAMS and
        # must be untouched — this change adds a branch, it moves none.
        "regression": (
            "tests/test_placeholder_resolver.py::"
            "test_folder_placeholder_resolves_when_the_name_pins_one_candidate"
        ),
    },
    {
        "name": "a pre-flight-guarded param must have a placeholder map",
        "file": "app/agents/placeholder_resolver.py",
        "edits": [(
            '_OPEN_TARGET_PARAMS = {"open_folder": "path"}\n',
            "_OPEN_TARGET_PARAMS = {}\n",
        )],
        "behavioural": f"{T}::test_the_placeholder_resolver_knows_the_tool",
        "regression": f"{T}::test_registered_as_a_read_tool",
    },
    {
        "name": "'show me where this file lives' resolves from a file hit",
        "file": "app/agents/placeholder_resolver.py",
        "edits": [(
            "    source = next((s for s in reversed(completed) if paths_from_step(s)[0]), None)\n",
            "    source = None\n",
        )],
        "behavioural": f"{T}::test_a_file_resolves_so_show_me_where_this_lives_also_works",
        "regression": f"{T}::test_the_fomi_placeholder_resolves_in_code",
    },
    {
        "name": "a file is never guessed while a folder was also found",
        "file": "app/agents/placeholder_resolver.py",
        "edits": [(
            "    if any(paths_from_step(s)[1] for s in completed):\n",
            "    if False:\n",
        )],
        "behavioural": f"{T}::test_a_file_is_never_guessed_while_a_folder_was_also_found",
        "regression": f"{T}::test_a_file_resolves_so_show_me_where_this_lives_also_works",
    },
    {
        "name": "a typo still triggers the which-drive question",
        "file": "app/agents/folder_resolver.py",
        "edits": [(
            "    return any(\n"
            "        fuzz.ratio(token.lower(), lname) >= _TYPO_FLOOR\n"
            "        for token in _WORD_RE.findall(corpus)\n"
            "    )\n",
            "    return False\n",
        )],
        "behavioural": f"{T}::test_a_typo_still_asks_which_downloads",
        # The exact spelling must keep working — the fuzzy pass is a FALLBACK
        # behind the word-boundary check, not a replacement for it.
        "regression": f"{T}::test_the_ways_people_type_it[open downloads]",
    },
    {
        "name": "the floor is high enough that short names get no tolerance",
        "file": "app/agents/folder_resolver.py",
        "edits": [("_TYPO_FLOOR = 84.0\n", "_TYPO_FLOOR = 70.0\n")],
        "behavioural": f"{T}::test_the_floor_self_scales_with_name_length",
        # 'donwloads' scores 88.9, so the incident still asks at a lower floor —
        # what a lower floor breaks is `dogs`/`docs`, which is the other test.
        "regression": f"{T}::test_a_typo_still_asks_which_downloads",
    },
    {
        "name": "the mode rule names opening a folder as a quick read",
        "file": "app/api/task_router.py",
        "edits": [(
            '"list my downloads", "open a folder on screen", "any new emails?"',
            '"list my downloads", "any new emails?"',
        )],
        "behavioural": f"{T}::test_the_mode_rule_names_opening_a_folder_as_a_quick_read",
        "regression": f"{T}::test_the_plan_rules_point_at_the_tool_and_no_longer_at_the_shell",
    },
]


def run_test(nodeid: str) -> tuple[bool, str]:
    """(passed, note). Exit code 5 = nothing collected, which is never a pass."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, "-q", "--no-header", "-x"],
        cwd=BACKEND, capture_output=True, text=True,
    )
    if proc.returncode == 5:
        return False, "NO TESTS COLLECTED (bad node id) - result is meaningless"
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1:] or [""]
    return proc.returncode == 0, tail[0].strip()[:90]


def apply(path: Path, edits, forward: bool) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        src, dst = (old, new) if forward else (new, old)
        count = text.count(src)
        if count != 1:
            raise SystemExit(
                f"ANCHOR NOT UNIQUE in {path.name}: found {count} occurrences of\n"
                f"  {src!r}\nAn ambiguous anchor patches the wrong line."
            )
        text = text.replace(src, dst)
    path.write_text(text, encoding="utf-8")


def landed(path: Path, edits, forward: bool) -> bool:
    """Confirm the edit is really on disk. Never trust a result without this."""
    text = path.read_text(encoding="utf-8")
    return all((new in text) if forward else (old in text) for old, new in edits)


def main() -> int:
    failures = 0
    for case in CASES:
        path = BACKEND / case["file"]
        print(f"\n=== {case['name']} ===")
        original = path.read_text(encoding="utf-8")
        try:
            apply(path, case["edits"], forward=True)
            if not landed(path, case["edits"], forward=True):
                print("  REVERT DID NOT LAND - result would be meaningless")
                failures += 1
                continue

            ok_b, note_b = run_test(case["behavioural"])
            ok_r, note_r = run_test(case["regression"])

            print(f"  behavioural : {'PASSED (BAD)' if ok_b else 'failed (good)'}  {note_b}")
            print(f"  regression  : {'passed (good)' if ok_r else 'FAILED (BAD)'}  {note_r}")

            if ok_b:
                print("  -> INVALID: the test passes on the broken code. It cannot")
                print("     reach the change, or the guarantee has a second copy.")
                failures += 1
            elif not ok_r:
                print("  -> INVALID: the regression test failed too, so the revert")
                print("     broke something unrelated and proves nothing.")
                failures += 1
            else:
                print("  -> VALID falsification")
        finally:
            path.write_text(original, encoding="utf-8")
            if path.read_text(encoding="utf-8") != original:
                print("  !! RESTORE FAILED - fix the tree by hand before continuing")
                return 2

    print(f"\n{len(CASES) - failures}/{len(CASES)} valid falsifications")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
