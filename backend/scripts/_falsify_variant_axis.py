"""
Falsify the 2026-08-08 variant-axis round.

Every behavioural change is proven to FAIL by reverting THAT SPECIFIC LINE IN
PLACE (never `git show :file` — in a tree with a large uncommitted baseline that
is not "the code before this change"), each paired with a regression test that
must keep PASSING. The harness itself is imported from _falsify_modal_wall.py so
its recorded lessons apply unchanged: unique whole-line anchors, the revert
VERIFIED on disk before a green result is believed, pytest's EXIT CODE read
(5 = nothing collected is never a pass), and the restore in a `finally`.

    venv\\Scripts\\python -u scripts\\_falsify_variant_axis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _falsify_modal_wall import Case, Edit, run  # noqa: E402

C = "app/browser/choice.py"
L = "app/browser/loop.py"
S = "app/browser/session.py"
T = "tests/test_browse_variant_axis.py"

CASES = [
    # ---------------------------------------------------------------- the gate
    Case(
        id="the ask exists at all",
        why="Without it a submit carries whatever the form had — measured EMPTY.",
        edits=[
            Edit(L,
                 "            if False:  # REVERTED (ask)\n",
                 "            if settle is not None and settle.options:\n"),
        ],
        behavioural=f"{T}::test_the_loop_asks_rather_than_submitting_a_form_with_no_size",
        regression=f"{T}::test_a_read_only_browse_never_reaches_the_gate",
    ),
    Case(
        id="code settles what is not a real question",
        why=("The forced/named cases must be applied to the form, or every add "
             "becomes a question and the contract keeps its empty variant."),
        edits=[
            Edit(L,
                 "            if False:  # REVERTED (settle)\n",
                 "            if settle is not None and settle.settled:\n"),
        ],
        behavioural=f"{T}::test_the_only_size_in_stock_is_taken_and_the_submit_proceeds",
        regression=f"{T}::test_the_loop_asks_rather_than_submitting_a_form_with_no_size",
    ),
    # ------------------------------------------------ availability is the filter
    Case(
        id="sold-out values are never offered",
        why=("MEASURED: six sizes, one in stock. Offering 'every value' offers "
             "five dead ends."),
        edits=[
            Edit(C,
                 "        return tuple(o for o in self.options)  # REVERTED\n",
                 "        return tuple(o for o in self.options if o.available)\n"),
        ],
        behavioural=f"{T}::test_a_genuinely_open_axis_asks_with_only_what_can_be_bought",
        regression=f"{T}::test_single_valued_axes_are_never_questions",
    ),
    Case(
        id="one buyable value is taken, not asked about",
        why="A forced choice is not a question; asking would offer 5 dead ends.",
        edits=[
            Edit(C,
                 "        if len(buyable) == 999:  # REVERTED\n",
                 "        if len(buyable) == 1:\n"),
        ],
        behavioural=f"{T}::test_six_sizes_one_in_stock_is_not_a_question",
        regression=f"{T}::test_a_genuinely_open_axis_asks_with_only_what_can_be_bought",
    ),
    Case(
        id="an absent availability flag reads as available",
        why=("Defaulting to False would read every un-marked control as sold out "
             "and switch the gate off silently."),
        edits=[
            Edit(C,
                 '                    available=bool(opt.get("available", False)),\n',
                 '                    available=bool(opt.get("available", True)),\n'),
        ],
        behavioural=f"{T}::test_a_missing_available_flag_reads_as_available",
        regression=f"{T}::test_a_sold_out_axis_is_not_our_question",
    ),
    # --------------------------------------------------- the user's words win
    Case(
        id="the user's own words settle the axis",
        why='"add janan sports 100ml" must not become a question about the size.',
        edits=[
            Edit(C,
                 '        wanted = ""  # REVERTED\n',
                 "        wanted = _answered(answer, pool) or _sole_match(target, pool)\n"),
        ],
        behavioural=f"{T}::test_the_users_own_words_settle_it_without_asking",
        regression=f"{T}::test_a_genuinely_open_axis_asks_with_only_what_can_be_bought",
    ),
    Case(
        id="a direct reply is matched exactly",
        why=("_tokens('M') is EMPTY, so without the exact rule a one-letter size "
             "reply can never settle the question it answers."),
        edits=[
            Edit(C,
                 "        if False:  # REVERTED (exact reply)\n",
                 "        if _compact(option.value) == key or _compact(option.label) == key:\n"),
        ],
        behavioural=f"{T}::test_the_answer_settles_the_axis_on_the_resumed_run",
        regression=f"{T}::test_prose_is_never_matched_by_the_exact_rule",
    ),
    Case(
        id="a pre-selected value is overridden by the user's words",
        why="Enforce, never trust — the 2026-08-02 select_option rule.",
        edits=[
            Edit(C,
                 "        if wanted and axis.chosen is None:  # REVERTED\n",
                 "        if wanted:\n"),
        ],
        behavioural=f"{T}::test_the_users_words_beat_a_value_the_page_preselected",
        regression=f"{T}::test_an_axis_the_page_already_settled_is_left_alone",
    ),
    # ------------------------------------------------------- corpus + fallback
    Case(
        id="the corpus is the user's words, not the planner's goal",
        why=("The goal is LLM-authored and drops the size the user named — the "
             "2026-08-07 'right question of the wrong string' lesson."),
        edits=[
            Edit(L,
                 "            goal, url, extra=[t for t in (chosen_target, chosen_option) if t]\n",
                 "            intent, url, extra=[t for t in (chosen_target, chosen_option) if t]\n"),
        ],
        behavioural=f"{T}::test_the_size_is_read_from_the_users_words_not_the_planners_goal",
        regression=f"{T}::test_without_user_words_the_goal_is_still_used",
    ),
    Case(
        id="a control that refuses the value asks instead of submitting",
        why=("Submitting a form whose variant we could not set is the defect the "
             "gate exists to stop."),
        edits=[
            Edit(L,
                 "                if True:  # REVERTED (treat refused as ok)\n",
                 '                if status == "ok":\n'),
        ],
        behavioural=f"{T}::test_a_control_that_refuses_the_value_asks_instead_of_submitting",
        regression=f"{T}::test_the_only_size_in_stock_is_taken_and_the_submit_proceeds",
    ),
    Case(
        id="the form is re-read after code sets a value",
        why=("Without the re-read the approval card carries the contract from "
             "BEFORE the variant was set — the measured empty id."),
        edits=[
            Edit(L,
                 "                    fresh = None  # REVERTED\n",
                 "                    fresh = await session.reread_commit_form()\n"),
        ],
        behavioural=f"{T}::test_the_only_size_in_stock_is_taken_and_the_submit_proceeds",
        regression=f"{T}::test_the_users_size_is_applied_end_to_end_without_a_question",
    ),
    Case(
        id="the page is given time to resolve the variant id",
        why=("MEASURED LIVE: the theme sets the hidden `id` in its OWN change "
             "handler, so an immediate re-read still sees id='' and the approval "
             "card would carry no variant at all."),
        edits=[
            # Paired with the next line: `await session.settle()` alone occurs
            # three times in this file, and the harness refused it — a
            # non-unique anchor patches the wrong statement.
            Edit(L,
                 "                    fresh = await session.reread_commit_form()\n",
                 "                    await session.settle()\n"
                 "                    fresh = await session.reread_commit_form()\n"),
        ],
        behavioural=f"{T}::test_the_only_size_in_stock_is_taken_and_the_submit_proceeds",
        regression=f"{T}::test_the_loop_asks_rather_than_submitting_a_form_with_no_size",
    ),
    # ------------------------------------------------------------- the reading
    Case(
        id="an opaque axis name is not read out",
        why="The cards name the same axis `option-15623440335008-1`.",
        edits=[
            Edit(C,
                 "    if not text:  # REVERTED\n",
                 "    if not text or _OPAQUE_AXIS_RE.match(text):\n"),
        ],
        behavioural=f"{T}::test_an_opaque_axis_name_is_not_read_out_in_the_question",
        regression=f"{T}::test_six_sizes_one_in_stock_is_not_a_question",
    ),
    Case(
        id="a single-valued control is not an axis",
        why=("Color and Style carry one value on every product measured; asking "
             "about them would fire on every add-to-cart."),
        edits=[
            Edit(C,
                 "        if name and len(options) > 0:\n",
                 "        if name and len(options) > 1:\n"),
        ],
        behavioural=f"{T}::test_single_valued_axes_are_never_questions",
        regression=f"{T}::test_six_sizes_one_in_stock_is_not_a_question",
    ),
    # ------------------------------------------- the fingerprint-invariance claim
    Case(
        id="axes stay OUT of the approval fingerprint",
        why=("⚠️ THE LOAD-BEARING SAFETY CLAIM. If axes were folded into the "
             "fingerprint, a cosmetic read could refuse — or accept — an "
             "approved submit."),
        edits=[
            Edit(S,
                 '    return (method, url, fields, uploads, str(state.get("axes") or ""))\n',
                 "    return (method, url, fields, uploads)\n"),
        ],
        behavioural=f"{T}::test_axes_cannot_move_the_approval_fingerprint",
        regression=f"{T}::test_the_chosen_value_does_move_the_fingerprint",
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
