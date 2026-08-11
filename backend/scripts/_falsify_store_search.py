"""Falsify the 2026-08-09 store-search round.

Every behavioural change is proven to FAIL by reverting THAT SPECIFIC LINE IN
PLACE (never `git show :file` — in a tree with a large uncommitted baseline that
is not "the code before this change"), each paired with a regression test that
must keep PASSING. The harness is imported from _falsify_modal_wall.py so its
recorded lessons apply unchanged: unique whole-line anchors, the revert VERIFIED
on disk before a green result is believed, pytest's EXIT CODE read (5 = nothing
collected is never a pass), and the restore in a `finally`.

⚠️ FIVE SWITCHES, AND EACH IS FALSIFIED SEPARATELY. That is the point of the
round: fixing any one of them alone leaves the incident intact, so a single
end-to-end test passing would not tell you which of the five is load-bearing.

    venv\\Scripts\\python -u scripts\\_falsify_store_search.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

C = "app/browser/choice.py"
L = "app/browser/loop.py"
P = "app/agents/planner.py"
F = "app/browser/commit_flow.py"

T = "tests/test_browse_store_search.py"
S = "tests/test_browse_latest_season.py"
K = "tests/test_browser_commit.py"

CASES = [
    # ------------------------------------------- S1: the commit gate (biggest)
    Case(
        id="S1 a commit journey may search at all",
        why=("The deterministic search was gated `not commit`, so a goal that "
             "ENDS in a submit could never search on the way there — the "
             "incident ran mode=commit, so this switch alone decided it."),
        edits=[
            Edit(L,
                 "        may_code_search = step == 0 and not commit  # REVERTED\n",
                 "        may_code_search = step == 0 or step == search_ui_opened_at + 1\n"),
        ],
        behavioural=f"{T}::test_a_commit_journey_may_search_at_all",
        regression=f"{T}::test_a_read_only_browse_is_unchanged",
    ),
    Case(
        id="S1 guard: never search away from the page that IS the target",
        why=("The one real regression risk of enabling the search on a commit "
             "journey: a start URL that is already the product page."),
        edits=[
            Edit(L,
                 "            if False:  # REVERTED (page_covers_target)\n",
                 "            if choice.page_covers_target(_target_words(obs.url), obs.title, obs.url):\n"),
        ],
        behavioural=f"{T}::test_a_start_url_that_is_the_product_is_left_alone",
        regression=f"{T}::test_a_commit_journey_may_search_at_all",
    ),
    # ------------------------------------------ S2: the hidden search box
    Case(
        id="S2 a search box behind an icon is opened",
        why=("MEASURED live: the homepage has ZERO text inputs and one link "
             "named 'drawer-search'. Without this leg the loop cannot search on "
             "that page at all, and the model clicks a category instead."),
        edits=[
            Edit(L,
                 "                    action = None  # REVERTED (open search UI)\n",
                 "                    action = _open_search_ui_action(obs)\n"),
        ],
        behavioural=f"{T}::test_the_incident_a_commit_journey_searches_instead_of_browsing_a_category",
        regression=f"{T}::test_the_fast_path_still_fires_on_a_single_real_box",
    ),
    Case(
        id="S2 bound: the search UI is opened at most once per run",
        why=("A control that reveals no box must never become a loop — and "
             "because clicking a toggle CHANGES the page, a per-page bound would "
             "not have held: a chain of 'search'-named controls would be clicked "
             "through one by one."),
        edits=[
            Edit(L,
                 "                ) and search_ui_opened_at < 999:  # REVERTED\n",
                 "                ) and search_ui_opened_at < 0:\n"),
        ],
        behavioural=f"{T}::test_a_toggle_that_reveals_nothing_is_not_clicked_twice",
        regression=f"{T}::test_the_incident_a_commit_journey_searches_instead_of_browsing_a_category",
    ),
    Case(
        id="S2 a real box is the fast path's job, not the toggle's",
        why=("A page with BOTH would otherwise click the icon and close the box "
             "it already had."),
        edits=[
            Edit(L,
                 "    if False:  # REVERTED (a real box present)\n",
                 "    if any(_is_typeable_search(e) for e in obs.elements):\n"),
        ],
        behavioural=f"{T}::test_a_real_search_box_is_the_fast_paths_job_not_the_toggles",
        regression=f"{T}::test_the_search_toggle_is_clicked_when_there_is_no_box",
    ),
    Case(
        id="S2 several toggles defer to the model",
        why="Code never picks between equals — the module's own rule.",
        edits=[
            Edit(L,
                 "    if len(toggles) < 1:\n",
                 "    if len(toggles) != 1:\n"),
        ],
        behavioural=f"{T}::test_several_toggles_defer_to_the_model",
        regression=f"{T}::test_the_search_toggle_is_clicked_when_there_is_no_box",
    ),
    # ----------------------------- S3: the fast path must not type into a link
    Case(
        id="S3 the fill target must be TYPEABLE",
        why=("Two defects, one cause: the code conflated 'is a search thing' "
             "with 'is a box you can type in'. MEASURED live — the homepage's "
             "lone candidate is a LINK (it would have typed into it), and the "
             "search page has a real box PLUS an 'Upload an image for search' "
             "button, which made it defer with a box in front of it."),
        edits=[
            Edit(L,
                 "    typeable = [e for e in candidates if _is_search_target(e)]  # REVERTED\n",
                 "    typeable = [e for e in candidates if _is_typeable_search(e)]\n"),
        ],
        behavioural=f"{T}::test_an_image_search_button_does_not_block_the_real_box",
        regression=f"{T}::test_two_real_boxes_are_still_ambiguous",
    ),
    Case(
        id="S3 several typeable boxes stay ambiguous",
        why="Code never picks between equals — the module's own rule.",
        edits=[
            Edit(L,
                 "    target = typeable[0] if len(typeable) >= 1 else None  # REVERTED\n",
                 "    target = typeable[0] if len(typeable) == 1 else None\n"),
        ],
        behavioural=f"{T}::test_two_real_boxes_are_still_ambiguous",
        regression=f"{T}::test_the_fast_path_still_fires_on_a_single_real_box",
    ),
    Case(
        id="S3 the ORIGINAL defect: a lone candidate taken unchecked",
        why=("Restores the two-branch shape this replaced — the genuineness test "
             "applied only when there were SEVERAL candidates, so the live "
             "homepage's lone 'drawer-search' LINK was taken as a fill target."),
        edits=[
            Edit(L,
                 "    target = candidates[0] if len(candidates) == 1 else (  # REVERTED\n"
                 "        typeable[0] if len(typeable) == 1 else None)\n",
                 "    target = typeable[0] if len(typeable) == 1 else None\n"),
        ],
        behavioural=f"{T}::test_the_fast_path_never_types_into_a_link",
        regression=f"{T}::test_the_fast_path_still_fires_on_a_single_real_box",
    ),
    Case(
        id="S3 a search box whose label never says 'search' is findable",
        why=("form_search joins the candidate gather; without it a box labelled "
             "'What are you looking for?' is invisible to the fast path."),
        edits=[
            Edit(L,
                 '        if e.role in _SEARCH_ROLES  # REVERTED\n'
                 '        or "search" in (e.name or "").lower()\n',
                 "        if e.role in _SEARCH_ROLES\n"
                 '        or getattr(e, "form_search", False)\n'
                 '        or "search" in (e.name or "").lower()\n'),
        ],
        behavioural=f"{T}::test_a_search_box_whose_label_never_says_search_is_still_found",
        regression=f"{T}::test_the_fast_path_still_fires_on_a_single_real_box",
    ),
    # ------------------------------- S4: the user's words reach the commit path
    Case(
        id="S4 the injector reaches a commit step still in discovery",
        why=("Scoped to `browse` only, so the two commit-mode-only consumers "
             "added on 2026-08-08 scored the planner's paraphrase."),
        edits=[
            # ⚠️ Paired with the comment line below: the bare
            # `if step.tool not in browser_grounding._BROWSE_TOOLS:` appears in
            # all THREE sibling injectors too, and a non-unique anchor patches
            # the wrong function.
            Edit(P,
                 '        if step.tool != "browse":  # REVERTED\n'
                 "            continue\n"
                 "        # An approval-bound step is never re-stamped — its contract is what the\n",
                 "        if step.tool not in browser_grounding._BROWSE_TOOLS:\n"
                 "            continue\n"
                 "        # An approval-bound step is never re-stamped — its contract is what the\n"),
        ],
        behavioural=f"{S}::test_user_words_reach_a_commit_step_that_is_still_in_discovery",
        regression=f"{S}::test_user_words_are_never_stamped_on_an_approval_bound_step",
    ),
    Case(
        id="S4 an approval-bound step is never re-stamped",
        why=("⚠️ THE SAFETY HALF. Once a commit step carries its contract it is "
             "what the user said yes to, and its signature must not move."),
        edits=[
            # Paired with the stamp below — the contract check alone is shared
            # verbatim with _inject_site_corrections and _inject_target_choices.
            Edit(P,
                 "        if False:  # REVERTED (approval-bound skip)\n"
                 "            continue\n"
                 '        step.parameters["user_words"] = words\n',
                 "        if browse_state.commit_contract(step.parameters) is not None:\n"
                 "            continue\n"
                 '        step.parameters["user_words"] = words\n'),
        ],
        behavioural=f"{S}::test_user_words_are_never_stamped_on_an_approval_bound_step",
        regression=f"{S}::test_user_words_reach_a_commit_step_that_is_still_in_discovery",
    ),
    Case(
        id="S4 discover actually HANDS the words to the loop",
        why=("⚠️ THE WIRING — and the wiring is what was broken. 2026-08-08 "
             "fixed the reader and never the writer, so the fallback fired every "
             "time and the fix was a no-op in the only mode its consumers run "
             "in. Two functions each behaving correctly cannot catch that."),
        edits=[
            Edit(F,
                 '                intent_text="",  # REVERTED\n',
                 '                intent_text=str(params.get("user_words") or "").strip(),\n'),
        ],
        behavioural=f"{K}::test_discover_hands_the_users_own_words_to_the_loop",
        regression=f"{K}::test_discover_without_user_words_passes_an_empty_intent",
    ),
    # ------------------------------------ S5: a shopping goal yields a product
    Case(
        id="S5 shopping verbs are stripped",
        why=("MEASURED: 7 of 7 shopping phrasings produced a junk term, and the "
             "missing verb also stopped the 'go to <site> and' prefix firing, so "
             "the DOMAIN stayed in the term too."),
        edits=[
            Edit(L,
                 '    r"take\\s+me\\s+to|bring\\s+up)"  # REVERTED\n',
                 '    r"take\\s+me\\s+to|bring\\s+up|add|buy|purchase)"\n'),
        ],
        behavioural=(
            f"{T}::test_a_shopping_goal_yields_the_product_name"
            "[add janan perfume to cart-janan perfume]"
        ),
        regression=(
            f"{T}::test_existing_extraction_is_unchanged"
            "[find the order of the phoenix on goodreads-the order of the phoenix]"
        ),
    ),
    Case(
        id="S5 a trailing cart phrase is stripped",
        why=("_TRAIL_SITE_RE eats a bare 'in cart' by accident but not 'to cart' "
             "or 'to my cart' — relying on that accident fixes one phrasing of "
             "three."),
        edits=[
            Edit(L,
                 "    text = text  # REVERTED (cart strip)\n",
                 '    text = _TRAIL_CART_RE.sub("", text)\n'),
        ],
        behavioural=(
            f"{T}::test_a_shopping_goal_yields_the_product_name"
            "[add janan sports 100ml to my cart-janan sports 100ml]"
        ),
        regression=(
            f"{T}::test_existing_extraction_is_unchanged"
            "[play the dangers in my heart on anikoto.cz-the dangers in my heart]"
        ),
    ),
]


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in CASES if not wanted or c.id == wanted]
    if not cases:
        print(f"no case matching {wanted!r}. Known:")
        for c in CASES:
            print(f"  {c.id}")
        return 2
    results = [(c.id, run(c)) for c in cases]
    print("\n" + "=" * 68)
    for cid, ok in results:
        print(f"  {'VALID  ' if ok else 'INVALID'}  {cid}")
    bad = [cid for cid, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} valid")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
