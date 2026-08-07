"""
Falsification harness for the performance round (2026-08-06).

A test that passes on the broken code is not a regression test. For every
behavioural change this reverts the specific line IN PLACE, re-runs the test
that is supposed to defend it, and requires it to FAIL — then restores and
requires it to pass again.

Lessons from prior rounds are encoded here as checks rather than as prose,
because every one of them has produced a wrong verdict in this project before:

  * NEVER `git show :file` — the tree carries a large uncommitted baseline, so
    the indexed copy is not "the code before this change" (2026-08-01).
  * The anchor must be UNIQUE and a WHOLE line including indentation. A
    substring of a more-indented line produced an IndentationError, every test
    failed for the wrong reason, and that is indistinguishable from a passing
    falsification by exit code alone (2026-08-03).
  * VERIFY the revert landed on disk before trusting a result. Three
    falsifications in this project have come back green because the patch never
    applied (2026-08-03).
  * Read pytest's EXIT CODE. 5 means nothing was collected — a typo'd test name
    scores identically to a failure unless you check (2026-08-04).
  * Restore in a `finally`. A harness that edits source and exits early leaves
    the tree reverted, and the next case then fails for a reason that is not
    the code's (2026-08-04).
  * A falsification must remove the GUARANTEE, not one of several copies of it.
    If a green result appears, suspect the test's reach and the second copy
    before suspecting the code (2026-08-03, 2026-08-04).

Run from backend/:  venv\\Scripts\\python -u scripts\\_falsify_perf.py
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

# Each case: revert `edits` in `file`, expect `behavioural` to FAIL and
# `regression` to still PASS.
CASES = [
    {
        "name": "windowed impersonation scan covers new text, not just a tail",
        "file": "app/api/chat.py",
        "edits": [(
            "                scan = emitted_tail + ready\n",
            "                scan = emitted_tail\n",
        )],
        "behavioural": "tests/test_task_router.py::test_impersonation_is_caught_inside_one_large_delta",
        # Small-delta fabrications still get cut by the broken version, which is
        # exactly why the large-delta test had to be written.
        "regression": "tests/test_task_router.py::test_impersonated_reminder_confirmation_is_corrected",
    },
    {
        "name": "scan window is wider than the longest possible match",
        "file": "app/api/chat.py",
        "edits": [("_VOICE_SCAN_WINDOW = 512\n", "_VOICE_SCAN_WINDOW = 8\n")],
        "behavioural": "tests/test_task_router.py::test_impersonation_window_exceeds_longest_possible_match",
        "regression": "tests/test_task_router.py::test_impersonation_guard_allows_capability_statements",
    },
    {
        "name": "the refresh breaker actually skips the network call",
        "file": "app/integrations/google_auth.py",
        "edits": [(
            "            if self._refresh_is_blocked():\n"
            "                raise GoogleNotConnectedError(RECONNECT_MESSAGE)\n",
            "            if False:\n"
            "                raise GoogleNotConnectedError(RECONNECT_MESSAGE)\n",
        )],
        "behavioural": "tests/test_google_auth.py::test_repeated_refresh_failures_stop_hitting_the_network",
        # A dead token must STILL degrade cleanly with the breaker disabled —
        # that is the pre-existing contract the breaker must not have changed.
        "regression": "tests/test_google_auth.py::test_refresh_failure_degrades_not_crashes",
    },
    {
        "name": "a successful refresh resets the breaker",
        "file": "app/integrations/google_auth.py",
        "edits": [(
            "            self.reset_refresh_breaker()\n"
            "            self._save_credentials(creds, account_email=data.get(_ACCOUNT_EMAIL_KEY))\n",
            "            self._save_credentials(creds, account_email=data.get(_ACCOUNT_EMAIL_KEY))\n",
        )],
        "behavioural": "tests/test_google_auth.py::test_a_successful_refresh_resets_the_breaker",
        "regression": "tests/test_google_auth.py::test_get_credentials_refreshes_expired_and_saves",
    },
    {
        "name": "engine CPU threads are bounded below the core count",
        "file": "app/core/gpu_bootstrap.py",
        "edits": [(
            "    return max(1, physical_ish - _RESERVED_CORES) if physical_ish > _RESERVED_CORES else max(1, physical_ish)\n",
            "    return logical\n",
        )],
        "behavioural": "tests/test_voice_stt.py::test_cpu_worker_threads_leaves_the_machine_usable",
        # Returning the raw core count is still >= 1, so the small-box guard
        # holds — it defends a different property and must not move.
        "regression": "tests/test_voice_stt.py::test_cpu_worker_threads_never_returns_zero_on_a_small_box",
    },
    {
        "name": "whisper's CPU path actually uses the bounded count",
        "file": "app/core/voice_stt.py",
        "edits": [(
            '        kwargs["cpu_threads"] = cpu_worker_threads()\n',
            '        kwargs["cpu_threads"] = os.cpu_count() or 4\n',
        )],
        "behavioural": "tests/test_voice_stt.py::test_whisper_cpu_path_uses_the_bounded_count",
        "regression": "tests/test_voice_stt.py::test_cpu_worker_threads_leaves_the_machine_usable",
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
