"""Falsification harness for the 2026-08-08 round (login modal / login hand-off /
explicit season / readiness).

THE HOUSE RULE: every behavioural change must be proven to FAIL when the specific
line that implements it is reverted IN PLACE. Not `git show :file` — this tree
carries a large uncommitted baseline, so the indexed copy is not "the code before
this change" (2026-08-01, learned when a restored file failed on a missing import
rather than on the defect).

Every lesson this project has paid for is encoded here as a CHECK rather than as a
convention someone has to remember:

  * ANCHORS ARE UNIQUE WHOLE LINES. A substring anchor once matched a more-indented
    line and the "revert" produced an IndentationError — every test failed, for the
    wrong reason, which is indistinguishable from a passing falsification if you
    only read the exit code. We assert the anchor occurs exactly once.

  * THE REVERT IS VERIFIED ON DISK before the result is trusted. Three
    falsifications have lied in this project. A patch that never landed reports a
    green behavioural test and reads exactly like "the change was pointless".

  * PYTEST'S EXIT CODE IS READ, NOT ITS OUTPUT. Exit 5 means nothing was
    collected — a typo'd node id — and scores identically to "test failed" if you
    only scrape text.

  * A CASE MAY REVERT SEVERAL LINES. A guarantee defended in three places survives
    the removal of any one of them, and a single-line revert then comes back green
    while proving nothing. A falsification must remove the GUARANTEE.

  * RESTORE IN A `finally`, and assert it. A harness that edits source and dies
    between the revert and the restore becomes the defect it is hunting.

Run from backend/:  venv\\Scripts\\python scripts\\_falsify_modal_wall.py [case-id]
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Edit:
    """One line to swap.

    `new` is what the file says NOW (after this round's change); `old` is what it
    said BEFORE. Reverting rewrites new -> old. For a purely ADDITIVE change `old`
    is "" — which is exactly the shape that broke the first cut of this harness:
    `_landed` asked "is `old` present?", and `"" in text` is trivially true, so a
    revert that never landed would have reported a confident verdict.

    `new` must appear EXACTLY ONCE in the file; a non-unique anchor patches the
    wrong line."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Case:
    """One falsification: revert `edits`, expect `behavioural` to FAIL and
    `regression` to still PASS."""

    id: str
    why: str
    edits: list[Edit]
    behavioural: str
    regression: str = ""
    extra: list[str] = field(default_factory=list)


def _pytest(node_ids: list[str]) -> tuple[bool, str]:
    """(passed, summary). Reads the EXIT CODE; 5 (nothing collected) is never a
    pass, however green the text looks."""
    proc = subprocess.run(
        [str(ROOT / "venv" / "Scripts" / "python"), "-m", "pytest", *node_ids, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
    summary = tail[-1] if tail else "(no output)"
    if proc.returncode == 5:
        return False, f"NOTHING COLLECTED (exit 5) - bad node id? | {summary}"
    return proc.returncode == 0, summary


def _revert(edits: list[Edit]) -> list[tuple[Path, str]]:
    """Rewrite new -> old for every edit. Returns the ORIGINAL contents so the
    caller can restore. Raises BEFORE touching anything if an anchor is not
    unique — a non-unique anchor patches the wrong line, and this project has
    already spent a round on one that did."""
    targets = []
    for e in edits:
        p = ROOT / e.path
        text = p.read_text(encoding="utf-8")
        if not e.new:
            raise AssertionError(f"{e.path}: `new` is empty — nothing to anchor on")
        count = text.count(e.new)
        if count != 1:
            raise AssertionError(
                f"anchor occurs {count}x in {e.path} (must be exactly 1):\n  {e.new!r}"
            )
        targets.append((p, text))

    for e, (p, text) in zip(edits, targets):
        p.write_text(text.replace(e.new, e.old, 1), encoding="utf-8")
    return targets


def _landed(edits: list[Edit]) -> bool:
    """Did the revert actually reach the disk?

    The meaningful test is that the CHANGE IS GONE — `new` absent — which holds
    for an additive edit too. Asking "is `old` present?" is what the first cut
    did, and for an additive edit (`old == ""`) that is trivially true: a revert
    that never landed would have reported a confident verdict."""
    for e in edits:
        text = (ROOT / e.path).read_text(encoding="utf-8")
        if e.new in text:
            return False
        if e.old and e.old not in text:
            return False
    return True


def run(case: Case) -> bool:
    print(f"\n=== {case.id} ===")
    print(f"    {case.why}")
    originals: list[tuple[Path, str]] = []
    try:
        try:
            originals = _revert(case.edits)
        except AssertionError as exc:
            # A bad anchor is this case's problem, not the run's. Reported as
            # INVALID and skipped — nothing was written, so there is nothing to
            # restore and nothing downstream to confuse.
            print(f"    !! BAD ANCHOR — {exc}")
            print("    => INVALID")
            return False
        if not _landed(case.edits):
            print("    !! REVERT DID NOT LAND — result not trusted")
            return False

        ok_behav, sum_behav = _pytest([case.behavioural, *case.extra])
        ok_regress, sum_regress = (True, "(none given)")
        if case.regression:
            ok_regress, sum_regress = _pytest([case.regression])
    finally:
        # ONLY what was actually written gets restored and checked. The first cut
        # asserted over every edit unconditionally, so a case that raised before
        # touching disk reported "the tree is left broken" about a tree that was
        # perfectly fine — a harness manufacturing the panic it exists to prevent.
        for p, text in originals:
            p.write_text(text, encoding="utf-8")
        if originals:
            for e in case.edits:
                assert e.new in (ROOT / e.path).read_text(encoding="utf-8"), (
                    f"RESTORE FAILED for {e.path} — the tree is left broken"
                )

    # The correct signature: the behavioural test FAILS on the reverted code and
    # the regression test still PASSES. A regression that also fails proves
    # nothing (it depended on the same line); a behavioural test that passes
    # means the guarantee survives the revert — suspect the test's REACH first.
    verdict = (not ok_behav) and ok_regress
    print(f"    behavioural  {'FAILED (good)' if not ok_behav else 'PASSED (BAD)'}  | {sum_behav}")
    if case.regression:
        print(f"    regression   {'passed (good)' if ok_regress else 'FAILED (BAD)'}  | {sum_regress}")
    print(f"    => {'VALID' if verdict else 'INVALID'}")
    return verdict


S = "app/browser/session.py"
L = "app/browser/loop.py"
O = "app/browser/observe.py"
T = "app/tools/browser_agent_tools.py"
SEA = "app/browser/season.py"
P = "app/agents/planner.py"

ST = "app/browser/state.py"

TS = "tests/test_browser_session.py"
TL = "tests/test_browser_loop.py"
TW = "tests/test_modal_wall.py"
TL2 = "tests/test_browser_login.py"
TSEA = "tests/test_browse_explicit_season.py"

CASES: list[Case] = [
    # ---------------------------------------------------------------- D4
    Case(
        id="D4-busy-exit",
        why="a substantive page whose DOM never settles must stop waiting",
        edits=[
            Edit(
                S,
                "",
                "            if (\n"
                "                time.monotonic() - started >= READY_BUSY_MS / 1000.0\n"
                "                and int(state.get(\"acts\") or 0) >= READY_BUSY_ACTS\n"
                "            ):\n"
                "                return _ready(\"substantive-but-busy\")\n",
            )
        ],
        behavioural=f"{TS}::test_a_busy_page_that_never_settles_stops_waiting",
        regression=f"{TS}::test_the_daraz_measurement_still_beats_the_busy_exit",
    ),
    Case(
        id="D4-act-floor",
        why="the act floor is what keeps a thin shell waiting for its full budget",
        edits=[
            Edit(
                S,
                '                and int(state.get("acts") or 0) >= 0',
                '                and int(state.get("acts") or 0) >= READY_BUSY_ACTS',
            )
        ],
        behavioural=f"{TS}::test_a_busy_but_thin_page_keeps_its_full_budget",
        regression=f"{TS}::test_a_busy_page_that_never_settles_stops_waiting",
    ),
    # ---------------------------------------------------------------- D1
    Case(
        id="D1-demotion",
        why="a credential form inside a dialog must not read as a hard wall",
        edits=[
            Edit(
                L,
                "",
                "    if credential_overlay_site(obs) is not None:\n"
                "        return None\n",
            )
        ],
        behavioural=f"{TW}::test_the_incident_a_sign_in_modal_is_not_a_wall",
        regression=f"{TW}::test_a_real_sign_in_page_is_still_a_wall",
    ),
    Case(
        id="D1-in-dialog-plumbing",
        why="the JS record's in_dialog flag must reach the Element dataclass",
        edits=[Edit(O, "", '                in_dialog=bool(item.get("in_dialog")),\n')],
        behavioural=f"{TW}::test_the_incident_a_sign_in_modal_is_not_a_wall",
        regression=f"{TW}::test_a_real_sign_in_page_is_still_a_wall",
    ),
    Case(
        id="D1-page-content",
        why="the page must have content of its own, or a login PAGE gets demoted",
        edits=[
            Edit(
                L,
                "    return own >= 0",
                "    return own >= _OVERLAY_MIN_PAGE_ELEMENTS",
            )
        ],
        behavioural=f"{TW}::test_a_login_page_rendered_as_a_dialog_is_still_a_wall",
        regression=f"{TW}::test_the_incident_a_sign_in_modal_is_not_a_wall",
    ),
    Case(
        id="D1-auth-route",
        why="a /login or /register URL is the page's own identity — never demotable",
        edits=[
            Edit(
                L,
                "",
                '    if _SIGNUP_ROUTE_RE.search(urlparse(obs.url).path or ""):\n'
                "        return None\n",
            )
        ],
        behavioural=f"{TW}::test_an_auth_route_is_never_demoted",
        regression=f"{TW}::test_the_incident_a_sign_in_modal_is_not_a_wall",
    ),
    Case(
        id="D1-auth-host",
        why="a dedicated sign-in host is the whole page, whatever its markup says",
        edits=[
            # NOT a bare `if _is_auth_host(host):` — that line appears in
            # detect_login_wall too, and a non-unique anchor patches the wrong one.
            Edit(L, "", "    if _is_auth_host(host):\n        return None\n")
        ],
        behavioural=f"{TW}::test_an_auth_host_is_never_demoted",
        regression=f"{TW}::test_the_incident_a_sign_in_modal_is_not_a_wall",
    ),
    Case(
        id="D1-escape",
        why="the loop must actually press Escape, not merely decline to wall",
        edits=[
            Edit(
                L,
                "                ok, note = (False, \"disabled\")\n",
                "                ok, note = await _act(\n"
                "                    session, obs, {\"action\": \"press_key\", \"key\": \"Escape\"}\n"
                "                )\n",
            )
        ],
        behavioural=f"{TW}::test_the_loop_dismisses_the_dialog_and_carries_on",
        regression=f"{TW}::test_a_real_sign_in_page_is_still_a_wall",
    ),
    Case(
        id="D1-survived-is-a-wall",
        why="a dialog that survives Escape is not dismissible, which is what a wall is",
        edits=[
            Edit(
                L,
                "        if False:\n",
                "        if wall is None and overlay_survived and not skip_login_wall:\n",
            )
        ],
        behavioural=f"{TW}::test_a_dialog_that_survives_escape_is_a_wall",
        regression=f"{TW}::test_the_loop_dismisses_the_dialog_and_carries_on",
    ),
    Case(
        id="D1-dismissal-cap",
        why="a page whose fingerprint churns must never spin the loop on Escape",
        edits=[
            Edit(
                L,
                "                and len(overlays_dismissed) < 10_000",
                "                and len(overlays_dismissed) < _MAX_OVERLAY_DISMISSALS",
            )
        ],
        behavioural=f"{TW}::test_the_dismissal_is_bounded",
        regression=f"{TW}::test_the_loop_dismisses_the_dialog_and_carries_on",
    ),
    # ---------------------------------------------------------------- D2
    Case(
        id="D2-in-place",
        why="a sign-in wall must hand the tab over, not close every tab",
        edits=[
            Edit(
                T,
                "                        login_in_place = False\n",
                "                        login_in_place = await session.release_to_user()\n",
            )
        ],
        behavioural=(
            f"{TL2}::test_a_sign_in_wall_hands_over_the_tab_instead_of_closing_the_browser"
        ),
        regression=f"{TL2}::test_a_failed_login_hand_over_falls_back_to_the_clean_window",
    ),
    Case(
        id="D2-escalation",
        why="a site that walls AGAIN must earn the profile-hungry clean window",
        edits=[
            Edit(T, "", '                        browser_session.note_handoff(login_site, "login")\n')
        ],
        behavioural=f"{TL2}::test_a_site_that_walls_again_escalates_to_the_clean_window",
        regression=(
            f"{TL2}::test_a_sign_in_wall_hands_over_the_tab_instead_of_closing_the_browser"
        ),
    ),
    Case(
        id="D2-pause-text",
        why="the pause text must not claim a window about a tab already on screen",
        edits=[
            Edit(
                P,
                "    if False:\n"
                "        lead = \"it's open in the browser window already on your screen\"\n",
                "    if in_place:\n"
                "        lead = \"it's open in the browser window already on your screen\"\n",
            )
        ],
        behavioural=(
            f"{TL2}::test_the_login_pause_text_points_at_the_tab_it_was_handed_over_on"
        ),
        regression=f"{TL2}::test_a_failed_login_hand_over_falls_back_to_the_clean_window",
    ),
    Case(
        id="D2-payload-plumbing",
        why="in_place must survive the HandoffPayload, or the text can never see it",
        edits=[
            Edit(
                ST,
                "",
                '            in_place=bool(getattr(outcome, "login_in_place", False)),\n',
            )
        ],
        behavioural=f"{TL2}::test_the_in_place_flag_survives_the_handoff_payload",
        regression=f"{TL2}::test_a_failed_login_hand_over_falls_back_to_the_clean_window",
    ),
    # ---------------------------------------------------------------- D3
    Case(
        id="D3-companion-exclusion",
        why="a film carrying the season's digit must not tie with the season",
        edits=[
            Edit(
                SEA,
                "",
                "    if is_companion_release(candidate, title):\n        return 0\n",
            )
        ],
        behavioural=f"{TSEA}::test_the_incident_season_4_no_longer_ties_with_a_movie",
        regression=f"{TSEA}::test_the_web_resolved_season_name_path_is_untouched",
    ),
    Case(
        id="D3-series-membership",
        why=(
            "a numbered season must be the RIGHT SHOW first — found by the "
            "runtime check against anikoto's fuzzy 40-row result set, where "
            "'Season 4' tied the real entry with an unrelated show's season 4"
        ),
        edits=[
            Edit(
                SEA,
                "",
                "    if not belongs_to_series(candidate, title):\n        return 0\n",
            )
        ],
        behavioural=(
            f"{TSEA}::test_the_runtime_finding_a_numbered_season_must_be_the_right_show_first"
        ),
        regression=f"{TSEA}::test_the_web_resolved_season_name_path_is_untouched",
    ),
    Case(
        id="D3-ordinal-normalisation",
        why="'5th season' and 'season 5' must be the same season",
        edits=[
            Edit(
                SEA,
                "    match = None\n",
                "    match = _ORDINAL_SUFFIX_RE.match(token)\n",
            )
        ],
        behavioural=f"{TSEA}::test_every_way_this_one_site_spells_a_season_resolves",
        regression=f"{TSEA}::test_the_incident_season_4_no_longer_ties_with_a_movie",
    ),
    Case(
        id="D3-wrong-season-guard",
        why="an episode must never be swapped into a season the user did not name",
        edits=[
            Edit(
                L,
                "        if action is None and not commit:\n"
                "            action = _episode_action(intent, obs)\n",
                "        if action is None and not commit and (on_target_season or not season_goal):\n"
                "            action = _episode_action(intent, obs)\n",
            )
        ],
        behavioural=f"{TSEA}::test_the_episode_is_never_swapped_into_the_wrong_season",
        regression=f"{TSEA}::test_the_episode_swap_still_works_once_the_season_is_right",
    ),
    Case(
        id="D3-ask-on-tie",
        why="code never picks between two entries that match a season equally",
        edits=[
            Edit(L, '                    out.choice_kind = "item"\n',
                 '                    out.choice_kind = "season"\n')
        ],
        behavioural=f"{TSEA}::test_a_tie_asks_instead_of_guessing",
        regression=f"{TSEA}::test_the_loop_reaches_season_4_episode_4_with_no_llm_call",
    ),
    Case(
        id="D3-answer-outranks-the-guess",
        why=(
            "an answered choice must be honoured BEFORE the season leg can re-ask "
            "the same question (the entries are still tied — that is why it asked)"
        ),
        edits=[
            Edit(
                L,
                "        if False and choice_pending:\n",
                "        if action is None and choice_pending:\n",
            )
        ],
        behavioural=f"{TSEA}::test_an_answered_choice_is_clicked_not_re_asked",
        regression=f"{TSEA}::test_a_tie_asks_instead_of_guessing",
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
