"""Falsification harness for the 2026-08-08 item-choice round.

THE HOUSE RULE: every behavioural change is proven to FAIL when the specific line
that implements it is reverted IN PLACE — never `git show :file`, because this
tree carries a large uncommitted baseline.

⚠️ THE MACHINERY IS IMPORTED, NOT COPIED. `_falsify_modal_wall.py` already
encodes every lesson this project has paid for (unique whole-line anchors, the
revert verified on disk, pytest's EXIT CODE read, restore in a `finally`), and a
second copy of it is the same second-copy-of-a-fact hole the codebase has
recorded seven times — the copy that drifts is the one that stops checking.

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_item_choice.py [case-id]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

L = "app/browser/loop.py"
C = "app/browser/choice.py"
ST = "app/browser/state.py"
P = "app/agents/planner.py"

T = "tests/test_browse_item_choice.py"

# The two that must survive every revert: one exercises the pre-decision leg, the
# other the stall fallback, so whichever half a case removes, the other is the
# regression proving the rest of the feature still stands.
INCIDENT = f"{T}::test_the_incident_a_stalled_decision_asks_instead_of_dying"
EARLY = f"{T}::test_the_tie_is_raised_without_spending_a_decision_on_it"

CASES: list[Case] = [
    Case(
        id="pre-decision-leg",
        why="Without it the tie is only noticed AFTER a decision is bought — the "
            "guess the whole feature exists to avoid asking for.",
        edits=[
            Edit(
                path=L,
                new="            and commit\n            and step > 0\n        ):",
                old="            and commit\n            and False  # FALSIFICATION\n        ):",
            )
        ],
        behavioural=EARLY,
        regression=INCIDENT,
    ),
    Case(
        id="stall-fallback",
        why="THE INCIDENT ITSELF: the model returned nothing twice and the browse "
            "died 'couldn't work out a safe next action', closing the window on a "
            "page holding twenty equal matches.",
        edits=[
            Edit(
                path=L,
                new="                # Deliberately NOT gated on step: the pre-decision leg's job is\n"
                    "                # to be early, this one's is to catch everything it cannot.\n"
                    "                if commit:",
                old="                # FALSIFICATION: the recall net is removed.\n"
                    "                if False:",
            )
        ],
        behavioural=INCIDENT,
        regression=EARLY,
    ),
    Case(
        id="page-is-the-target-suppression",
        why="The 2026-08-02b rule must survive the new leg: on a product page the "
            "tie is a 'you may also like' rail, and asking about it re-opens a "
            "question the user already answered.",
        edits=[
            Edit(
                path=L,
                new="            if len(tied_all) > 1 and not choice.page_is_the_target(\n"
                    "                target_words, obs.title, obs.url, tied_all\n"
                    "            ):",
                old="            if len(tied_all) > 1:  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_the_page_that_IS_the_product_still_acts_rather_than_re_asking",
        regression=EARLY,
    ),
    Case(
        id="shown-cap-8",
        why="At four, the user's own requirement — show BOTH janan sports 100ml "
            "and 200ml — cannot be expressed for more than one family.",
        edits=[Edit(path=C, new="MAX_CHOICE_OPTIONS = 8", old="MAX_CHOICE_OPTIONS = 4")],
        behavioural=f"{T}::test_the_incident_reports_how_many_there_really_were",
        regression=INCIDENT,
    ),
    Case(
        id="tied-matches-untruncated",
        why="If the whole tie is not available, the caller cannot report a total "
            "and a shortened list silently reads as the complete answer.",
        edits=[
            Edit(
                path=C,
                new="    return tied\n\n\ndef tied_candidates(",
                old="    return tied[:MAX_CHOICE_OPTIONS]\n\n\ndef tied_candidates(",
            )
        ],
        behavioural=f"{T}::test_tied_matches_returns_the_whole_tie_and_tied_candidates_caps_it",
        regression=INCIDENT,
    ),
    Case(
        id="total-stamped",
        why="The count is what makes a truncated question honest.",
        edits=[
            Edit(
                path=L,
                new="    out.choice_total = len(tied_all)",
                old="    out.choice_total = 0  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_the_incident_reports_how_many_there_really_were",
        regression=INCIDENT,
    ),
    Case(
        id="total-reaches-the-payload",
        why="A pause parks the plan, so a count that does not ride the payload is "
            "lost by the time the question is rendered.",
        edits=[
            Edit(
                path=ST,
                new='            choice_total=int(getattr(outcome, "choice_total", 0) or 0),\n',
                old="",
            )
        ],
        behavioural=f"{T}::test_the_total_rides_the_handoff_payload",
        regression=f"{T}::test_a_payload_parked_before_this_field_still_deserializes",
    ),
    Case(
        id="total-survives-serialization",
        why="Same reason one layer down: to_dict/from_dict is how a parked pause "
            "gets back to the question builder.",
        edits=[
            Edit(
                path=ST,
                new='            "choice_total": self.choice_total,\n',
                old="",
            )
        ],
        behavioural=f"{T}::test_the_total_rides_the_handoff_payload",
        regression=INCIDENT,
    ),
    Case(
        id="question-says-the-total",
        why="Eight buttons under '8 things match' is indistinguishable from the "
            "complete answer when twenty matched — the 'record lied' failure.",
        edits=[
            Edit(
                path=P,
                new="        if total > len(options):",
                old="        if False:  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_the_question_says_how_many_it_is_not_showing",
        regression=f"{T}::test_a_tie_that_fits_reads_exactly_as_it_did",
    ),
]


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in CASES if not wanted or c.id == wanted]
    if not cases:
        print(f"no case matching {wanted!r}. Known: {', '.join(c.id for c in CASES)}")
        return 2
    results = [(c.id, run(c)) for c in cases]
    print("\n" + "=" * 60)
    for cid, ok in results:
        print(f"  {'VALID  ' if ok else 'INVALID'}  {cid}")
    bad = [cid for cid, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} valid")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
