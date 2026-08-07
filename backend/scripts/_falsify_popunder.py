"""Falsification for the 2026-08-07 round-3 pop-under work.

THE HOUSE RULE: every behavioural change must be proven to FAIL when the
specific line is reverted IN PLACE. Never `git show :file` — this tree carries a
large uncommitted baseline, so the indexed copy is not "the code before this
change" and a test failing on a missing import proves nothing.

The recorded harness lessons, encoded as CHECKS rather than intentions:
  * anchors must be UNIQUE whole lines (a non-unique anchor patches the wrong
    branch and the run is silently meaningless)
  * the revert is VERIFIED ON DISK before the result is trusted (three lying
    falsifications in this project so far)
  * pytest's EXIT CODE is read — 5 means nothing was collected, which scores
    identically to "failed" if you only look at the text
  * the restore runs in a `finally` and is asserted, because a harness that
    edits source and dies mid-edit becomes the bug it is hunting
  * every case names a REGRESSION test that must keep PASSING, or a revert that
    simply breaks the file would look like a successful falsification

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_popunder.py
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print = functools.partial(print, flush=True)  # noqa: A001

POPUNDER = "tests/test_browse_popunder.py"
SESSION = "tests/test_browser_session.py"


CASES = [
    {
        "name": "D1 the take-over waits for a real URL",
        "why": "Collapsing 'unknown' back into an immediate take-over IS the incident.",
        "edits": [(
            "app/browser/session.py",
            "        if verdict == \"unknown\":\n            self._defer_take_over(page)\n            return\n",
            "",
        )],
        "behavioural": f"{POPUNDER}::test_the_incident_a_blank_popunder_never_costs_us_the_page",
        "regression": f"{POPUNDER}::test_a_tab_already_on_an_ad_is_still_refused_outright",
    },
    {
        "name": "D1 'unknown' is a real third answer",
        "why": "If the verdict says 'allow' for a blank tab, the deferral never fires.",
        "edits": [(
            "app/browser/session.py",
            "            if not url or parsed.scheme in _LOCAL_SCHEMES:\n                return \"unknown\"\n",
            "            if not url or parsed.scheme in _LOCAL_SCHEMES:\n                return \"allow\"\n",
        )],
        "behavioural": f"{POPUNDER}::test_the_incident_a_blank_popunder_never_costs_us_the_page",
        "regression": f"{POPUNDER}::test_a_tab_already_on_an_ad_is_still_refused_outright",
    },
    {
        "name": "D1 a resolved allowed tab IS followed",
        "why": "Waiting must not become never — the target=_blank flow depends on it.",
        "edits": [(
            "app/browser/session.py",
            "                if verdict == \"allow\":\n                    await self._take_over(page)\n                    return\n",
            "                if verdict == \"allow\":\n                    return\n",
        )],
        "behavioural": f"{POPUNDER}::test_a_blank_tab_that_lands_somewhere_allowed_is_still_followed",
        "regression": f"{POPUNDER}::test_the_incident_a_blank_popunder_never_costs_us_the_page",
    },
    {
        "name": "D1 a tab that resolves to an ad is closed",
        "why": "Leaving it would put an ad window on screen and leak a tab per popup.",
        "edits": [(
            "app/browser/session.py",
            "                if verdict == \"refuse\":\n                    try:\n                        await _maybe_await(page.close())\n                    except Exception as exc:\n                        logger.debug(f\"close refused tab: {type(exc).__name__}: {exc}\")\n                    return\n",
            "                if verdict == \"refuse\":\n                    return\n",
        )],
        "behavioural": f"{POPUNDER}::test_a_blank_tab_that_lands_on_an_ad_is_closed_and_we_stay_put",
        "regression": f"{POPUNDER}::test_a_tab_that_never_says_where_it_is_going_is_left_alone",
    },
    {
        "name": "D1 the guard goes on while we wait",
        "why": "An un-adopted ad tab would otherwise load for real — no SSRF check, no Rule 3.",
        "edits": [(
            "app/browser/session.py",
            "        try:\n            await self._install_interception(page)\n        except Exception as exc:\n            logger.debug(f\"adopt popup route: {type(exc).__name__}: {exc}\")\n        self._refuse_downloads(page)\n        if verdict == \"unknown\":\n",
            "        if verdict == \"unknown\":\n",
        )],
        "behavioural": f"{POPUNDER}::test_a_blank_tab_is_guarded_from_its_first_request",
        "regression": f"{POPUNDER}::test_the_incident_a_blank_popunder_never_costs_us_the_page",
    },
    {
        "name": "D1 close() cancels a pending take-over",
        "why": "A decision landing after teardown swaps to a tab on a dying context.",
        "edits": [(
            "app/browser/session.py",
            "        for task in list(self._deferred_adopts):\n            try:\n                task.cancel()\n            except Exception as exc:\n                logger.debug(f\"cancel deferred adopt: {type(exc).__name__}: {exc}\")\n        self._deferred_adopts.clear()\n",
            "",
        )],
        "behavioural": f"{POPUNDER}::test_closing_the_session_cancels_a_pending_take_over",
        "regression": f"{POPUNDER}::test_the_incident_a_blank_popunder_never_costs_us_the_page",
    },
    {
        "name": "D2 observe survives a navigation mid-read",
        "why": "This exact exception ended both live runs.",
        "edits": [(
            "app/browser/observe.py",
            "    raw = await _extract_top(page, observation_id)\n",
            "    raw = await page.evaluate(_EXTRACT_JS, {\"obsId\": observation_id, \"base\": 0})\n",
        )],
        "behavioural": f"{POPUNDER}::test_the_incident_a_navigation_mid_observe_is_survived",
        "regression": f"{POPUNDER}::test_a_healthy_page_costs_nothing_extra",
    },
    {
        "name": "D2 the retry is bounded to one",
        "why": "Unbounded, a genuinely dead page becomes invisible instead of reported.",
        "edits": [(
            "app/browser/observe.py",
            "    await asyncio.sleep(_OBSERVE_RETRY_MS / 1000.0)\n    return await page.evaluate(_EXTRACT_JS, {\"obsId\": observation_id, \"base\": 0})\n",
            "    while True:\n        await asyncio.sleep(_OBSERVE_RETRY_MS / 1000.0)\n        try:\n            return await page.evaluate(_EXTRACT_JS, {\"obsId\": observation_id, \"base\": 0})\n        except Exception:\n            continue\n",
        )],
        "behavioural": f"{POPUNDER}::test_a_page_that_is_genuinely_dead_still_says_so",
        "regression": f"{POPUNDER}::test_the_incident_a_navigation_mid_observe_is_survived",
    },
    {
        "name": "the old test was pinning the defect",
        "why": (
            "test_a_blank_new_tab_is_still_adopted asserted `session.page is blank` — "
            "the incident written down as an expectation. Its replacement must fail "
            "on the old behaviour."
        ),
        "edits": [(
            "app/browser/session.py",
            "        if verdict == \"unknown\":\n            self._defer_take_over(page)\n            return\n",
            "",
        )],
        "behavioural": f"{SESSION}::test_a_blank_new_tab_is_not_refused_but_does_not_take_the_page",
        "regression": f"{SESSION}::test_an_ad_popup_is_never_adopted",
    },
]


def run(node_id: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    # EXIT CODE, not the text. 5 = nothing collected, which reads like a pass in
    # the summary line and is actually "your node id is wrong".
    if proc.returncode == 5:
        return False, "NO TESTS COLLECTED (bad node id)"
    tail = (proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "no output")


def main() -> int:
    valid = 0
    invalid: list[str] = []

    for case in CASES:
        print(f"\n{'=' * 78}\n{case['name']}\n  {case['why']}")
        originals: list[tuple[Path, str]] = []
        landed = True

        try:
            for rel, old, new in case["edits"]:
                path = ROOT / rel
                text = path.read_text(encoding="utf-8")
                originals.append((path, text))
                count = text.count(old)
                if count != 1:
                    print(f"  ANCHOR NOT UNIQUE in {rel}: found {count}, need exactly 1")
                    landed = False
                    break
                path.write_text(text.replace(old, new), encoding="utf-8")
                # VERIFY ON DISK. An additive revert legitimately leaves `old`
                # present, so the check is "the file changed and now contains
                # what we asked for" rather than "old is gone".
                after = path.read_text(encoding="utf-8")
                if after == text or (new and new not in after):
                    print(f"  REVERT DID NOT LAND in {rel}")
                    landed = False
                    break

            if not landed:
                invalid.append(f"{case['name']}: revert did not land")
                continue

            beh_ok, beh_line = run(case["behavioural"])
            reg_ok, reg_line = run(case["regression"])

            if beh_line.endswith("(bad node id)") or reg_line.endswith("(bad node id)"):
                print(f"  INVALID — {beh_line} / {reg_line}")
                invalid.append(f"{case['name']}: bad node id")
            elif not beh_ok and reg_ok:
                print("  VALID — behavioural FAILS, regression PASSES")
                print(f"     behavioural: {beh_line}")
                valid += 1
            elif beh_ok:
                print("  INVALID — the behavioural test PASSED on the reverted code.")
                print("     Suspect the test's reach, or a second copy of the guarantee.")
                invalid.append(f"{case['name']}: behavioural passed when reverted")
            else:
                print("  INVALID — the regression test ALSO failed; the revert broke more")
                print(f"     than the guarantee under test. regression: {reg_line}")
                invalid.append(f"{case['name']}: regression also failed")
        finally:
            for path, text in originals:
                path.write_text(text, encoding="utf-8")
                assert path.read_text(encoding="utf-8") == text, f"RESTORE FAILED: {path}"

    print(f"\n{'=' * 78}\n{valid}/{len(CASES)} valid falsifications")
    for line in invalid:
        print(f"   INVALID: {line}")
    return 0 if valid == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
