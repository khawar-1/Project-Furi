"""Falsification harness for the 2026-08-11 "open <site>" round.

THE HOUSE RULE: every behavioural change is proven to FAIL when the specific line
that implements it is reverted IN PLACE — never `git show :file`, because this
tree carries a large uncommitted baseline.

⚠️ THE MACHINERY IS IMPORTED, NOT COPIED. `_falsify_modal_wall.py` already
encodes every lesson this project has paid for (unique whole-line anchors, the
revert verified on disk, pytest's EXIT CODE read, restore in a `finally`), and a
second copy of it is the same second-copy-of-a-fact hole the codebase has
recorded seven times.

NOTE THE TWO SIGNATURES. Cases 1/3/4 remove a guarantee, so the INCIDENT test
must fail. Case 2 removes the guard's NARROWNESS, so the opposite test fails —
the feeder chain — while the incident stays caught. A round that only ever
falsifies in one direction has not tested the boundary, only the middle.

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_open_url.py [case-id]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

P = "app/agents/planner.py"
T = "tests/test_browse_open_site.py"

INCIDENT = f"{T}::test_the_incident_a_read_webpage_only_browse_plan_is_rejected"
# The guard's narrowness: a read-only web tool used ALONGSIDE the browser must
# keep planning (_collapse_browse_apply exists to fold exactly such a step in).
FEEDER = f"{T}::test_a_web_search_feeding_a_browse_is_not_a_substitution"
RULE20 = f"{T}::test_rule_20_no_longer_claims_the_word_open"
RULE21 = f"{T}::test_rule_21_owns_the_bare_navigation_case"

CASES: list[Case] = [
    Case(
        id="guard-wired",
        why="The guard is IN the reject chain. Unwire it and the planner never "
            "consults it — the pre-fix state, and the 2026-07-17/08-03 failure "
            "mode where a unit-tested predicate had never once fired.",
        # The WHOLE tuple entry, both lines: dropping only the lambda leaves a
        # dangling "(GUARD_X," and the revert is a SyntaxError, which fails every
        # test for the wrong reason and is indistinguishable from a real result.
        edits=[Edit(
            path=P,
            old="",
            new="                            (plan_trace.GUARD_BROWSE_SUBSTITUTION,\n"
                "                             lambda: _browse_substitution(steps, self.agent.key)),\n",
        )],
        behavioural=INCIDENT,
        regression=FEEDER,
    ),
    Case(
        id="narrowness",
        why="INVERSE SIGNATURE. The _has_browse_action early-out is what keeps a "
            "feeder chain (web_search -> browse) planning. Remove it and the "
            "guard over-blocks: the FEEDER test fails while the incident is "
            "still caught.",
        edits=[Edit(
            path=P,
            # `old` keeps the statement that FOLLOWS the removed guard — a revert
            # that also eats it is a SyntaxError, not a falsification.
            old="    swapped_in =",
            new="    if _has_browse_action(steps):\n        return None\n    swapped_in =",
        )],
        behavioural=FEEDER,
        regression=INCIDENT,
    ),
    Case(
        id="rule-20-wording",
        why="Rule 20's 'the DEFAULT way to open a URL' is the sentence the model "
            "followed live. Belt, not cause — but the contract test must be able "
            "to see it come back.",
        edits=[Edit(
            path=P,
            old="20. read_webpage is the DEFAULT way to open a URL:",
            new="20. read_webpage is the DEFAULT way to READ THE CONTENT of a URL — an article, a docs page, a listing you need the text of:",
        )],
        behavioural=RULE20,
        regression=INCIDENT,
    ),
    Case(
        id="rule-21-wording",
        why="Rule 20's absence only helps if rule 21 OWNS the bare-navigation "
            "case; otherwise 'open <site>' is left unclaimed by any rule.",
        edits=[Edit(
            path=P,
            old="",
            new=" Putting a site ON SCREEN is browse too:",
        )],
        behavioural=RULE21,
        regression=INCIDENT,
    ),
]


if __name__ == "__main__":
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in CASES if not wanted or c.id == wanted]
    if not cases:
        print(f"no such case: {wanted}")
        raise SystemExit(2)
    ok = all(run(c) for c in cases)
    print(f"\n{'ALL VALID' if ok else 'SOME INVALID'} — {len(cases)} case(s)")
    raise SystemExit(0 if ok else 1)
