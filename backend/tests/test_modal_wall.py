"""
A login MODAL is not a login WALL — the 2026-08-08 incident, frozen.

Live: "play ep 4 of season 4 of my hero academia on anikoto". A normal Chrome
window opened on season 4 EPISODE 1 (not 4), the chat asked "Continue without
signing in", and answering that closed the browser and re-ran the whole browse
from the homepage before finally reaching ep 4.

⚠️ THE FAKE PAGE IS THE REAL PAGE. Fetching
https://anikoto.cz/watch/my-hero-academia-4-mt2j9/ep-1 during the post-mortem
returned THREE `type="password"` inputs — a sign-in modal and a register modal —
inside `<div class="modal fade" id="sign" aria-hidden="true">`. So the site ships
a credential form on EVERY watch page, and detect_login_wall walls on any visible
password field: the moment one of those modals was open, the task was over.

The discriminator is that A WALL IS NOT DISMISSIBLE. A dialog-borne credential
form on a page with content of its own is an OFFER — the loop presses Escape (an
action it has always had; it just never got the chance, because the wall check
returned before any decision) and carries on. A dialog that survives that is a
wall, and hands off exactly as before.

The JS half — that the real extractor marks a modal's password field `in_dialog`
against a real Chromium — lives in test_browser_extract_js.py, because a string
test cannot tell you whether a program works.
"""
import pytest

from app.agents import browser_loop
from app.agents.browser_loop import (
    credential_overlay_site,
    detect_login_wall,
    run_browse,
)

from test_browser_loop import (  # the established fakes — one shape, one place
    FakeProvider,
    FakeSession,
    ScriptedPage,
    _el,
    _page,
)

def _obs(payload):
    """A payload dict → an Observation, THROUGH the real _elements_of. Building
    Element objects by hand here would bypass the very plumbing D1 adds (the JS
    record's `in_dialog` reaching the dataclass), and a test that skips the code
    under test proves nothing."""
    dom = browser_loop.dom_observe
    elements = dom._elements_of(payload)
    return dom.Observation(
        observation_id="o",
        url=payload["url"],
        title=payload.get("title", ""),
        element_total=len(elements),
        elements=elements,
        page_text=payload.get("text", ""),
        text_truncated=False,
    )


USER_WORDS = "play ep 4 of season 4 of my hero academia on anikoto"
PLANNER_GOAL = "Find My Hero Academia Season 4 Episode 4 on anikoto and play it"
WATCH_URL = "https://anikoto.cz/watch/my-hero-academia-4-mt2j9/ep-1"


def _page_own_elements():
    """anikoto's watch page behind the modal: nav plus the episode list. These
    are what make it a CONTENT page rather than a sign-in page."""
    return [
        _el(1, "link", "Anikoto", href="/"),
        _el(2, "link", "Browse", href="/filter"),
        _el(3, "link", "Episode 1", href="/watch/my-hero-academia-4-mt2j9/ep-1"),
        _el(4, "link", "Episode 2", href="/watch/my-hero-academia-4-mt2j9/ep-2"),
        _el(5, "link", "Episode 3", href="/watch/my-hero-academia-4-mt2j9/ep-3"),
        _el(6, "link", "Episode 4", href="/watch/my-hero-academia-4-mt2j9/ep-4"),
    ]


def _modal_elements(start=7):
    """The `#sign` modal's own controls — the real markup's login pane."""
    return [
        _el(start, "input", "Email", in_dialog=True),
        _el(start + 1, "password", "Password", in_dialog=True),
        _el(start + 2, "button", "Login", in_dialog=True),
    ]


def _watch_page_with_modal(url=WATCH_URL):
    return _page(
        _page_own_elements() + _modal_elements(),
        url=url,
        title="Anikoto - My Hero Academia 4 Episode 1 Watch Anime Online",
    )


def _watch_page_clean(url=WATCH_URL):
    """The same page after Escape closed the dialog."""
    return _page(
        _page_own_elements(),
        url=url,
        title="Anikoto - My Hero Academia 4 Episode 1 Watch Anime Online",
    )


