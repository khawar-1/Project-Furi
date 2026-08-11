"""Live runtime verification for the 2026-08-08 round (login modal / in-place
login hand-off / explicit season / readiness busy exit).

The hermetic suite proves the parts. This proves the WHOLE, against the real
site, in production's shape — the only thing that can show the wiring boots.
The lessons this project has already paid for are built in:

  * the SELECTOR event loop, because a standalone asyncio.run() is Proactor on
    Windows and hides the Playwright-subprocess bug class that shipped twice;
  * the REAL page, not a fixture — the incident was caused by markup anikoto
    actually ships, and a synthetic page cannot confirm that;
  * NO LLM anywhere. Every mechanism this round touches is deterministic, so a
    provider outage cannot make this run lie, and it costs no credits.

⚠️ THIS OPENS A REAL HEADED CHROME WINDOW. It plays nothing, submits nothing,
signs into nothing, and closes what it opened.

Run from backend/:  venv\\Scripts\\python scripts\\_verify_modal_wall_runtime.py
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print = functools.partial(print, flush=True)  # noqa: A001

# The incident's own page and words.
WATCH_URL = "https://anikoto.cz/watch/my-hero-academia-4-mt2j9/ep-1"
SEARCH_URL = "https://anikoto.cz/filter?keyword=my+hero+academia"
USER_WORDS = "play ep 4 of season 4 of my hero academia on anikoto"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


# --------------------------------------------------------------------------
async def part1_real_page() -> None:
    """D1, against the page that caused it."""
    print("\n== 1. the real anikoto watch page, in a real browser ==")
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe
    from app.core.browser_session import BrowserSession

    session = await BrowserSession.open({"anikoto.cz"})
    try:
        await session.goto(WATCH_URL)
        obs = await dom_observe.observe(session.page)
        check(
            len(obs.elements) > 20,
            "the watch page loaded with real content",
            f"{len(obs.elements)} elements, title={obs.title[:60]!r}",
        )

        # The modal ships CLOSED (Bootstrap's .modal is display:none), so the
        # baseline must be "no credential form visible at all".
        passwords = [e for e in obs.elements if (e.role or "").lower() == "password"]
        check(
            not passwords,
            "closed: no password field is listed while the modal is hidden",
            f"{len(passwords)} password field(s) visible",
        )
        check(
            browser_loop.detect_login_wall(obs) is None,
            "closed: an ordinary watch page is not a wall",
        )

        # Now open it the way the site does — this is the state that ended the
        # live task. Bootstrap 5 is loaded on the page; fall back to adding the
        # classes by hand if the global is not exposed.
        opened = await session.page.evaluate(
            """() => {
              const el = document.querySelector('#sign');
              if (!el) return 'no #sign modal on this page';
              try {
                if (window.bootstrap && window.bootstrap.Modal) {
                  window.bootstrap.Modal.getOrCreateInstance(el).show();
                  return 'shown via bootstrap';
                }
              } catch (e) {}
              el.classList.add('show');
              el.style.display = 'block';
              return 'shown by hand';
            }"""
        )
        print(f"         (modal: {opened})")
        bootstrap_shown = "bootstrap" in str(opened)
        await asyncio.sleep(1.0)
        obs_open = await dom_observe.observe(session.page)

        passwords = [e for e in obs_open.elements if (e.role or "").lower() == "password"]
        check(
            bool(passwords),
            "open: the sign-in modal's password field is now visible",
            f"{len(passwords)} password field(s)",
        )
        check(
            all(e.in_dialog for e in passwords),
            "open: and every one of them is marked in_dialog",
            f"in_dialog = {[e.in_dialog for e in passwords]}",
        )
        own = [e for e in obs_open.elements if not e.in_dialog]
        check(
            len(own) >= 5,
            "the page still has content of its own behind the modal",
            f"{len(own)} non-dialog elements",
        )

        # THE INCIDENT, at the exact moment it happened.
        site = browser_loop.credential_overlay_site(obs_open)
        check(site == "anikoto.cz", "the verdict is OVERLAY", f"site = {site!r}")
        check(
            browser_loop.detect_login_wall(obs_open) is None,
            "and NOT a wall — the browse would have carried on",
        )

        # And it is genuinely dismissible, which is what makes that verdict right.
        #
        # ⚠️ ONLY MEANINGFUL IF THE SITE'S OWN CODE OPENED IT. A modal forced open
        # by adding the classes by hand has no Bootstrap instance listening for
        # Escape, so a failure there would say nothing about the real site — and
        # reporting it as one would be the probe manufacturing a defect, which
        # this project has done three times. Stated rather than faked.
        await session.page.keyboard.press("Escape")
        await asyncio.sleep(1.0)
        obs_after = await dom_observe.observe(session.page)
        dismissed = browser_loop.credential_overlay_site(obs_after) is None
        if bootstrap_shown:
            check(
                dismissed,
                "Escape closed it — a wall is precisely the form you cannot dismiss",
            )
        else:
            # NOT recorded as a check at all. A tautological "assertion" here
            # would be a pass that cannot fail, which is worse than no check.
            print(
                "  [SKIP] Escape-dismissal: the modal was forced open by hand, so "
                "no key handler was ever attached\n"
                f"         (it {'did' if dismissed else 'did NOT'} clear — either "
                "way that is the fixture, not the site)"
            )
    finally:
        await session.close()


async def part2_real_listing() -> None:
    """D3, against the listing the model had to choose from."""
    print("\n== 2. the real search results, and 'season 4' ==")
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe
    from app.core.browser_session import BrowserSession

    session = await BrowserSession.open({"anikoto.cz"})
    try:
        await session.goto(SEARCH_URL)
        obs = await dom_observe.observe(session.page)
        hrefs, _labels, _names = browser_loop._season_entry_index(obs)
        check(
            len(hrefs) >= 5,
            "the listing carries the franchise's entries",
            f"{len(hrefs)} entries, e.g. {sorted(hrefs)[:3]}",
        )

        number = browser_loop._target_season(USER_WORDS)
        check(number == 4, "the goal's season is read as 4", f"got {number!r}")
        title = browser_loop._extract_search_term(USER_WORDS)
        check(
            title == "my hero academia",
            "and its title is the bare series name",
            f"got {title!r}",
        )

        # ⚠️ THIS PAGE IS WHY THE SERIES-MEMBERSHIP RULE EXISTS. anikoto's search
        # is FUZZY: these 40 rows span a dozen franchises, so before that rule
        # "Season 4" tied the real entry with `that-time-i-got-reincarnated-as-a-
        # slime-season-4` and the loop asked the user to choose between two
        # unrelated shows. The runtime check found that; no test had.
        slugs = set(hrefs)
        check(
            any("slime" in s for s in slugs) or any("hitorijime" in s for s in slugs),
            "the listing really does carry other shows (the fuzzy-search hazard)",
            f"e.g. {[s for s in sorted(slugs) if 'my-hero-academia' not in s][:3]}",
        )

        action = browser_loop._season_entry_action(
            obs, title or "", browser_loop.season_label(number or 0)
        )
        check(action is not None, "season 4 resolves to exactly ONE entry")
        url = (action or {}).get("url", "")
        check("my-hero-academia-4" in url, "and it is the season, not a decoy", url)
        check("the-movie" not in url, "never the film carrying the same digit", url)
        check("slime" not in url, "and never another show that has a season 4", url)
    finally:
        await session.close()


async def part3_readiness() -> None:
    """D4, as a property of the shipped constants."""
    print("\n== 3. the readiness busy exit ==")
    from app.browser import session as sess_mod

    check(
        sess_mod.READY_BUSY_MS / 1000.0 > 6.36,
        "the busy exit waits longer than the slowest page measured to settle",
        f"READY_BUSY_MS = {sess_mod.READY_BUSY_MS}",
    )
    check(
        54 < sess_mod.READY_BUSY_ACTS < 170,
        "the act floor sits between daraz's thin state and anikoto's busy one",
        f"READY_BUSY_ACTS = {sess_mod.READY_BUSY_ACTS}",
    )
    check(
        sess_mod.READY_BUSY_MS < sess_mod.READY_POLL_MS,
        "and it fires before the deadline, or it is not an exit",
        f"{sess_mod.READY_BUSY_MS} < {sess_mod.READY_POLL_MS}",
    )
    check(
        "substantive-but-busy" in Path(sess_mod.__file__).read_text(encoding="utf-8"),
        "the exit is wired into _await_readiness, not merely defined",
    )


async def part4_wiring() -> None:
    """D2: the hand-off path is reachable in the real process."""
    print("\n== 4. the login hand-off wiring ==")
    import app.tools  # noqa: F401 — registers the real tools
    from app.browser import session as browser_session
    from app.browser import state as browse_state

    check(
        hasattr(browser_session.BrowserSession, "release_to_user"),
        "a session can be handed to the user in place",
    )
    check(
        callable(getattr(browser_session, "handed_over_recently", None))
        and callable(getattr(browser_session, "note_handoff", None)),
        "and the escalation memory takes a KIND, rather than being copied",
    )
    # A challenge hand-off must not make a login wall look already-handled.
    browser_session.reset_challenge_handoffs()
    browser_session.note_handoff("anikoto.cz", "challenge")
    check(
        browser_session.handed_over_recently("anikoto.cz", "challenge") is True
        and browser_session.handed_over_recently("anikoto.cz", "login") is False,
        "the two kinds are keyed apart — neither escalates the other",
    )
    browser_session.reset_challenge_handoffs()

    payload = browse_state.handoff_from_flags(
        {
            "login_required": True,
            "login_site": "anikoto.cz",
            "login_in_place": True,
            "wall_kind": "login",
        }
    )
    check(
        payload is not None and payload.in_place is True,
        "in_place survives into the payload the pause text reads",
    )

    from app.agents.planner import _login_wall_question

    question = _login_wall_question(
        {"login_site": "anikoto.cz", "login_window_opened": True,
         "login_in_place": True, "wall_kind": "login"}
    )
    check(
        "already on your screen" in question.text
        and "i've opened a sign-in window" not in question.text.lower(),
        "and the pause text points AT the tab instead of claiming a new window",
    )


async def main() -> int:
    print("Runtime verification — 2026-08-08 browser round")
    print("(real Chrome, real anikoto, no LLM, nothing signed into)")
    try:
        await part1_real_page()
        await part2_real_listing()
        await part3_readiness()
        await part4_wiring()
    finally:
        from app.browser import window as browser_window

        try:
            await browser_window.close_all()
        except Exception as exc:
            print(f"  (cleanup: {type(exc).__name__}: {exc})")

    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 62)
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {label}")
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    # THE SELECTOR LOOP, on purpose: it is what uvicorn installs in production,
    # and a Proactor-by-default asyncio.run() would hide the Playwright
    # subprocess class of bug that has shipped here twice. run_browser marshals
    # the real work onto the browser runtime's own Proactor thread.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from app.core.browser_runtime import run_browser, shutdown_browser_runtime

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        code = loop.run_until_complete(run_browser(main()))
    finally:
        try:
            loop.run_until_complete(shutdown_browser_runtime())
        except Exception:
            pass
        loop.close()
    raise SystemExit(code)
