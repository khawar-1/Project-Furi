"""Falsification harness for the 2026-08-10 add-to-cart round.

THE HOUSE RULE: every behavioural change is proven to FAIL when the specific line
that implements it is reverted IN PLACE — never `git show :file`, because this
tree carries a large uncommitted baseline.

⚠️ THE MACHINERY IS IMPORTED, NOT COPIED. `_falsify_modal_wall.py` already
encodes every lesson this project has paid for (unique whole-line anchors, the
revert verified on disk, pytest's EXIT CODE read — 5 means nothing was collected
and is never a pass, restore asserted in a `finally`), and a second copy is the
same second-copy-of-a-fact hole recorded eight times now.

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_ecommerce_flow.py [case-id]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

L = "app/browser/loop.py"
C = "app/browser/choice.py"
S = "app/browser/session.py"
R = "app/agents/rendering.py"

TC = "tests/test_browse_add_to_cart.py"
TI = "tests/test_browse_item_choice.py"
TV = "tests/test_browse_variant_axis.py"

# The two that must survive every revert in the CHOICE half.
TIE = f"{TI}::test_suppressor_one_a_joined_pair_no_longer_manufactures_a_leader"
TITLE = f"{TI}::test_suppressor_two_a_search_title_no_longer_claims_to_be_the_thing"
PAIR_KEPT = f"{TI}::test_the_joined_pair_still_does_the_job_it_exists_for"
RAIL = f"{TI}::test_a_product_page_still_beats_its_own_rail"

# …and in the CART half.
BUYBOX = f"{TC}::test_the_incident_the_buy_box_is_reached_and_the_size_is_asked"
SIZE = f"{TC}::test_the_users_own_size_is_taken_without_asking"
STEER = f"{TC}::test_a_steered_resume_does_not_search_away_from_the_page_it_asked_about"
FRESH = f"{TC}::test_a_fresh_run_still_searches_from_a_page_that_is_not_the_item"
OOS = f"{TC}::test_an_out_of_stock_product_is_reported_not_submitted"
NOFIRE = f"{TC}::test_a_goal_that_never_mentioned_a_cart_never_looks"
CART_OK = f"{TC}::test_cart_verification_reports_the_sites_own_numbers"
CART_NO = f"{TC}::test_cart_verification_says_so_when_nothing_changed"
PROSE_GUARD = f"{TV}::test_prose_is_never_matched_by_the_exact_rule"


CASES: list[Case] = [
    # ---------------------------------------------------------------- choice.py
    Case(
        id="score-counts-ideas-not-tokens",
        why="THE ADJACENCY BONUS. Counting joined-pair spellings as extra ideas "
            "let a label that happens to spell two of the user's words NEXT TO "
            "EACH OTHER out-score one carrying the same words apart — 3 vs 2 on "
            "the incident — so a unique leader existed where the user saw equals "
            "and the question was never asked.",
        edits=[
            Edit(
                path=C,
                new="    return sum(1 for c in concepts if _hits(c) or c in covered_by_pair)",
                old="    return sum(1 for t in target if _hits(t))  # FALSIFICATION",
            )
        ],
        behavioural=TIE,
        # ⚠️ NOT `PAIR_KEPT`: that test asserts the NEW magnitudes (4/2/3), so it
        # fails under the reverted scorer too — and a regression that also fails
        # proves nothing. The title suppressor is independent of the scorer's
        # magnitudes (it asserts a subject of 0 and a tie of 3, both of which
        # hold either way), so it is a real control.
        regression=TITLE,
    ),
    Case(
        id="page-subject-drops-the-request",
        why="Shopify prints the search query INTO the title, so the request came "
            "back through a channel the ?q= rule did not cover: subject 5 vs a "
            "best candidate of 2, and the belt meaning 'this page IS the thing' "
            "fired on a SEARCH RESULTS PAGE.",
        edits=[
            Edit(
                path=C,
                new="        if token in _GOAL_STOPWORDS or token in request or token in kept:",
                old="        if token in _GOAL_STOPWORDS or token in kept:  # FALSIFICATION",
            )
        ],
        behavioural=TITLE,
        regression=RAIL,
    ),
    Case(
        id="answered-size-synonym",
        why="A direct REPLY of 'large' against an axis offering XS/S/M/L/XL: "
            "`_compact` needs an exact string, so it matched nothing and the user "
            "was asked the question they had just answered.",
        edits=[
            Edit(
                path=C,
                new="    return _named_size([answer], options)",
                old="    return \"\"  # FALSIFICATION",
            )
        ],
        behavioural=f"{TV}::test_a_direct_reply_is_matched_exactly[large-L]",
        regression=PROSE_GUARD,
    ),
    Case(
        id="prose-size-synonym",
        why="THE INCIDENT'S OWN STEER. 'select size large and add to cart' arrives "
            "as prose, and an option labelled 'L' is one character — below the "
            "token rule's length gate — so it scored 0 against everything.",
        edits=[
            Edit(
                path=C,
                new="            return _named_size(target, options)\n        return \"\"",
                old="            return \"\"  # FALSIFICATION\n        return \"\"",
            )
        ],
        behavioural=SIZE,
        regression=PROSE_GUARD,
    ),
    Case(
        id="prose-size-needs-the-axis-named",
        why="A size word in a sentence is not always an answer about the size: in "
            "'the small grey shirt' it describes the product. Without the guard "
            "the prose path settles S off it and answers a question the user "
            "never addressed.",
        edits=[
            Edit(
                path=C,
                new="        if axis_name and _mentions_axis(target, axis_name):",
                old="        if True:  # FALSIFICATION",
            )
        ],
        behavioural=PROSE_GUARD,
        regression=SIZE,
    ),
    # ------------------------------------------------------------------ loop.py
    Case(
        id="buy-box-leg",
        why="THE INCIDENT. The buy button is disabled until a size is chosen and "
            "observe.eligible() drops disabled elements, so the buy box was not "
            "in the element list AT ALL — the model produced 'more', 'scroll up' "
            "and then nothing. Without this leg there is no way to add anything.",
        edits=[
            Edit(
                path=L,
                new="            and wants_cart(intent)\n"
                    "            and fingerprint not in buy_box_tried",
                old="            and False  # FALSIFICATION\n"
                    "            and fingerprint not in buy_box_tried",
            )
        ],
        behavioural=BUYBOX,
        regression=NOFIRE,
    ),
    Case(
        id="buy-box-contract-reaches-the-submit",
        why="A code-authored submit carries its contract because the control is "
            "not in the element list — read_commit_target has no index to "
            "resolve, so the submit branch would call the page 'not part of a "
            "form' and the run would spin.",
        edits=[
            Edit(
                path=L,
                new="            if action.get(\"index\") == BUY_BOX_INDEX:\n"
                    "                target = buy_box_contract",
                old="            if False:  # FALSIFICATION\n"
                    "                target = buy_box_contract",
            )
        ],
        behavioural=BUYBOX,
        regression=NOFIRE,
    ),
    Case(
        id="out-of-stock-is-reported",
        why="MEASURED on a real product: the button reads 'Out of stock' and is "
            "disabled with no axis to change that. Submitting a form the site has "
            "switched off is a claim we cannot support.",
        edits=[
            Edit(
                path=L,
                new="                    if not axes and contract.get(\"submit_disabled\"):",
                old="                    if False:  # FALSIFICATION",
            )
        ],
        behavioural=OOS,
        regression=BUYBOX,
    ),
    Case(
        id="steered-resume-does-not-search-away",
        why="THE SECOND HALF OF THE INCIDENT: the resume began at step 0 on the "
            "page the user had just answered about, fired the search leg, and "
            "opened a DIFFERENT product. From their chair the task restarted and "
            "ignored them.",
        edits=[
            Edit(
                path=L,
                new="            (step == 0 and not answered_already) or step == search_ui_opened_at + 1",
                old="            step == 0 or step == search_ui_opened_at + 1  # FALSIFICATION",
            )
        ],
        behavioural=STEER,
        regression=FRESH,
    ),
    # --------------------------------------------------------------- session.py
    Case(
        id="wait-for-the-variant-to-resolve",
        why="MEASURED on the live page: the swatch updates Size and the barcode "
            "synchronously while the variant id lands ~1s later. Without holding "
            "out for the blank field, the approval card names a size in words "
            "over a contract carrying NO variant, and the submit adds an "
            "unspecified one.",
        edits=[
            Edit(
                path=S,
                new="            if blanks:",
                old="            if False:  # FALSIFICATION",
            )
        ],
        behavioural=f"{TC}::test_the_wait_holds_out_for_the_blank_field_to_fill",
        regression=f"{TC}::test_a_form_with_nothing_blank_settles_on_stability",
    ),
    # -------------------------------------------------------------- rendering.py
    Case(
        id="cart-verdict-is-reported",
        why="'Submitted' is a fact about the REQUEST — the interceptor watched it "
            "leave — and a storefront can accept an add and drop it. Without this "
            "the user is told it went in when nothing did.",
        edits=[
            Edit(
                path=R,
                new="    verified = commit.get(\"cart_verified\")",
                old="    verified = None  # FALSIFICATION",
            )
        ],
        behavioural=CART_NO,
        regression=f"{TC}::test_no_cart_evidence_claims_nothing_either_way",
    ),
    Case(
        id="cart-confirmation-names-the-numbers",
        why="A confirmation the user cannot check is not a confirmation. The "
            "site's own before/after count is the evidence.",
        edits=[
            Edit(
                path=R,
                new="            f\" The cart went from {before} to {after} item(s), so it is in.\"",
                old="            \"\"  # FALSIFICATION",
            )
        ],
        behavioural=CART_OK,
        regression=CART_NO,
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