EP4_URL = "https://anikoto.cz/watch/my-hero-academia-4-mt2j9/ep-4"


def _watch_page_ep4():
    """Where the goal was always headed. The title↔URL agreement is what
    _current_episode requires to call the arrival proven."""
    return _page(
        _page_own_elements(),
        url=EP4_URL,
        title="Anikoto - My Hero Academia 4 Episode 4 Watch Anime Online",
    )


# ------------------------------------------------------------- the incident
def test_the_incident_a_sign_in_modal_is_not_a_wall():
    """THE DEFECT, at unit level. Every password field on the page is inside the
    `#sign` modal and the page has its own content behind it, so this is an
    offer — and before this the browse aborted here."""
    obs = _obs(_watch_page_with_modal())

    assert credential_overlay_site(obs) == "anikoto.cz"
    assert detect_login_wall(obs) is None, (
        "a sign-in modal on a usable page still reads as a hard wall"
    )


def test_a_real_sign_in_page_is_still_a_wall():
    """THE REGRESSION THAT MATTERS. A credential form in the page's OWN flow is
    unchanged — this is what the demotion must never reach."""
    obs = _obs(
        _page(
            [
                _el(1, "input", "Email"),
                _el(2, "password", "Password"),
                _el(3, "button", "Sign in"),
            ],
            url="https://shop.test/account",
        )
    )
    assert credential_overlay_site(obs) is None
    assert detect_login_wall(obs) == ("login", "shop.test")


def test_a_login_page_rendered_as_a_dialog_is_still_a_wall():
    """The second half of the condition, and it is load-bearing: a sign-in page
    whose form sits in a `.modal`-classed wrapper over an empty backdrop is a
    wall, because there the dialog IS the page. Without the "page has content of
    its own" test, every such page would be demoted."""
    obs = _obs(
        _page(
            [
                _el(1, "input", "Email", in_dialog=True),
                _el(2, "password", "Password", in_dialog=True),
                _el(3, "button", "Sign in", in_dialog=True),
                _el(4, "link", "Forgot?", in_dialog=True),
            ],
            url="https://shop.test/account",
        )
    )
    assert credential_overlay_site(obs) is None
    assert detect_login_wall(obs) == ("login", "shop.test")


def test_an_auth_route_is_never_demoted():
    """The URL is the page's OWN identity: an address that says "this page is for
    signing up" is a wall however the form is styled."""
    obs = _obs(
        _page(
            _page_own_elements()
            + [
                _el(7, "input", "Email", in_dialog=True),
                _el(8, "password", "Password", in_dialog=True),
            ],
            url="https://shop.test/register",
        )
    )
    assert credential_overlay_site(obs) is None
    assert detect_login_wall(obs) == ("signup", "shop.test")


def test_an_auth_host_is_never_demoted():
    """A dedicated sign-in host is the whole page, whatever its markup says."""
    obs = _obs(
        _page(
            _page_own_elements() + [_el(7, "password", "Password", in_dialog=True)],
            url="https://accounts.google.com/signin/v2",
        )
    )
    assert credential_overlay_site(obs) is None
    assert detect_login_wall(obs) == ("login", "accounts.google.com")


def test_a_page_with_no_credential_form_is_not_an_overlay():
    """An ordinary content page must cost nothing — no dismissal, no wall."""
    obs = _obs(_watch_page_clean())
    assert credential_overlay_site(obs) is None
    assert detect_login_wall(obs) is None


