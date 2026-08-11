"""Falsification harness for the 2026-08-09 e-commerce round.

THE HOUSE RULE: every behavioural change is proven to FAIL when the specific line
that implements it is reverted IN PLACE — never `git show :file`, because this
tree carries a large uncommitted baseline.

⚠️ THE MACHINERY IS IMPORTED, NOT COPIED, for the reason `_falsify_item_choice`
already records: `_falsify_modal_wall` encodes every lesson this project has paid
for (unique whole-line anchors, the revert verified on disk, pytest's EXIT CODE
read — 5 means nothing collected and is never a pass — and a restore in a
`finally`), and a second copy is the same second-copy-of-a-fact hole recorded
seven times.

A case must remove the GUARANTEE, not one of several copies of it, and its
`regression` twin must PASS on the reverted tree — a "regression" that also fails
proves nothing.

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_ecommerce_round.py [case-id]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

L = "app/browser/loop.py"
C = "app/browser/choice.py"
O = "app/browser/observe.py"
ST = "app/browser/state.py"
CF = "app/browser/commit_flow.py"
P = "app/agents/planner.py"

T = "tests/test_browse_stock_and_stuck.py"

# Two independent survivors, so whichever half a case removes the other stands as
# the proof that the rest of the round still works.
STOCK = f"{T}::test_the_incident_sold_out_items_are_not_offered"
STUCK = f"{T}::test_the_incident_a_dead_end_asks_instead_of_killing_the_run"

CASES: list[Case] = [
    # ------------------------------------------------------------------ stock
    Case(
        id="stock-filter",
        why="THE REPORTED DEFECT: six of the twenty things offered for 'janan' "
            "were sold out, so picking one sent the user to a page that cannot "
            "add it.",
        edits=[
            Edit(
                path=C,
                new="    buyable = tuple(c for c in tied_all if c.available)",
                old="    buyable = tuple(tied_all)  # FALSIFICATION: stock ignored",
            )
        ],
        behavioural=STOCK,
        regression=STUCK,
    ),
    Case(
        id="stock-flag-read",
        why="The Candidate must carry what the observation said. Without the read "
            "every item is 'available' and the filter above has nothing to act on.",
        edits=[
            Edit(
                path=C,
                new="            available=not bool(getattr(element, \"sold_out\", False)),",
                old="            available=True,  # FALSIFICATION: the page's signal is dropped",
            )
        ],
        behavioural=STOCK,
        regression=STUCK,
    ),
    # ⚠️ NO CASE FOR THE JS WALK, AND THAT IS NOT AN OVERSIGHT. `soldOutOf` runs
    # inside a real document; the hermetic suite's ScriptedPage.evaluate returns
    # a canned dict and never executes a line of it, so reverting the JS here
    # changes NOTHING a hermetic test can see — the first attempt at this case
    # came back "behavioural PASSED (BAD)" for exactly that reason, which is the
    # harness correctly refusing to certify a claim it cannot check.
    #
    # It is falsified in the place that can run it: tests/test_browser_extract_js.py
    # (opt-in, real Chromium, the measured card markup). Verified by hand
    # 2026-08-09 — replacing `sold_out: soldOutOf(el)` with `sold_out: false`
    # fails `test_a_sold_out_card_marks_only_its_own_product`, and shortening
    # the walk to the element itself fails it the same way.
    Case(
        id="one-in-stock-is-taken",
        why="unresolved_axis rule 3 one layer up. Without it a tie that stock "
            "narrows to ONE returns 'no question', which means 'let the model "
            "decide' — silently handing back the choice the user was about to be "
            "asked about.",
        edits=[
            Edit(
                path=C,
                new="    if len(buyable) == 1:\n        return ItemChoice(settled=buyable[0], dropped=dropped, all_tied=every)",
                old="    if False:  # FALSIFICATION\n        return ItemChoice(settled=buyable[0], dropped=dropped, all_tied=every)",
            )
        ],
        behavioural=f"{T}::test_one_item_left_in_stock_is_taken_not_asked_about",
        regression=STOCK,
    ),
    Case(
        id="all-sold-out-still-asks",
        why="Returning 'no question' when nothing can be bought hands the page to "
            "the model with every option dead.",
        edits=[
            Edit(
                path=C,
                new="        return ItemChoice(\n            tied=every, dropped=dropped, none_buyable=True, all_tied=every\n        )",
                old="        return None  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_when_nothing_can_be_bought_it_still_asks_and_says_so",
        regression=STOCK,
    ),
    Case(
        id="stock-never-filters-an-answered-pick",
        why="`locate` ENFORCES what the user said. Filtering inside candidates_of "
            "would silently ignore someone who deliberately picked a sold-out "
            "item — and it would look like the feature working.",
        edits=[
            # Anchored on the loop's OWN guard, so the revert genuinely removes
            # `new` from the file. Inserting a line ABOVE `key = _dedupe_key(...)`
            # leaves that line in place, and the harness rightly reports "REVERT
            # DID NOT LAND" rather than trusting the result.
            Edit(
                path=C,
                new="        if not _is_choosable(element):",
                old="        if not _is_choosable(element) or getattr(element, \"sold_out\", False):",
            )
        ],
        behavioural=f"{T}::test_stock_never_filters_the_answered_pick",
        # ⚠️ NOT the stock test: adding the filter here legitimately changes the
        # tie from twenty to fourteen, so that "regression" fails too — and a
        # regression that also fails proves nothing. This one is independent of
        # stock entirely.
        regression=STUCK,
    ),
    # ------------------------------------------------------- the rail re-ask
    Case(
        id="answered-here-belt",
        why="THE REPORTED DEFECT: the product page the user had just chosen "
            "re-opened the question they had already answered. page_is_the_target "
            "cannot suppress it — MEASURED 9 vs 9.",
        edits=[
            Edit(
                path=L,
                new="        if chosen_target and choice.answered_here(chosen_target, obs.title, obs.url):\n            return target_words, None",
                old="        if False:  # FALSIFICATION\n            return target_words, None",
            )
        ],
        behavioural=f"{T}::test_the_rail_question_is_gone_end_to_end",
        regression=STOCK,
    ),
    Case(
        id="page-belt-reads-the-tie-before-stock",
        why="FOUND IN SELF-REVIEW. page_is_the_target needs two candidates and "
            "answers False for anything shorter, so reading the POST-stock set "
            "switches the belt off the moment stock narrows a tie to one — and "
            "the run then clicks a related product on the page the user asked "
            "for.",
        edits=[
            Edit(
                path=L,
                new="            target_words, obs.title, obs.url, decision.all_tied",
                old="            target_words, obs.title, obs.url, decision.tied  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_stock_narrowing_a_tie_does_not_switch_off_the_page_belt",
        regression=STOCK,
    ),
    Case(
        id="identical-options-merge",
        why="MEASURED: the rail carries two DIFFERENT products with the SAME name "
            "at different hrefs, so the old (label, href) key offered the user two "
            "byte-identical buttons — unanswerable, and pick_by_answer then takes "
            "the first, i.e. code picking between real equals.",
        edits=[
            Edit(
                path=C,
                new="    return \" \".join(_tokens(candidate.option()))",
                old="    return \" \".join(_tokens(candidate.label)) + \"|\" + candidate.href  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_two_identical_options_are_never_both_offered",
        regression=f"{T}::test_two_same_named_items_at_different_prices_stay_distinct",
    ),
    # -------------------------------------------------------------- extraction
    Case(
        id="nav-clause-strip",
        why="THE SIXTH SWITCH: 'go to <site> in the <X> section add <item> to "
            "cart' extracted NOTHING, so both deterministic search legs were "
            "unreachable and a storefront homepage went to the model to guess on.",
        edits=[
            Edit(
                path=L,
                new="    text = _LEAD_NAV_CLAUSE_RE.sub(\"\", text)",
                old="    pass  # FALSIFICATION: the navigation clause survives",
            )
        ],
        behavioural=f"{T}::test_a_section_clause_no_longer_swallows_the_search_term",
        regression=f"{T}::test_the_extraction_controls_did_not_move",
    ),
    Case(
        id="trailing-strip-repeats",
        why="The three trailing clauses can appear in either order and each regex "
            "is anchored to the end, so a fixed order leaves whichever ran first "
            "unable to see its own clause: 'add X to cart on <site>' kept 'to "
            "cart' in the term.",
        edits=[
            Edit(
                path=L,
                new="    for _ in range(3):",
                old="    for _ in range(1):  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_a_section_clause_no_longer_swallows_the_search_term",
        regression=f"{T}::test_the_extraction_controls_did_not_move",
    ),
    # ------------------------------------------------------------------ stuck
    Case(
        id="stuck-raise",
        why="THE REPORTED DEFECT: a page the loop cannot act on killed the run and "
            "closed the window.",
        edits=[
            Edit(
                path=L,
                new="                if commit and not stuck_advice:",
                old="                if False:  # FALSIFICATION",
            )
        ],
        behavioural=STUCK,
        regression=STOCK,
    ),
    Case(
        id="stuck-precedence-branch",
        why="Without a branch in handoff_from_outcome the flag is set and nothing "
            "ever reads it — a no-op that reports success.",
        edits=[
            Edit(
                path=ST,
                new="    if getattr(outcome, \"stuck_required\", False):",
                old="    if False:  # FALSIFICATION",
            )
        ],
        behavioural=STUCK,
        regression=f"{T}::test_stuck_is_the_last_reason_so_it_shadows_nothing",
    ),
    Case(
        id="stuck-holds-the-window",
        why="A reason absent from _DISCOVERY_HOLD_REASONS has its session closed "
            "in discover()'s finally — and that window closing is literally the "
            "reported defect.",
        edits=[
            Edit(
                path=CF,
                new="    Handoff.STUCK: \"stuck\",",
                old="    # FALSIFICATION: the window closes",
            )
        ],
        # The BEHAVIOUR, not the declaration: the dict test would pass even if
        # the lookup that reads it were removed.
        behavioural=f"{T}::test_the_hold_is_actually_taken_not_just_declared",
        regression=STUCK,
    ),
    Case(
        id="one-stuck-ask-per-run",
        why="A second stall after the user has already steered means the steer did "
            "not unblock it; asking again is chaining questions off a question.",
        edits=[
            Edit(
                path=L,
                new="                if commit and not stuck_advice:",
                old="                if commit:  # FALSIFICATION: asks again",
            )
        ],
        behavioural=f"{T}::test_a_second_stall_after_advice_does_not_ask_again",
        regression=STUCK,
    ),
    Case(
        id="advice-reaches-the-model",
        why="The answer has to change what the run does next, or the resume walks "
            "into the same wall — and the ask would never terminate.",
        edits=[
            Edit(
                path=L,
                new="            if stuck_advice:\n                decide_goal = (",
                old="            if False:  # FALSIFICATION\n                decide_goal = (",
            )
        ],
        behavioural=f"{T}::test_the_steer_changes_what_the_model_is_asked",
        regression=STUCK,
    ),
    Case(
        id="advice-never-invalidates-an-approval",
        why="`stuck_advice` lives in step parameters, so it MOVES step.signature(). "
            "Stamping it onto an approved step would invalidate the contract the "
            "user said yes to.",
        edits=[
            Edit(
                path=P,
                new="        if browse_state.commit_contract(step.parameters) is not None:\n            continue\n        step.parameters[\"stuck_advice\"] = advice",
                old="        step.parameters[\"stuck_advice\"] = advice  # FALSIFICATION",
            )
        ],
        behavioural=f"{T}::test_stuck_advice_is_never_stamped_on_an_approved_step",
        regression=f"{T}::test_no_advice_stamps_nothing",
    ),
]


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in CASES if not wanted or c.id == wanted]
    if not cases:
        print(f"no case {wanted!r}; known: {', '.join(c.id for c in CASES)}")
        return 2
    ok = [run(c) for c in cases]
    print(f"\n{'=' * 60}\n{sum(ok)}/{len(ok)} valid falsifications")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
