"""Falsification harness for the 2026-08-07 round-2 season-catalog work.

The house rule: a test that does not FAIL when the change it covers is reverted
is not evidence. Every behavioural change below is reverted IN PLACE (never
`git show :file` — this tree carries a large uncommitted baseline, so the index
is not "the code before this change"), the behavioural test must FAIL, and a
REGRESSION test that does not depend on the reverted line must still PASS.

Every lesson this project has recorded about lying harnesses is encoded as a
check rather than as a comment:

  * anchors must be WHOLE LINES and must appear EXACTLY ONCE (a substring anchor
    once matched a more-indented line and produced an IndentationError, which
    fails every test for the wrong reason and reads exactly like a pass)
  * the patched file is re-read from disk and the revert CONFIRMED before any
    result is trusted (three falsifications have come back green here without
    the edit having landed)
  * pytest's EXIT CODE is read, never its output — code 5 means NOTHING WAS
    COLLECTED, i.e. a mistyped node id, which scores identically to a failure
  * the restore happens in a `finally`, so an abort can never leave the tree
    holding a reverted line
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "venv" / "Scripts" / "python.exe"

LOOP = "app/browser/loop.py"
SEASON = "app/browser/season.py"
API = "app/browser/series_api.py"
TOOLS = "app/tools/browser_agent_tools.py"

CATALOG = "tests/test_browse_season_catalog.py"
SERIES = "tests/test_series_api.py"


# (name, file, [(anchor, replacement), ...], behavioural node, regression node)
CASES = [
    (
        "a season goal does not fetch an absolute episode number",
        LOOP,
        [("    if latest_title and not wants_season:", "    if latest_title:")],
        f"{CATALOG}::test_the_second_incident_a_season_goal_never_jumps_to_the_original",
        f"{CATALOG}::test_an_unscoped_latest_episode_goal_still_uses_the_web_number",
    ),
    (
        "the belt blocks a series jump while a season is unreached",
        LOOP,
        [("            and not hold_for_season", "            and True")],
        f"{CATALOG}::test_the_belt_blocks_a_jump_when_the_page_supplies_the_number",
        f"{CATALOG}::test_an_unscoped_latest_episode_goal_still_uses_the_web_number",
    ),
    (
        "the hold releases when no catalog knows the series",
        LOOP,
        [
            (
                "            and not (season_done and season_hint is None)",
                "            and True",
            )
        ],
        f"{CATALOG}::test_the_hold_releases_when_no_catalog_knows_the_series",
        f"{CATALOG}::test_the_belt_blocks_a_jump_when_the_page_supplies_the_number",
    ),
    (
        "the catalog is consulted before the prose read",
        SEASON,
        [("        facts = await series_api.resolve_season(title)", "        facts = None")],
        f"{CATALOG}::test_the_catalog_answers_with_no_provider_call",
        f"{CATALOG}::test_the_prose_read_is_still_the_fallback",
    ),
    (
        "cancel_background_lookups is wired into the teardown",
        TOOLS,
        [
            (
                "                    browser_loop.cancel_background_lookups(session)",
                "                    pass",
            )
        ],
        f"{CATALOG}::test_the_cancel_is_wired_into_the_teardown_path",
        f"{CATALOG}::test_background_lookups_are_cancelled_before_the_provider_closes",
    ),
    (
        "the cleanup survives a descriptor that raises",
        LOOP,
        [
            (
                "        try:\n"
                "            task = getattr(session, attr, None)\n"
                "        except Exception:\n"
                "            continue",
                "        task = getattr(session, attr, None)",
            )
        ],
        f"{CATALOG}::test_cancelling_lookups_never_raises",
        f"{CATALOG}::test_background_lookups_are_cancelled_before_the_provider_closes",
    ),
    (
        "an unaired season is never the latest season",
        API,
        [
            (
                'WATCHABLE = frozenset({"RELEASING", "FINISHED"})',
                'WATCHABLE = frozenset({"RELEASING", "FINISHED", "NOT_YET_RELEASED"})',
            )
        ],
        f"{SERIES}::test_an_unaired_season_is_never_the_latest_season",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
    (
        "a companion release is never a season",
        API,
        [
            (
                'SEASON_FORMATS = frozenset({"TV", "TV_SHORT"})',
                'SEASON_FORMATS = frozenset({"TV", "TV_SHORT", "MOVIE", "ONA"})',
            )
        ],
        f"{SERIES}::test_a_companion_release_is_never_a_season",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
    (
        "the match floor refuses a live-action query",
        API,
        [
            (
                "    if best_row is None or best_score < MATCH_FLOOR:",
                "    if best_row is None:",
            )
        ],
        f"{SERIES}::test_the_match_floor_alone_refuses_an_unrelated_show",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
    (
        "the franchise guard rejects an unrelated releasing row",
        API,
        [
            (
                "        and _same_franchise(series_title, _row_title(row))",
                "        and True",
            )
        ],
        f"{SERIES}::test_an_unrelated_releasing_row_cannot_win",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
    (
        "an airing season outranks a later start date",
        API,
        [("    pool = releasing or candidates", "    pool = candidates")],
        f"{SERIES}::test_releasing_wins_over_a_newer_start_date",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
    (
        "the planned total is never reported as the aired count",
        API,
        [
            (
                "    if isinstance(nxt, int) and not isinstance(nxt, bool):",
                "    if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt > 1:",
            ),
            ("        return nxt - 1 if nxt > 1 else None", "        return nxt - 1"),
        ],
        f"{SERIES}::test_a_first_episode_still_airing_reports_no_episode",
        f"{SERIES}::test_the_incident_resolves_to_the_calamity",
    ),
]


def read(path: Path) -> str:
    return io.open(path, encoding="utf-8").read()


def write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def run(node: str) -> tuple[bool, int]:
    """(passed, exit code). Exit 5 = nothing collected — never a pass."""
    proc = subprocess.run(
        [str(PY), "-m", "pytest", node, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.returncode


def main() -> int:
    valid = 0
    invalid = 0

    for name, rel, edits, behavioural, regression in CASES:
        path = ROOT / rel
        original = read(path)
        patched = original

        problem = None
        for anchor, replacement in edits:
            count = patched.count(anchor)
            if count != 1:
                problem = f"anchor appears {count} times (must be exactly 1): {anchor[:70]!r}"
                break
            patched = patched.replace(anchor, replacement)

        if problem:
            print(f"[INVALID] {name}\n           {problem}")
            invalid += 1
            continue

        try:
            write(path, patched)

            # The revert must be ON DISK before any result is trusted.
            on_disk = read(path)
            landed = all(
                on_disk.count(new) >= 1 and old not in on_disk for old, new in edits
            )
            if not landed:
                # An ADDITIVE revert legitimately leaves the old text present
                # (adding a member to a set); accept it when the new text landed.
                landed = all(new in on_disk for _, new in edits)
            if not landed:
                print(f"[INVALID] {name}\n           revert did not land on disk")
                invalid += 1
                continue

            b_pass, b_code = run(behavioural)
            r_pass, r_code = run(regression)

            if b_code == 5 or r_code == 5:
                print(f"[INVALID] {name}\n           pytest collected nothing (bad node id)")
                invalid += 1
            elif b_pass:
                print(
                    f"[INVALID] {name}\n"
                    f"           the behavioural test PASSED on reverted code — it\n"
                    f"           does not reach the guarantee it claims to cover"
                )
                invalid += 1
            elif not r_pass:
                print(
                    f"[INVALID] {name}\n"
                    f"           the regression test ALSO failed — it depends on the\n"
                    f"           reverted line, so it is a second behavioural test"
                )
                invalid += 1
            else:
                print(f"[VALID]   {name}")
                valid += 1
        finally:
            write(path, original)
            assert read(path) == original, f"FAILED TO RESTORE {rel}"

    print(f"\n{valid} valid / {invalid} invalid, out of {len(CASES)}")
    return 0 if invalid == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