# ------------------------------------------------------- through the loop
@pytest.mark.asyncio
async def test_the_loop_dismisses_the_dialog_and_carries_on():
    """THE INCIDENT END TO END, and it asserts the OUTCOME the user asked for:
    dismiss the dialog, then reach EPISODE 4. Before this the browse aborted at
    the first page, opened a normal window on episode 1, and asked a question
    whose only sensible answer was "carry on"."""
    page = ScriptedPage(
        [_watch_page_with_modal(), _watch_page_clean(), _watch_page_ep4()]
    )
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"on the episode"}'])

    out = await run_browse(
        session, PLANNER_GOAL, provider=provider, intent_text=USER_WORDS
    )

    assert out.login_required is False, "the browse still aborted on a dismissible modal"
    assert out.success is True
    kinds = [(k, v) for _, _, k, v in page.acted]
    assert ("key", "Escape") in kinds, f"the dialog was never dismissed: {page.acted}"
    assert page.url == EP4_URL, (
        f"ended on {page.url} — the run was asked for episode 4"
    )


@pytest.mark.asyncio
async def test_a_dialog_that_survives_escape_is_a_wall():
    """THE BOUND, and it is what keeps the detector honest in both directions.
    The same page twice: Escape changed nothing, so the form is not dismissible —
    which is exactly what a wall is. It hands off through the ordinary door."""
    page = ScriptedPage([_watch_page_with_modal(), _watch_page_with_modal()])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'])

    out = await run_browse(
        session, PLANNER_GOAL, provider=provider, intent_text=USER_WORDS
    )

    assert out.login_required is True, "an undismissable credential form must wall"
    assert out.login_site == "anikoto.cz"
    assert out.wall_kind == "login"


@pytest.mark.asyncio
async def test_the_dismissal_is_bounded():
    """A page whose fingerprint churns for unrelated reasons must never spin the
    loop on Escape. The cap is the belt behind the fingerprint set."""
    pages = [
        _page(
            _page_own_elements() + _modal_elements(),
            url=f"{WATCH_URL}?t={n}",  # a fresh fingerprint every time
            title=f"Episode 1 — {n} watching",
        )
        for n in range(8)
    ]
    page = ScriptedPage(pages)
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'] * 8)

    out = await run_browse(
        session, PLANNER_GOAL, provider=provider, intent_text=USER_WORDS
    )

    escapes = [v for _, _, k, v in page.acted if k == "key"]
    assert len(escapes) <= browser_loop._MAX_OVERLAY_DISMISSALS, (
        f"pressed Escape {len(escapes)} times — the cap is not holding"
    )
    assert out.login_required is True, "and it still ends honestly, not in a spin"


@pytest.mark.asyncio
async def test_skip_login_wall_still_clears_the_dialog_but_never_walls():
    """skip_login_wall means "do not STOP for a wall", not "leave a modal sitting
    over the page" — an open dialog occludes real controls, so clearing it helps
    every run. Only the hand-off is suppressed."""
    page = ScriptedPage([_watch_page_with_modal(), _watch_page_with_modal()])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'] * 4)

    out = await run_browse(
        session,
        PLANNER_GOAL,
        provider=provider,
        intent_text=USER_WORDS,
        skip_login_wall=True,
    )

    kinds = [(k, v) for _, _, k, v in page.acted]
    assert ("key", "Escape") in kinds, "the dialog was left occluding the page"
    assert out.login_required is False, "a skipped run must never wall"


@pytest.mark.asyncio
async def test_a_surviving_signup_dialog_is_messaged_as_a_signup_wall():
    """The kind is RE-READ when a dialog survives, not assumed "login". An
    account-creation form that will not close is a sign-up wall, and the pause
    text the user reads must say so — they are being asked to do different
    things."""
    signup_modal = _page(
        _page_own_elements()
        + [
            _el(7, "input", "Email address", in_dialog=True),
            _el(8, "password", "Password", in_dialog=True),
            _el(9, "button", "Create account", in_dialog=True),
        ],
        url=WATCH_URL,
        title="Anikoto - My Hero Academia 4 Episode 1 Watch Anime Online",
    )
    page = ScriptedPage([signup_modal, signup_modal])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'])

    out = await run_browse(
        session, PLANNER_GOAL, provider=provider, intent_text=USER_WORDS
    )

    assert out.login_required is True
    assert out.wall_kind == "signup", f"messaged as {out.wall_kind!r}"
