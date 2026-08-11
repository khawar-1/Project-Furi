"""
Phase 14 Part 2 — the browse loop: bounded, terminal, non-spinning, and cheap.

These pin the properties that make an LLM-driven browser safe to run in the
background: it stops (action cap), it does not spin on a dead button (dedupe —
the ended-stream bug), the fast path searches the title with no model call, a
hallucinated index cannot be acted on, and the media registry keeps exactly one
window playing.
"""
import asyncio
import re

import pytest

from app.agents import browser_loop
from app.agents.browser_loop import (
    BrowseOutcome,
    _EXTRACT_MAX_RECORDS,
    _coerce_record,
    _current_episode,
    _episode_action,
    _extract_data,
    _extract_search_term,
    _extract_what,
    _fast_path_action,
    _memory_block,
    _href_latest_episode,
    _latest_episode_action,
    _latest_series_action,
    _max_or_none,
    _parse_action,
    _range_expand_action,
    _range_latest,
    _resolve_latest_episode,
    _swap_episode_in_url,
    _target_episode,
    _wants_latest_episode,
    detect_auth_offer,
    run_browse,
)
from app.core import browser_session
from app.providers.base import LLMResponse


def _ep_obs(url, title):
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title=title, element_total=0,
        elements=[], page_text="", text_truncated=False,
    )


# ---------------------------------------- deterministic episode navigation (Fix 1+2)
def test_target_episode_reads_the_number_or_none():
    assert _target_episode("play episode 170 of black clover on anikoto.cz") == 170
    assert _target_episode("play ep 4 of my hero academia on anikoto.cz") == 4
    assert _target_episode("watch epi 12 of naruto") == 12
    assert _target_episode("play s2e7 of demon slayer") == 7
    # a worded "last episode" carries no number → the model's/vision's job, not this path
    assert _target_episode("play the last episode of black clover") is None
    # no episode named at all
    assert _target_episode("play black clover") is None
    # a bare title number is not an episode qualifier
    assert _target_episode("play blink 182 videos") is None


def test_current_episode_needs_title_and_url_to_agree():
    # title says Episode 87 AND the URL carries 87 → proven current episode
    assert _current_episode(
        _ep_obs("https://anikoto.cz/watch/black-clover-g7tjy/ep-87", "Watch Black Clover Episode 87")
    ) == 87
    # title says an episode the URL does NOT carry → not proven (None)
    assert _current_episode(
        _ep_obs("https://anikoto.cz/watch/black-clover-g7tjy/", "Watch Black Clover Episode 87")
    ) is None
    # not an episode page at all
    assert _current_episode(
        _ep_obs("https://anikoto.cz/browse", "Browse & Filter - Anikoto")
    ) is None


def test_current_episode_never_trusts_an_intent_search_host():
    """The 'humrahi' live miss (2026-07-25): searching 'humrahi episode 35' landed
    on youtube.com/results?search_query=humrahi+episode+35 with title
    'humrahi episode 35 - YouTube' — the number echoed into BOTH title and URL, so
    the catalog title↔URL proof false-fired and the loop declared the RESULTS page
    done. Intent hosts never number episodes in their URLs, so the proof is void
    there and _top_result_action owns the pick."""
    # The exact trap: title AND url both carry 35, yet this is a SEARCH page.
    assert _current_episode(
        _ep_obs(
            "https://www.youtube.com/results?search_query=humrahi+episode+35",
            "humrahi episode 35 - YouTube",
        )
    ) is None
    # Even a genuine YouTube watch page is not read as a catalog episode page.
    assert _current_episode(
        _ep_obs("https://www.youtube.com/watch?v=abc35", "Humrahi Episode 35 [Eng Sub]")
    ) is None
    # Google likewise.
    assert _current_episode(
        _ep_obs("https://www.google.com/search?q=humrahi+episode+35", "humrahi episode 35 - Google Search")
    ) is None
    # A catalog host with the same title+URL agreement is STILL proven (regression).
    assert _current_episode(
        _ep_obs("https://anikoto.cz/watch/humrahi-x/ep-35", "Watch Humrahi Episode 35")
    ) == 35


def test_swap_episode_replaces_only_the_last_standalone_number():
    # the coincidental "7" in the slug g7tjy is left alone; the ep number swaps
    assert _swap_episode_in_url(
        "https://anikoto.cz/watch/black-clover-g7tjy/ep-87", 87, 170
    ) == "https://anikoto.cz/watch/black-clover-g7tjy/ep-170"
    assert _swap_episode_in_url(
        "https://anikoto.cz/watch/my-hero-academia-kuzfp/ep-1", 1, 4
    ) == "https://anikoto.cz/watch/my-hero-academia-kuzfp/ep-4"
    assert _swap_episode_in_url("https://x/none-here", 5, 9) is None


def test_episode_action_navigates_to_the_target_by_url():
    # on Episode 87, want 170 → navigate to ep-170 (bypasses the range dropdown)
    action = _episode_action(
        "play episode 170 of black clover on anikoto.cz",
        _ep_obs("https://anikoto.cz/watch/black-clover-g7tjy/ep-87", "Black Clover Episode 87"),
    )
    assert action == {
        "action": "navigate",
        "url": "https://anikoto.cz/watch/black-clover-g7tjy/ep-170",
    }


def test_episode_action_finishes_when_already_on_the_target():
    # on Episode 4, want 4 → done (stops the over-click-then-wander cascade)
    action = _episode_action(
        "play ep 4 of my hero academia on anikoto.cz",
        _ep_obs("https://anikoto.cz/watch/my-hero-academia-kuzfp/ep-4", "My Hero Academia Episode 4"),
    )
    assert action is not None and action["action"] == "done"


def test_episode_action_none_when_no_number_or_not_on_an_episode_page():
    # no number in the goal
    assert _episode_action(
        "play the last episode of black clover",
        _ep_obs("https://anikoto.cz/watch/black-clover-g7tjy/ep-87", "Black Clover Episode 87"),
    ) is None
    # numbered goal but not on a recognizable episode page yet
    assert _episode_action(
        "play episode 170 of black clover",
        _ep_obs("https://anikoto.cz/browse", "Browse & Filter - Anikoto"),
    ) is None


def _auth_obs(url, *elements):
    # Each element is (role, name) or (role, name, href).
    els = [
        browser_loop.dom_observe.Element(
            index=i + 1, role=e[0], name=e[1], href=(e[2] if len(e) > 2 else "")
        )
        for i, e in enumerate(elements)
    ]
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title="", element_total=len(els),
        elements=els, page_text="", text_truncated=False,
    )


# ------------------------------------------- OPTIONAL sign-in offer (2026-07-19)
def test_detect_auth_offer_reads_signin_and_signup_links():
    signin, signup, site = detect_auth_offer(
        _auth_obs("https://jobs.test/apply", ("link", "Sign in"), ("textbox", "Name"))
    )
    assert signin and not signup and site == "jobs.test"

    signin, signup, _ = detect_auth_offer(
        _auth_obs("https://jobs.test/apply", ("button", "Create an account"))
    )
    assert signup and not signin

    signin, signup, _ = detect_auth_offer(
        _auth_obs("https://jobs.test/apply", ("link", "Log in"), ("link", "Register"))
    )
    assert signin and signup


def test_detect_auth_offer_ignores_non_links_and_plain_pages():
    # "Sign in" as page TEXT (not a link/button role) is not an offer.
    assert detect_auth_offer(_auth_obs("https://x.test/", ("textbox", "Sign in below"))) is None
    # A plain form with no auth affordance.
    assert detect_auth_offer(
        _auth_obs("https://x.test/apply", ("textbox", "Email"), ("button", "Submit"))
    ) is None


def test_detect_auth_offer_ignores_third_party_account_links():
    """SAME-SITE ONLY (2026-07-21): an auth affordance whose href points to
    ANOTHER registrable domain is a third-party account offer ("Sign in with
    Google", a newsletter "Sign up"), not this site's wall — never interrupt."""
    # "Sign in with Google" pointing at accounts.google.com — third party.
    assert detect_auth_offer(
        _auth_obs(
            "https://jobs.test/apply",
            ("link", "Sign in with Google", "https://accounts.google.com/o/oauth2/x"),
        )
    ) is None
    # A third-party "Sign up" (marketing/newsletter) on a subdomain of another site.
    assert detect_auth_offer(
        _auth_obs(
            "https://www.linkedin.com/in/anas",
            ("link", "Sign up", "https://mailer.thirdparty.io/register"),
        )
    ) is None


def test_detect_auth_offer_counts_same_site_links():
    """A same-registrable-domain href (relative or on a sibling subdomain) IS
    this site's own offer and still counts."""
    # Relative href — resolves to the same host.
    signin, signup, site = detect_auth_offer(
        _auth_obs("https://jobs.test/apply", ("link", "Sign in", "/login"))
    )
    assert signin and not signup and site == "jobs.test"
    # Sibling subdomain of the same registrable domain.
    _, signup, _ = detect_auth_offer(
        _auth_obs(
            "https://www.linkedin.com/in/anas",
            ("link", "Join now", "https://secure.linkedin.com/signup"),
        )
    )
    assert signup


async def test_commit_loop_pauses_on_an_optional_signin_offer():
    """In commit mode the loop STOPS on a page that offers sign in / sign up and
    returns auth_offer_required WITHOUT deciding — the user chooses. No LLM call
    (the check is before decide)."""
    page = _page([_el(1, role="link", name="Sign in"), _el(2, role="button", name="Apply")],
                 url="https://jobs.test/apply")
    session = FakeSession(ScriptedPage([page]))
    session.allowlist = {"jobs.test"}
    provider = FakeProvider([])

    outcome = await run_browse(session, "apply to the job", provider, commit=True)

    assert outcome.auth_offer_required is True
    assert outcome.auth_offer_signin is True
    assert outcome.auth_offer_url == "https://jobs.test/apply"
    assert provider.calls == 0  # asked before any decision


async def test_a_resolved_page_does_not_re_ask_the_signin_offer():
    """Once the user has decided the offer for a page (auth_resolved), the loop
    proceeds past it — the "every distinct page once" rule, so 'apply as guest'
    never re-asks the same page forever."""
    page = _page([_el(1, role="link", name="Sign in"), _el(2, role="button", name="Apply")],
                 url="https://jobs.test/apply")
    session = FakeSession(ScriptedPage([page]))
    session.allowlist = {"jobs.test"}
    provider = FakeProvider(['{"action":"done","reason":"applied"}'])

    outcome = await run_browse(
        session, "apply to the job", provider, commit=True,
        auth_resolved={"https://jobs.test/apply"},
    )
    assert outcome.auth_offer_required is False
    assert provider.calls == 1  # it went on to decide instead of asking


async def test_a_read_only_browse_ignores_a_signin_link():
    """The offer is a COMMIT-mode concern (an application). A read-only browse
    (play a video) never pauses on a header 'Sign in'."""
    page = _page([_el(1, role="link", name="Sign in"), _el(2, role="link", name="A video", href="/v")],
                 url="https://vids.test/")
    session = FakeSession(ScriptedPage([page]))
    session.allowlist = {"vids.test"}
    provider = FakeProvider(['{"action":"done","reason":"done"}'])

    outcome = await run_browse(session, "watch a video", provider)  # commit=False
    assert outcome.auth_offer_required is False


# --------------------------------------------------------------- fake driver
class FakeHandle:
    def __init__(self, page, index):
        self.page = page
        self.index = index

    async def fill(self, text):
        self.page.record(self.index, "fill", text)

    async def click(self):
        self.page.record(self.index, "click", None)

    async def press(self, key):
        self.page.record(self.index, "press", key)

    async def select_option(self, label=None, value=None):
        self.page.record(self.index, "select", label or value)

    async def hover(self):
        self.page.record(self.index, "hover", None)

    async def get_attribute(self, name):
        if name == "href":
            for e in self.page._current().get("elements", []):
                if e["index"] == self.index:
                    return e.get("href") or ""
        return None

    async def query_selector_all(self, selector):
        """A <select>'s own <option>s, the way a real ElementHandle returns them.

        Modelled rather than stubbed because the distinction MATTERS: the
        2026-08-02 option-choice gate reads these to offer the page's real
        labels, and a fake that always returned [] could not tell "this control
        offers 100/50/20 ML" from "this control is unreadable" — which are the
        two branches of that gate."""
        if selector != "option":
            return []
        for e in self.page._current().get("elements", []):
            if e["index"] == self.index:
                return [FakeOption(t) for t in (e.get("options") or [])]
        return []


class FakeOption:
    """One <option> node — only inner_text() is ever read off it."""

    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text


class FakeKeyboard:
    """Page-level key input, the shape _act's press_key path reaches for."""

    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.record(None, "key", key)


class ScriptedPage:
    """A page that advances to the NEXT scripted payload whenever an action
    navigates (a click or an Enter). `acted` records every action taken."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.i = 0
        self.url = payloads[0].get("url", "https://site.test/")
        self.acted = []

    def _current(self):
        return self.payloads[min(self.i, len(self.payloads) - 1)]

    async def evaluate(self, js, arg=None):
        # One shape for every evaluate the stack issues (observe, marks,
        # scroll): return the current scripted payload; nothing advances.
        payload = dict(self._current())
        payload.setdefault("url", self.url)
        self.url = payload["url"]
        return payload

    async def query_selector(self, selector):
        m = re.search(r'idx="(\d+)"', selector)
        idx = int(m.group(1)) if m else -1
        present = {e["index"] for e in self._current().get("elements", [])}
        return FakeHandle(self, idx) if idx in present else None

    async def wait_for_load_state(self, *a, **k):
        pass

    @property
    def keyboard(self):
        """A real Playwright Page has one; without it _act's press_key path takes
        its "keyboard input is unavailable" branch and every Escape silently
        fails (2026-08-08). A fake that cannot express the contract cannot test
        it — the shape this codebase has now shipped four times."""
        return FakeKeyboard(self)

    def record(self, index, kind, value):
        self.acted.append((self.i, index, kind, value))
        # A click, an Enter, or a page-level key press is a change — advance to
        # the next scripted page. For a key press that is the point: Escape
        # closing a dialog changes the DOM, and a test where the dialog SURVIVES
        # simply scripts the same payload twice.
        if kind in ("click", "press", "key") and self.i < len(self.payloads) - 1:
            self.i += 1
            self.url = self.payloads[self.i].get("url", self.url)

    def navigate(self, url):
        """A GET navigation (session.goto), e.g. opening a link's href — advance
        to the next scripted page, mirroring a click."""
        self.acted.append((self.i, None, "goto", url))
        if self.i < len(self.payloads) - 1:
            self.i += 1
            self.url = self.payloads[self.i].get("url", url)
        else:
            self.url = url


class FakeStats:
    def __init__(self, **over):
        self._d = {
            "blocked_mutations": 0,
            "blocked_navigations": 0,
            "blocked_hosts": 0,
            "mutation_urls": [],
        }
        self._d.update(over)

    def as_dict(self):
        return dict(self._d)


class FakeSession:
    def __init__(self, page, stats=None):
        self.page = page
        self.stats = stats or FakeStats()

    async def settle(self):
        pass

    async def goto(self, url):
        # A link is opened by navigating to its href (a GET) — see _act.
        self.page.navigate(url)


class FakeProvider:
    """Scripted decisions. Records how many times it was asked — the fast-path
    'costs no call' claim is a call-count assertion (the evidence_resolver thesis
    applied to browsing).

    Also records the PROMPTS. Whether the model was shown the thing it needed is
    the defect class that cost 2026-07-26: extraction was reading a 4000-char
    prose prefix while the products sat in the element list, and no call-count
    assertion could ever have seen that."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        self.prompts.append("\n".join(getattr(m, "content", "") or "" for m in messages))
        content = self.responses.pop(0) if self.responses else '{"action":"done","reason":"end"}'
        return LLMResponse(content=content, model="fake", provider="fake")


def _el(
    index, role="link", name="x", value="", href="", form=None, options=None,
    in_dialog=False, sold_out=False,
):
    item = {"index": index, "role": role, "name": name, "value": value, "href": href}
    if form is not None:
        item["form"] = form
    if options is not None:
        item["options"] = list(options)
    # DIALOG MEMBERSHIP (2026-08-08). The real extractor emits this for every
    # element; without it here no fake page could express "a password field
    # inside a modal", which is the whole of the D1 incident.
    if in_dialog:
        item["in_dialog"] = True
    # STOCK (2026-08-09), and for the same reason: the real extractor emits this
    # for every element, and without it no fake listing could express "six of
    # these twenty cannot be bought" — which is the whole of that incident. A
    # fake that cannot state the contract cannot test it.
    if sold_out:
        item["sold_out"] = True
    return item


def _page(elements, url="https://site.test/", title="T"):
    return {"url": url, "title": title, "elements": list(elements), "total": len(elements), "text": ""}


# ---------------------------------------------------------------- fast path
def test_extract_search_term():
    assert _extract_search_term("search 'jane by the long faces' and play it") == "jane by the long faces"
    assert _extract_search_term("search jane by the long faces on youtube") == "jane by the long faces"
    assert _extract_search_term("play lofi hip hop on youtube") == "lofi hip hop"
    assert _extract_search_term("") is None
    # A number that is not a media qualifier is part of the title.
    assert _extract_search_term("play blink 182 on youtube") == "blink 182"


def test_fast_path_types_the_title_not_the_media_descriptor():
    """The two 2026-07-22 incidents (anikoto): a media goal must SEARCH the title,
    not the "ep 4 … season 2" descriptor. Leading and trailing qualifier chains
    (and the s2e4 shorthand) are stripped to the bare title; the model navigates
    to the right season/episode from the results page."""
    assert _extract_search_term(
        "play ep 4 of the dangers in my heart season 2 on anikoto.cz"
    ) == "the dangers in my heart"
    assert _extract_search_term(
        "Play episode 1 of season 2 of The Dangers in My Heart"
    ) == "The Dangers in My Heart"
    assert _extract_search_term("watch s2e1 of demon slayer") == "demon slayer"
    assert _extract_search_term("play attack on titan season 4 episode 2") == "attack on titan"
    # A qualifier WORD with no number, or a bare "Part 1" title, is left alone.
    assert _extract_search_term("play part of me by katy perry") == "part of me by katy perry"
    assert _extract_search_term("watch lord of the rings") == "lord of the rings"


def test_fast_path_strips_worded_ordinal_episode_qualifiers():
    """The 2026-07-22 anikoto incident: "play the last episode of The Dangers in
    My Heart" searched that ENTIRE phrase because the numeric qualifier rule saw
    no number to strip. A WORDED ordinal ("last/latest/most recent … episode of")
    is stripped to the bare title so the model navigates from the results page."""
    assert _extract_search_term(
        "play the last episode of the dangers in my heart season 2 on anikoto.cz"
    ) == "the dangers in my heart"
    assert _extract_search_term(
        "play the last episode of The Dangers in My Heart"
    ) == "The Dangers in My Heart"
    assert _extract_search_term("play latest episode of one piece") == "one piece"
    assert _extract_search_term(
        "watch the most recent episode of frieren on crunchyroll"
    ) == "frieren"
    # TITLE SAFETY — an ordinal word NOT immediately followed by an episode/season
    # word is part of the title and must never be eaten.
    assert _extract_search_term("play The Last of Us") == "The Last of Us"
    assert _extract_search_term("watch The Last Airbender") == "The Last Airbender"
    assert _extract_search_term("play The First Slam Dunk") == "The First Slam Dunk"
    assert _extract_search_term(
        "play attack on titan the final season"
    ) == "attack on titan the final season"


def test_fast_path_strips_a_release_adjective_before_the_episode_word():
    """The 2026-07-24 black-clover incident: "last RELEASED ep of black clover" was
    typed VERBATIM into the search box (the adjective between the ordinal and the
    media word defeated the ordinal stripper) → landed on a /genre junk page → the
    run died. The whitelisted adjective slot strips it to the bare title."""
    assert _extract_search_term(
        "play last released ep of black clover on anikoto.cz"
    ) == "black clover"
    assert _extract_search_term(
        "play the latest released episode of black clover"
    ) == "black clover"
    assert _extract_search_term("watch the newest aired episode of naruto") == "naruto"
    # And the wants-latest GATE recognizes it, so the latest-number path starts.
    assert _wants_latest_episode("play last released ep of black clover on anikoto.cz")
    assert _wants_latest_episode("play the latest released episode of one piece")
    # TITLE SAFETY — none of the whitelisted adjectives begins a real title, and a
    # non-whitelisted word between the ordinal and the media word is left intact
    # (the phrase then simply isn't recognized as an ordinal — no over-eating).
    assert _extract_search_term("play The Last of Us") == "The Last of Us"
    assert _extract_search_term("watch The Last Airbender") == "The Last Airbender"


def _series_obs(url, *hrefs):
    """A results/series page: each href becomes a link element."""
    els = [
        browser_loop.dom_observe.Element(index=i + 1, role="link", name="", href=h)
        for i, h in enumerate(hrefs)
    ]
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title="Search results", element_total=len(els),
        elements=els, page_text="", text_truncated=False,
    )


def test_latest_series_action_builds_the_episode_url_from_one_matching_slug():
    """Fix B1 (2026-07-25): the number is known but we're not on a proven episode
    page — build …/watch/<slug>/ep-<latest> from the ONE series link whose slug
    contains every title token, and navigate. This is the leg the numbered path
    leans on the model for."""
    obs = _series_obs(
        "https://anikoto.cz/filter?keyword=Black+Clover",
        "/watch/black-clover-g7tjy",
        "/watch/naruto-shippuden-x/ep-1",
    )
    action = _latest_series_action(obs, "black clover", 170, set())
    assert action == {
        "action": "navigate",
        "url": "https://anikoto.cz/watch/black-clover-g7tjy/ep-170",
    }


def test_latest_series_action_picks_the_tightest_slug_over_a_movie_entry():
    """Fix B1 tiebreak (2026-07-25): a search page lists the TV series AND its
    movie under the same title. The canonical series is the TIGHTEST slug — fewest
    EXTRA tokens beyond the title (the id suffix 'g7tjy' is 1 extra; the movie's
    'mahou-tei-no-ken' is 4) — so code picks it instead of deferring to a confused
    model (the 2026-07-25 live 'couldn't even open black clover' failure)."""
    obs = _series_obs(
        "https://anikoto.cz/filter?keyword=Black+Clover",
        "/watch/black-clover-g7tjy",
        "/watch/black-clover-mahou-tei-no-ken",
    )
    action = _latest_series_action(obs, "black clover", 158, set())
    assert action == {
        "action": "navigate",
        "url": "https://anikoto.cz/watch/black-clover-g7tjy/ep-158",
    }


def test_latest_series_action_defers_on_a_genuine_tie_for_tightest():
    """Two equally-tight same-title entries (each carries exactly one extra id
    token) → a real tie → code never picks; None, so the model decides (with the
    number injected via B2)."""
    obs = _series_obs(
        "https://anikoto.cz/filter?keyword=One+Piece",
        "/watch/one-piece-abcde",
        "/watch/one-piece-vwxyz/ep-1",
    )
    assert _latest_series_action(obs, "one piece", 1122, set()) is None


def test_latest_series_action_none_without_a_number_or_when_already_tried():
    obs = _series_obs(
        "https://anikoto.cz/filter?keyword=Black+Clover",
        "/watch/black-clover-g7tjy",
    )
    # unknown number → nothing to build
    assert _latest_series_action(obs, "black clover", None, set()) is None
    # a target already attempted (a wrong count that did not land) is not retried
    assert _latest_series_action(obs, "black clover", 170, {170}) is None
    # no title tokens → cannot ground a slug match
    assert _latest_series_action(obs, "", 170, set()) is None
    # no series link on the page at all
    assert _latest_series_action(
        _series_obs("https://anikoto.cz/filter?keyword=Black+Clover", "/about"),
        "black clover", 170, set(),
    ) is None


# ------------------------------------ paginated episode-range selector (2026-07-25)
def _range_obs(url, title, *labels):
    """A page carrying episode-range controls (a '001-100' dropdown / tabs): each
    label becomes a clickable element with that visible name."""
    els = [
        browser_loop.dom_observe.Element(index=i + 1, role="button", name=label, href="")
        for i, label in enumerate(labels)
    ]
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title=title, element_total=len(els),
        elements=els, page_text="", text_truncated=False,
    )


def test_max_or_none():
    assert _max_or_none(None, None) is None
    assert _max_or_none(None, 5, None, 3) == 5
    assert _max_or_none(100, 170) == 170


def test_range_latest_reads_the_true_max_behind_the_dropdown():
    """The live 2026-07-25 miss: the grid shows 1-100 but the selector's option
    labels name '101-170'. The range labels give the true latest (170) even before
    the higher range is opened — which feeds the verify-before-done gate."""
    obs = _range_obs("https://anikoto.cz/watch/black-clover-g7tjy/ep-100", "Black Clover Episode 100",
                     "Sub & Dub", "001-100", "101-170")
    assert _range_latest(obs) == 170


def test_range_latest_is_anchored_so_a_year_or_price_filter_never_inflates_it():
    """A range needs a '1-N' anchor (an episode paginator's first range is always
    001-1xx). A lone '2020-2024' year filter has no such anchor → ignored, so it
    can never be mistaken for episode 2024 and strand the run."""
    assert _range_latest(_range_obs("https://x.test/", "T", "2020-2024")) is None
    assert _range_latest(_range_obs("https://x.test/", "T", "Newest", "Oldest")) is None
    # With the 1-anchored range present, the higher (real) range is trusted.
    assert _range_latest(_range_obs("https://x.test/", "T", "1-50", "51-90")) == 90


def test_range_expand_action_opens_the_highest_unopened_range():
    """Deterministically operate the selector: click the highest range not yet
    opened (the collapsed '001-100' toggle first, then '101-170'), tracking opened
    ranges so it never loops; None once every range has been opened."""
    obs = _range_obs("https://anikoto.cz/series", "Black Clover", "001-100", "101-170")
    opened: set = set()
    first = _range_expand_action(obs, opened)
    assert first == {"action": "click", "index": 2}  # 101-170 is the highest
    assert "101-170" in opened
    second = _range_expand_action(obs, opened)
    assert second == {"action": "click", "index": 1}  # then 001-100
    assert _range_expand_action(obs, opened) is None  # all opened → nothing to do


def test_range_expand_action_ignores_an_unanchored_filter():
    """No episode paginator (no 1-N range) → nothing to expand, so it never clicks a
    year/genre filter."""
    obs = _range_obs("https://x.test/", "T", "2020-2024", "Action")
    assert _range_expand_action(obs, set()) is None


def test_extract_search_term_handles_compound_planner_goals():
    """The planner authors compound browse goals ("<verb> and <verb> <title> on
    <site>") whose strippers did not compose: the greedy trailing-action rule ate
    "and play the latest episode of One Piece on anikoto.cz" and returned "Find"
    (live 2026-07-24). The verb CHAIN + pronoun-only trailing action fix it."""
    assert _extract_search_term(
        "Find and play the latest episode of One Piece on anikoto.cz"
    ) == "One Piece"
    assert _extract_search_term(
        "go to anikoto.cz and find the latest episode of one piece"
    ) == "one piece"
    assert _extract_search_term(
        "search and play attack on titan season 4 episode 2"
    ) == "attack on titan"
    # a genuine trailing throwaway action is still stripped
    assert _extract_search_term("open lofi hip hop and play it") == "lofi hip hop"
    # a title's own "and" is never treated as a verb chain
    assert _extract_search_term("play tom and jerry") == "tom and jerry"


# ------------------------------------------ latest-episode navigation (2026-07-24)
def _ep_obs_links(url, title, *hrefs):
    els = [
        browser_loop.dom_observe.Element(
            index=i + 1, role="link", name=f"Episode {i + 1}", href=h
        )
        for i, h in enumerate(hrefs)
    ]
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title=title, element_total=len(els),
        elements=els, page_text="", text_truncated=False,
    )


def test_wants_latest_episode_intent_matrix():
    assert _wants_latest_episode("play the latest episode of one piece on anikoto.cz")
    assert _wants_latest_episode("play latest episode of one piece")
    assert _wants_latest_episode("watch the newest episode of frieren")
    assert _wants_latest_episode("play the final episode of naruto")
    assert _wants_latest_episode("play the most recent episode of bleach")
    assert _wants_latest_episode("watch the latest season of demon slayer")
    # a concrete number is the numbered path, not this one
    assert not _wants_latest_episode("play episode 170 of black clover")
    # "first/next/previous" is a different target, not "latest"
    assert not _wants_latest_episode("play the first episode of one piece")
    assert not _wants_latest_episode("play the next episode")
    # a title that merely contains an ordinal word is not a latest-request
    assert not _wants_latest_episode("play The Last of Us")
    assert not _wants_latest_episode("watch attack on titan the final season")
    assert not _wants_latest_episode("play one piece")


async def test_resolve_latest_episode_parses_the_max_episode(monkeypatch):
    from app.tools import browser_tools

    def fake_search(query, max_results):
        assert "one piece" in query.lower()
        return [
            {"title": "Wiki", "snippet": "Episode 1120 aired last week.", "content": ""},
            {"title": "News", "snippet": "", "content": "The latest is Episode 1122 (2026)."},
        ]

    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", fake_search)
    assert await _resolve_latest_episode("one piece") == 1122


async def test_resolve_latest_episode_never_reads_a_year_and_is_best_effort(monkeypatch):
    from app.tools import browser_tools

    # A year (2026) is a BARE number, not "episode N" — never picked (the reverted
    # heuristic's exact failure). Nothing parseable → None.
    monkeypatch.setattr(
        browser_tools, "SEARCH_PROVIDER_FACTORY",
        lambda q, n: [{"title": "x", "snippet": "One Piece is popular in 2026.", "content": ""}],
    )
    assert await _resolve_latest_episode("one piece") is None
    # no title → no search, no crash
    assert await _resolve_latest_episode("") is None

    def boom(q, n):
        raise RuntimeError("provider down")

    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", boom)
    assert await _resolve_latest_episode("one piece") is None


def test_href_latest_episode_picks_the_max_sibling_link():
    obs = _ep_obs_links(
        "https://anikoto.cz/watch/one-piece-x/ep-1",
        "One Piece Episode 1",
        "https://anikoto.cz/watch/one-piece-x/ep-2",
        "/watch/one-piece-x/ep-15",                   # relative href, same series
        "https://anikoto.cz/watch/one-piece-x/ep-9",
        "https://anikoto.cz/watch/bleach-y/ep-500",   # different series — ignored
        "https://anikoto.cz/browse",                  # not an episode link
    )
    assert _href_latest_episode(obs) == 15
    # not on an /ep-N page → None
    assert _href_latest_episode(_ep_obs_links("https://anikoto.cz/browse", "Browse")) is None


def test_latest_episode_action_navigates_finishes_and_defers():
    on_ep1 = _ep_obs("https://anikoto.cz/watch/one-piece-x/ep-1", "One Piece Episode 1")
    # on ep-1, latest=1122 → navigate to ep-1122
    assert _latest_episode_action(on_ep1, 1122, set()) == {
        "action": "navigate",
        "url": "https://anikoto.cz/watch/one-piece-x/ep-1122",
    }
    # already on the latest → done
    on_latest = _ep_obs("https://anikoto.cz/watch/one-piece-x/ep-1122", "One Piece Episode 1122")
    a = _latest_episode_action(on_latest, 1122, set())
    assert a is not None and a["action"] == "done"
    # target already tried and we're NOT on it (a wrong count / 404) → defer, never loop
    assert _latest_episode_action(on_ep1, 1122, {1122}) is None
    # number unknown / not on an episode page → None
    assert _latest_episode_action(on_ep1, None, set()) is None
    assert _latest_episode_action(_ep_obs("https://anikoto.cz/browse", "Browse"), 1122, set()) is None


async def test_latest_episode_flow_web_number_then_url_swap(monkeypatch):
    """End to end: on ep-1 with a 'latest episode' goal, the concurrently-searched
    web number is swapped into the URL and the loop finishes on it — no LLM call."""
    from app.tools import browser_tools

    monkeypatch.setattr(
        browser_tools, "SEARCH_PROVIDER_FACTORY",
        lambda q, n: [{"title": "One Piece", "snippet": "The latest is Episode 1122.", "content": ""}],
    )
    page = ScriptedPage([
        _page([_el(1, "link", "Episodes")],
              url="https://anikoto.cz/watch/one-piece-x/ep-1", title="Watch One Piece Episode 1"),
        _page([], url="https://anikoto.cz/watch/one-piece-x/ep-1122",
              title="Watch One Piece Episode 1122"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([])  # must NOT be consulted — the swap is deterministic
    outcome = await run_browse(
        session, "play the latest episode of one piece on anikoto.cz", provider
    )
    assert outcome.success
    assert page.url == "https://anikoto.cz/watch/one-piece-x/ep-1122"
    assert provider.calls == 0


async def test_latest_episode_flow_falls_back_to_on_page_links(monkeypatch):
    """When the web search yields nothing, the highest visible /ep-N sibling link
    is used instead — still no LLM call, and arrival at the target is recognized."""
    from app.tools import browser_tools

    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", lambda q, n: [])
    page = ScriptedPage([
        _page(
            [_el(1, "link", "Episode 2", href="https://anikoto.cz/watch/demo-z/ep-2"),
             _el(2, "link", "Episode 3", href="https://anikoto.cz/watch/demo-z/ep-3")],
            url="https://anikoto.cz/watch/demo-z/ep-1", title="Demo Episode 1",
        ),
        _page([], url="https://anikoto.cz/watch/demo-z/ep-3", title="Demo Episode 3"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([])
    outcome = await run_browse(session, "play the latest episode of demo on anikoto.cz", provider)
    assert outcome.success
    assert page.url == "https://anikoto.cz/watch/demo-z/ep-3"
    assert provider.calls == 0


async def test_verify_gate_rejects_a_premature_done_and_fails_honestly(monkeypatch):
    """The live 2026-07-25 miss: a paginated site (anikoto's 100-episode dropdown)
    let the loop settle on the visible max (ep 100 of 170) and report it as the
    latest. Here the deterministic swap to ep-170 was attempted but the site landed
    on ep-100; the model then insists "done". The verify-before-done gate KNOWS the
    latest is 170 (web) and refuses done on ep-100 — the run fails honestly instead
    of falsely succeeding on the wrong episode."""
    from app.tools import browser_tools

    monkeypatch.setattr(
        browser_tools, "SEARCH_PROVIDER_FACTORY",
        lambda q, n: [{"title": "Demo", "snippet": "The latest is Episode 170.", "content": ""}],
    )
    page = ScriptedPage([
        _page([_el(1, "link", "Episode 2", href="https://anikoto.cz/watch/demo-z/ep-2")],
              url="https://anikoto.cz/watch/demo-z/ep-1", title="Demo Episode 1"),
        # The ep-170 deep link landed on ep-100 (a paginated site's quirk).
        _page([_el(1, "link", "x")],
              url="https://anikoto.cz/watch/demo-z/ep-100", title="Demo Episode 100"),
    ])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"playing"}'] * 4)
    outcome = await run_browse(session, "play the latest episode of demo on anikoto.cz", provider)
    assert not outcome.success
    assert "170" in (outcome.error or "") and "100" in outcome.error


async def test_verify_gate_accepts_done_when_there_is_no_episode_number(monkeypatch):
    """YouTube-shaped: 'latest' with no episode number in the URL. The gate has
    nothing to compare (the open page proves no episode number), so it trusts the
    model's done — it must never block a legitimately-newest video (the no-number
    path relies on the model reading upload dates)."""
    from app.tools import browser_tools

    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", lambda q, n: [])
    page = ScriptedPage([
        _page([_el(1, "link", "x")],
              url="https://www.youtube.com/watch?v=sgA9pV6j_dw", title="Humrahi Episode 34"),
    ])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"playing the newest upload"}'])
    outcome = await run_browse(session, "play the latest episode of humrahi on youtube", provider)
    assert outcome.success


class FlakyProvider:
    """Raises once (a transient 400/dropped connection), then answers — to prove
    the text-decision retry keeps a browse alive instead of stranding it."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient 400 Bad Request")
        return LLMResponse(
            content=self.responses.pop(0) if self.responses else '{"action":"done","reason":"ok"}',
            model="fake", provider="fake",
        )


async def test_decide_retries_once_on_a_transient_llm_failure():
    page = ScriptedPage([_page([_el(1, "link", "x")], url="https://site.test/")])
    session = FakeSession(page)
    provider = FlakyProvider(['{"action":"done","reason":"ok"}'])
    outcome = await run_browse(session, "do something", provider)
    assert provider.calls == 2  # failed once, retried, succeeded
    assert outcome.success


def test_fast_path_fires_only_with_a_single_search_box():
    obs_one = browser_loop.dom_observe.Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[browser_loop.dom_observe.Element(index=1, role="searchbox", name="Search")],
        page_text="", text_truncated=False,
    )
    action = _fast_path_action("play lofi on youtube", obs_one)
    assert action == {"action": "type", "index": 1, "text": "lofi", "submit": True}

    # A media goal still fast-paths — but with the TITLE as the query.
    assert _fast_path_action(
        "play ep 4 of the dangers in my heart season 2 on anikoto.cz", obs_one
    ) == {"action": "type", "index": 1, "text": "the dangers in my heart", "submit": True}

    # Two search-ish inputs is ambiguous — defer to the model.
    obs_two = browser_loop.dom_observe.Observation(
        observation_id="o", url="u", title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=1, role="searchbox", name="Search"),
            browser_loop.dom_observe.Element(index=2, role="searchbox", name="Search site"),
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action("play lofi on youtube", obs_two) is None


def test_fast_path_prefers_the_one_genuine_search_target_among_noise():
    """A homepage with a real search box PLUS a 'Search' link/button (its name
    contains the word, but it is not a fillable search target) used to defer to
    the model — which then fumbled across the ad-heavy page with extra searches.
    Now it fires on the one genuine target (2026-07-22)."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="u", title="", element_total=3,
        elements=[
            browser_loop.dom_observe.Element(index=1, role="link", name="Search"),
            browser_loop.dom_observe.Element(index=2, role="searchbox", name="Find anime"),
            browser_loop.dom_observe.Element(index=3, role="button", name="Search"),
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action("play the dangers in my heart on anikoto.cz", obs) == {
        "action": "type", "index": 2, "text": "the dangers in my heart", "submit": True,
    }


def test_fast_path_still_defers_when_two_real_search_targets():
    """Two genuine search boxes remain ambiguous — the model must choose."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="u", title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=1, role="searchbox", name="Search"),
            browser_loop.dom_observe.Element(index=2, role="combobox", name="Filter"),
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action("play lofi on youtube", obs) is None


async def test_the_fast_path_search_costs_no_llm_call():
    """The fast path does the first search in CODE: home page → (fast-path fill +
    submit) → results page → ONE 'done' response = exactly ONE provider call. The
    reliable typed search is also what keeps the model off a hostile homepage's
    ad links instead of searching (2026-07-22b)."""
    home = _page([_el(1, role="searchbox", name="Search")], url="https://youtube.com/")
    results = _page([_el(1, role="link", name="lofi hip hop", href="/watch?v=a")],
                    url="https://youtube.com/results")
    session = FakeSession(ScriptedPage([home, results]))
    provider = FakeProvider(['{"action":"done","reason":"the video is playing"}'])

    outcome = await run_browse(session, "search 'lofi' on youtube and play it", provider)

    assert outcome.success is True
    assert provider.calls == 1  # the search was free; only the 'done' cost a call
    kinds = [(a[2], a[3]) for a in session.page.acted]
    assert ("fill", "lofi") in kinds
    assert ("press", "Enter") in kinds


# ------------------------------ intent vs catalog search semantics (2026-07-25)
def test_is_intent_search_host_classifies_engines_vs_catalogs():
    yes = browser_loop._is_intent_search_host
    assert yes("https://www.youtube.com/results?search_query=humrahi")
    assert yes("https://m.youtube.com/")
    assert yes("https://youtu.be/abc")
    assert yes("https://www.google.co.uk/search?q=x")
    assert yes("https://www.bing.com/search?q=x")
    # A catalog — and a lookalike whose label merely CONTAINS a brand — are not.
    assert not yes("https://anikoto.cz/filter?keyword=Black+Clover")
    assert not yes("https://my-youtube-clone.com/")
    assert not yes("https://evil-youtube.com/")
    assert not yes("")


def test_search_query_for_adapts_to_the_site():
    q = browser_loop._search_query_for
    # Catalog: the bare title, exactly as before (anikoto needs a literal match).
    assert q("play latest ep of black clover on anikoto", "https://anikoto.cz/", None) == "black clover"
    assert q("play black clover on anikoto", "https://anikoto.cz/", 170) == "black clover"
    # Intent engine + a latest goal → the natural 'latest episode' query, NEVER the
    # number injected (2026-07-25): '<title> episode 35' ranked the Teaser #1 AND
    # echoed 35 into the results title+URL, manufacturing the _current_episode
    # false-positive. The number is ignored here regardless of whether it is known.
    assert q("play latest ep of humrahi on youtube", "https://youtube.com/", 35) == "humrahi latest episode"
    assert q("play latest ep of humrahi on youtube", "https://youtube.com/", None) == "humrahi latest episode"
    # Intent engine, not a latest goal → the plain title.
    assert q("play lofi hip hop on youtube", "https://youtube.com/", None) == "lofi hip hop"
    # No tellable title → None (the fast path then defers to the model).
    assert q("open the travel category then click next", "https://youtube.com/", None) is None


def test_top_result_action_clicks_the_first_youtube_video():
    top = browser_loop._top_result_action
    E = browser_loop.dom_observe.Element
    O = browser_loop.dom_observe.Observation
    results = O(
        observation_id="o", url="https://www.youtube.com/results?search_query=humrahi",
        title="humrahi - YouTube", element_total=3,
        elements=[
            E(index=1, role="link", name="Filters", href="/results?sp=x"),
            E(index=2, role="link", name="Humrahi Episode 34", href="/watch?v=sgA9pV6j_dw"),
            E(index=3, role="link", name="Humrahi Episode 33", href="/watch?v=abc123"),
        ],
        page_text="", text_truncated=False,
    )
    # Both results overlap the title token {humrahi}; the DOM-first one wins the tie.
    assert top(results, "play humrahi on youtube") == {"action": "click", "index": 2}
    # A watch page (its sidebar is full of /watch?v= links) never re-fires.
    watch = O(
        observation_id="o", url="https://www.youtube.com/watch?v=sgA9pV6j_dw",
        title="Humrahi Episode 34", element_total=1,
        elements=[E(index=1, role="link", name="Up next", href="/watch?v=zzz")],
        page_text="", text_truncated=False,
    )
    assert top(watch, "play humrahi on youtube") is None
    # A non-YouTube results page is not this leg's job (catalog nav is separate).
    other = O(
        observation_id="o", url="https://anikoto.cz/filter?keyword=x", title="x",
        element_total=1, elements=[E(index=1, role="link", name="a", href="/watch/x/ep-1")],
        page_text="", text_truncated=False,
    )
    assert top(other, "play x on anikoto") is None
    # A YouTube results page with no video link yet → None (nothing to click).
    empty = O(
        observation_id="o", url="https://www.youtube.com/results?search_query=x", title="x",
        element_total=1, elements=[E(index=1, role="link", name="Filters", href="/results?sp=y")],
        page_text="", text_truncated=False,
    )
    assert top(empty, "play x on youtube") is None


def test_top_result_action_ranks_by_relevance_not_dom_order():
    """The Avengers Doomsday live miss (2026-07-25): the FIRST /watch?v= element in
    DOM order was an unrelated shelf video (a Jujutsu Kaisen result) → the loop
    clicked it. DOM order is not visual rank, so candidates are ranked by title-token
    overlap; the relevant video wins even when it is not first."""
    top = browser_loop._top_result_action
    E = browser_loop.dom_observe.Element
    O = browser_loop.dom_observe.Observation
    results = O(
        observation_id="o",
        url="https://www.youtube.com/results?search_query=avengers+doomsday",
        title="avengers doomsday - YouTube", element_total=4,
        elements=[
            E(index=1, role="link", name="Filters", href="/results?sp=x"),
            # A shelf video that appears FIRST in the DOM but matches nothing.
            E(index=37, role="link",
              name='Jujutsu Kaisen "Jane Juliet VS Yuta"', href="/watch?v=jjk"),
            E(index=52, role="link",
              name="Avengers: Doomsday | Official Trailer | Marvel Studios",
              href="/watch?v=avn"),
        ],
        page_text="", text_truncated=False,
    )
    assert top(results, "play treailer of avengers doomsday on youtube") == {
        "action": "click", "index": 52
    }
    # When NOTHING overlaps the title, defer to the model rather than click a random
    # (irrelevant) link — better than the old first-in-DOM-order pick.
    junk = O(
        observation_id="o",
        url="https://www.youtube.com/results?search_query=avengers+doomsday",
        title="avengers doomsday - YouTube", element_total=2,
        elements=[
            E(index=1, role="link", name="Filters", href="/results?sp=x"),
            E(index=9, role="link", name="Totally Unrelated Video", href="/watch?v=zzz"),
        ],
        page_text="", text_truncated=False,
    )
    assert top(junk, "play avengers doomsday on youtube") is None


async def test_youtube_results_top_video_is_played_deterministically():
    """The 'humrahi' live miss (2026-07-25): the bare title was typed, then the
    model fumbled a 179-element results page and FAILED to pick. Now the top video
    is opened in CODE — the ranker already chose. The search AND the result-pick
    cost ZERO LLM calls; only the final 'done' on the watch page costs one."""
    home = _page([_el(1, role="searchbox", name="Search")], url="https://www.youtube.com/")
    results = _page(
        [_el(1, role="link", name="Filters", href="/results?sp=x"),
         _el(2, role="link", name="Humrahi Episode 34", href="/watch?v=abc")],
        url="https://www.youtube.com/results?search_query=humrahi",
    )
    watch = _page([_el(1, role="button", name="Pause")], url="https://www.youtube.com/watch?v=abc")
    session = FakeSession(ScriptedPage([home, results, watch]))
    provider = FakeProvider(['{"action":"done","reason":"playing"}'])

    outcome = await run_browse(session, "play humrahi on youtube", provider)

    assert outcome.success is True
    assert provider.calls == 1  # search + result-pick were free; only 'done' cost a call
    assert "watch?v=abc" in outcome.url
    # The top result was opened by a GET navigation, never a JS click.
    assert any(k == "goto" and "watch?v=abc" in str(v) for (_i, _idx, k, v) in session.page.acted)


async def test_latest_episode_on_youtube_never_false_dones_on_the_results_page():
    """The 'humrahi' live miss (2026-07-25): a LATEST-episode goal on YouTube landed
    on /results?search_query=humrahi+episode+35 whose title ('humrahi episode 35 -
    YouTube') AND URL both carried '35'. _current_episode false-fired and the catalog
    latest-episode leg declared the RESULTS page 'Episode 35 (the latest) is open,
    done' — nothing played. The intent-host guard now voids that proof, so the loop
    opens the top result instead of stranding on the search page."""
    home = _page([_el(4, role="searchbox", name="Search")],
                 url="https://www.youtube.com/", title="YouTube")
    # Reproduce the trap directly: the number is echoed into BOTH title and URL.
    results = _page(
        [_el(1, role="link", name="Humrahi Episode 35 [Eng Sub]", href="/watch?v=abc")],
        url="https://www.youtube.com/results?search_query=humrahi+episode+35",
        title="humrahi episode 35 - YouTube",
    )
    watch = _page([_el(1, role="button", name="Pause")],
                  url="https://www.youtube.com/watch?v=abc", title="Humrahi Episode 35 [Eng Sub]")
    session = FakeSession(ScriptedPage([home, results, watch]))
    provider = FakeProvider(['{"action":"done","reason":"playing"}'])

    outcome = await run_browse(session, "play latest ep of humrahi on youtube", provider)

    assert outcome.success is True
    # Opened the video, never stranded on /results with a bogus 'done'.
    assert "watch?v=abc" in outcome.url
    assert any(k == "goto" and "watch?v=abc" in str(v) for (_i, _idx, k, v) in session.page.acted)
    # The result-pick was deterministic (code); only the final 'done' cost an LLM call.
    assert provider.calls == 1


def test_is_media_watch_page_recognizes_youtube_video_urls():
    m = browser_loop._is_media_watch_page
    assert m("https://www.youtube.com/watch?v=sgA9pV6j_dw")
    assert m("https://www.youtube.com/watch?v=abc&t=6s&pp=xyz")
    assert m("https://m.youtube.com/watch?feature=share&v=abc123")
    assert m("https://youtu.be/dQw4w9WgXcQ")
    # Not a video page: results / home / a catalog watch page are NOT this.
    assert not m("https://www.youtube.com/results?search_query=humrahi")
    assert not m("https://www.youtube.com/")
    assert not m("https://anikoto.cz/watch/one-piece-odmau/ep-1170")
    assert not m("")


async def test_youtube_play_goal_hands_off_from_the_watch_page_without_llm():
    """The 'humrahi handoff' live miss (2026-07-25): the loop clicked the top result,
    reached youtube.com/watch?v=…, then FAILED because a pre-roll ad ran in the
    automation window and _decide could not find a safe action — the step failed, so
    the clean-window handoff (which runs only on success) never fired, even though
    the video had loaded. A keep_open play goal now treats REACHING the watch page
    as done — the clean ad-blocked window is what actually plays it — so it never
    depends on the ad-heavy automation window and costs ZERO LLM calls."""
    home = _page([_el(4, role="searchbox", name="Search")],
                 url="https://www.youtube.com/", title="YouTube")
    results = _page(
        [_el(1, role="link", name="Humrahi Episode 38 [Eng Sub]", href="/watch?v=abc")],
        url="https://www.youtube.com/results?search_query=Humrahi+latest+episode",
        title="Humrahi latest episode - YouTube",
    )
    # A pre-roll ad overlay is on screen — the old path fumbled here; we finish anyway.
    watch = _page([_el(1, role="button", name="Skip Ad")],
                  url="https://www.youtube.com/watch?v=abc",
                  title="Humrahi Episode 38 [Eng Sub]")
    session = FakeSession(ScriptedPage([home, results, watch]))
    provider = FakeProvider([])  # must NOT be consulted — the whole path is code

    outcome = await run_browse(
        session, "play latest ep of humrahi on youtube", provider, keep_open=True
    )

    assert outcome.success is True
    assert "watch?v=abc" in outcome.url
    assert provider.calls == 0  # search, result-pick, AND the done were all deterministic


# ------------------------------------- fast-path hardening (2026-07-21)
def test_a_typing_goal_never_fast_paths_its_quoted_content():
    """"…open messages and type 'hi' but do not send it" must not fast-path 'hi'
    into a global search box (the LinkedIn incident). A typing/composing goal is
    not a search — refuse, the model decides."""
    goal = (
        "Open the profile of the Anas who is a 1st connection, then open the "
        "messages area and type 'hi' but do not send it"
    )
    assert _extract_search_term(goal) is None
    assert _extract_search_term("compose a 'hello there' message") is None
    assert _extract_search_term("write 'thanks' in the comment box") is None
    assert _extract_search_term("draft 'hi' but don't send") is None


def test_an_unquoted_multi_clause_goal_is_not_a_search_term():
    """The extraction must yield a TERM (short, single-clause), never the goal's
    remaining instructions."""
    assert _extract_search_term(
        "Open the books.toscrape.com homepage, then click the Travel category"
    ) is None
    assert _extract_search_term(
        "open the profile of anas who is in my first connections and say hello"
    ) is None
    assert _extract_search_term("play lofi hip hop on youtube") == "lofi hip hop"


# ------------------------------------------ the back guard (2026-07-21)
class _BackPage:
    """A page whose go_back lands where the script says — about:blank models a
    fresh session with no history."""

    def __init__(self, lands_on):
        self.url = "https://site.test/somewhere"
        self.lands_on = lands_on
        self.went_forward = 0

    async def go_back(self, **kwargs):
        self.url = self.lands_on

    async def go_forward(self, **kwargs):
        self.went_forward += 1
        self.url = "https://site.test/somewhere"


async def test_back_onto_about_blank_is_an_honest_failure():
    """A fresh session's `back` lands on about:blank — live 2026-07-21 the loop
    thrashed navigate→back→blank until the stuck detector failed the step. The
    guard undoes the blank landing and tells the model there is no history."""
    page = _BackPage(lands_on="about:blank")
    session = FakeSession(page)
    ok, note = await browser_loop._act(session, None, {"action": "back"})
    assert ok is False
    assert "no earlier page" in note
    assert page.went_forward == 1        # the blank landing was undone


async def test_back_with_real_history_still_works():
    page = _BackPage(lands_on="https://site.test/previous")
    session = FakeSession(page)
    ok, note = await browser_loop._act(session, None, {"action": "back"})
    assert ok is True and note == ""
    assert page.went_forward == 0


# ---------------------------------------------------------------- termination
async def test_done_ends_the_loop_successfully():
    session = FakeSession(ScriptedPage([_page([_el(1, name="anything")])]))
    provider = FakeProvider(['{"action":"done","reason":"open"}'])
    outcome = await run_browse(session, "just look", provider)
    assert outcome.success is True
    assert outcome.done_reason == "open"


async def test_a_link_is_opened_by_navigating_to_its_href_not_a_js_click():
    """The YouTube SPA lesson, measured live 2026-07-17: a JS click POSTs and
    READ mode aborts it, so a link is opened by NAVIGATING to its href (a GET)."""
    results = _page(
        [_el(1, role="link", name="jane - The Long Faces", href="/watch?v=xyz")],
        url="https://youtube.com/results",
    )
    watch = _page([_el(1, role="button", name="Pause")], url="https://youtube.com/watch?v=xyz")
    session = FakeSession(ScriptedPage([results, watch]))
    # The top result is now picked in CODE (the intent-engine leg), so the model
    # only confirms the video is playing on the watch page.
    provider = FakeProvider(['{"action":"done","reason":"playing"}'])

    outcome = await run_browse(session, "play jane by the long faces on youtube", provider)

    assert outcome.success is True
    assert "watch?v=xyz" in outcome.url
    # It navigated (a GET) rather than issuing a raw JS click.
    assert any(k == "goto" and "watch?v=xyz" in str(v) for (_i, _idx, k, v) in session.page.acted)
    assert not any(k == "click" for (_i, _idx, k, v) in session.page.acted)


async def test_a_button_without_an_href_is_clicked_not_navigated():
    """The other half: a real button (consent 'Accept', a play control) has no
    href, so it still gets a genuine click."""
    page = _page([_el(1, role="button", name="Accept all")])
    session = FakeSession(ScriptedPage([page]))
    provider = FakeProvider(['{"action":"click","index":1}', '{"action":"done","reason":"ok"}'])

    outcome = await run_browse(session, "accept the dialog", provider)

    assert outcome.success is True
    assert any(k == "click" for (_i, _idx, k, v) in session.page.acted)
    assert not any(k == "goto" for (_i, _idx, k, v) in session.page.acted)


async def test_dedupe_halts_a_repeated_action():
    """The ENDED-STREAM bug: a page whose button never changes anything. The model
    keeps clicking it; dedupe stops after _MAX_REPEAT rather than burning the
    whole budget on one dead button."""
    stuck = _page([_el(1, role="button", name="Play")])
    session = FakeSession(ScriptedPage([stuck]))  # one page — a click changes nothing
    provider = FakeProvider(['{"action":"click","index":1}'] * 20)

    outcome = await run_browse(session, "play the stream", provider)

    assert outcome.success is False
    assert "didn't respond" in outcome.error
    # Bounded: it did not consult the model 20 times.
    #
    # The bound LOOSENED on 2026-07-26 (was _MAX_REPEAT + 1). A tripped repeat no
    # longer ends the run — it REFUSES the move, tells the model so in the
    # history it reads, and lets it choose again; only three refusals in a row
    # end it. That buys recoverability (the model can route around a dead button
    # instead of the whole task dying on it) for a few extra decisions on a page
    # that was doomed anyway. Still a small constant, still nowhere near 20.
    assert provider.calls <= browser_loop._MAX_REPEAT + 4


async def test_progress_detection_stops_a_wheel_spinning_loop():
    """15.1: the per-element dedupe catches ONE re-hit button; progress detection
    catches WANDERING — cycling among several elements that change nothing. Six
    buttons on a page that never changes, clicked round-robin: no single element
    trips _MAX_REPEAT, but nothing NEW is interacted with, so the loop stops on
    _STUCK_LIMIT before burning the whole action budget."""
    buttons = [_el(i, role="button", name=chr(64 + i)) for i in range(1, 7)]  # A..F
    session = FakeSession(ScriptedPage([_page(buttons)]))  # one page — clicks change nothing
    provider = FakeProvider([f'{{"action":"click","index":{i}}}' for i in range(1, 7)] * 3)

    outcome = await run_browse(session, "spin", provider, max_actions=15)

    assert outcome.success is False
    assert "progress" in outcome.error
    assert provider.calls < 15  # stopped short of the hard action cap


async def test_the_session_carries_browse_history_across_a_resume():
    """Multi-commit (15.1) RESUMES the same session; run_browse seeds its history
    from the session and stores it back, so a resumed sub-goal keeps the model's
    context of what it already did instead of starting blind."""
    session = FakeSession(ScriptedPage([
        _page([_el(1, role="button", name="Next")]),
        _page([_el(1, name="second page")]),
    ]))
    p1 = FakeProvider(['{"action":"click","index":1}', '{"action":"done","reason":"ok"}'])
    await run_browse(session, "reach the form", p1)
    carried = list(session.browse_history)
    assert carried  # the click was recorded on the session

    # A resume seeds from what was carried (does not reset it).
    p2 = FakeProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(session, "continue", p2)
    assert session.browse_history[:len(carried)] == carried


async def test_the_action_cap_stops_the_loop():
    """No done, no repeat (each page's element is uniquely named) — the hard cap
    is the backstop that guarantees the loop always ends."""
    pages = [_page([_el(1, name=f"item {i}", href=f"/p{i}")], url=f"https://site.test/p{i}")
             for i in range(10)]
    session = FakeSession(ScriptedPage(pages))
    provider = FakeProvider(['{"action":"click","index":1}'] * 20)

    outcome = await run_browse(session, "wander", provider, max_actions=3)

    assert outcome.success is False
    assert "limit" in outcome.error
    assert provider.calls == 3


async def test_the_wall_clock_deadline_stops_a_slow_run(monkeypatch):
    """TIME, not just STEPS. The action cap bounds how MANY steps run, not how
    LONG they take — a page or provider that is slow-but-not-hung on every step
    still adds up to a multi-minute freeze (live report 2026-07-17). The
    wall-clock deadline is the backstop; here a clock that jumps past the limit
    makes the loop stop at step 0 before it ever consults the model."""
    clock = {"t": 0.0}

    def fake_monotonic():
        v = clock["t"]
        clock["t"] = browser_loop.BROWSE_DEADLINE_SECONDS + 100  # every later call is past the deadline
        return v

    monkeypatch.setattr(browser_loop.time, "monotonic", fake_monotonic)
    session = FakeSession(ScriptedPage([_page([_el(1, name="anything")])]))
    provider = FakeProvider(['{"action":"click","index":1}'] * 5)

    outcome = await run_browse(session, "wander forever", provider)

    assert outcome.success is False
    assert "time limit" in outcome.error
    assert provider.calls == 0  # the deadline fired before any decision


async def test_a_stalled_decision_call_is_bounded_and_stops(monkeypatch):
    """One _decide call must not freeze the whole browse for the shared LLM
    client's 300s read timeout. A provider that stalls past
    BROWSE_DECISION_TIMEOUT_SECONDS is cut off, reads as 'no usable action', and
    the loop stops honestly instead of hanging."""
    monkeypatch.setattr(browser_loop, "BROWSE_DECISION_TIMEOUT_SECONDS", 0.05)

    class StallProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, temperature=0.7, max_tokens=None):
            self.calls += 1
            await asyncio.sleep(1)  # far longer than the 0.05s cap above
            return LLMResponse(content='{"action":"done"}', model="f", provider="f")

    session = FakeSession(ScriptedPage([_page([_el(1, name="only element")])]))
    provider = StallProvider()

    outcome = await run_browse(session, "do something", provider)

    assert outcome.success is False
    assert "safe next action" in outcome.error
    assert provider.calls == 1  # it asked once, the stall was bounded, it stopped


async def test_a_hallucinated_index_stops_the_loop():
    """The index contract at the loop level: a chosen index not on the page is
    refused (never resolved against whatever is third), so the loop stops rather
    than clicking the wrong thing."""
    session = FakeSession(ScriptedPage([_page([_el(1, name="only element")])]))
    provider = FakeProvider(['{"action":"click","index":99}'])
    outcome = await run_browse(session, "click something", provider)
    assert outcome.success is False
    assert provider.calls == 1


async def test_blocked_mutations_pass_through_to_the_outcome():
    """An aborted POST is a breakage the user should see, not silent — the outcome
    carries the interceptor's stats."""
    session = FakeSession(ScriptedPage([_page([_el(1)])]), stats=FakeStats(blocked_mutations=3))
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    outcome = await run_browse(session, "look", provider)
    assert outcome.blocked["blocked_mutations"] == 3


async def test_an_unparseable_decision_stops_honestly():
    session = FakeSession(ScriptedPage([_page([_el(1)])]))
    provider = FakeProvider(["not json at all", "still not json"])
    outcome = await run_browse(session, "do something", provider)
    assert outcome.success is False
    # ONE retry, then stop. An empty/unparseable reply is the one _decide failure
    # class that is plausibly transient (a reasoning model that spent its whole
    # token budget thinking returns an empty string), so it is worth exactly one
    # more ask — and no more, or every real refusal costs double.
    assert provider.calls == 2


async def test_an_unparseable_decision_is_retried_once_and_recovers():
    """The retry is not decoration: live, an empty reply killed a browse on a
    page the model could see perfectly well. A second ask that parses continues
    the run."""
    session = FakeSession(ScriptedPage([_page([_el(1)]), _page([_el(1)])]))
    provider = FakeProvider(["", '{"action":"done","reason":"found it"}'])
    outcome = await run_browse(session, "do something", provider)
    assert outcome.success is True
    assert provider.calls == 2


# ---------------------------------------------------------------- parse
def test_parse_action_accepts_the_three_verbs():
    assert _parse_action('{"action":"done","reason":"x"}') == {"action": "done", "reason": "x"}
    assert _parse_action('{"action":"click","index":2}') == {"action": "click", "index": 2}
    typed = _parse_action('{"action":"type","index":1,"text":"hi","submit":true}')
    assert typed == {"action": "type", "index": 1, "text": "hi", "submit": True}


def test_parse_action_accepts_navigate():
    assert _parse_action('{"action":"navigate","url":"https://youtube.com/results?q=x"}') == {
        "action": "navigate", "url": "https://youtube.com/results?q=x"
    }
    assert _parse_action('{"action":"navigate"}') is None  # no url


async def test_navigate_drives_a_get_url_within_the_allowlist():
    """The SPA lesson, measured live on YouTube: a search box that submits via a
    blocked POST is recovered by navigating (a GET) to the results URL. This is
    the read-only, in-code path for driving SPA sites."""
    home = _page([_el(1, role="searchbox", name="Search")], url="https://youtube.com/")
    results = _page(
        [_el(1, role="link", name="Jane! - The Long Faces", href="/watch?v=z")],
        url="https://youtube.com/results?search_query=jane",
    )
    watch = _page([_el(1, role="button", name="Pause")], url="https://youtube.com/watch?v=z")
    session = FakeSession(ScriptedPage([home, results, watch]))
    # Step 0 fast-paths the search (thin SPA results); the model then navigates to
    # the results URL, opens the video, and finishes.
    provider = FakeProvider([
        '{"action":"navigate","url":"https://youtube.com/results?search_query=jane"}',
        '{"action":"click","index":1}',
        '{"action":"done","reason":"playing"}',
    ])

    outcome = await run_browse(session, "search 'jane' on youtube and play it", provider)

    assert outcome.success is True
    assert "watch?v=z" in outcome.url
    assert any(k == "goto" and "results" in str(v) for (_i, _idx, k, v) in session.page.acted)


def test_parse_action_rejects_garbage_and_unknown_verbs():
    assert _parse_action("") is None
    assert _parse_action("nonsense") is None
    assert _parse_action('{"action":"drag","index":1}') is None   # drag with no target
    assert _parse_action('{"action":"click"}') is None  # no index
    # press_key is whitelisted keys ONLY — Enter is a submit gesture, refused.
    assert _parse_action('{"action":"press_key","key":"Enter"}') is None
    assert _parse_action('{"action":"press_key","key":"Escape"}') == {
        "action": "press_key", "key": "Escape"
    }
    # select_option needs a value; scroll normalizes its direction.
    assert _parse_action('{"action":"select_option","index":2}') is None
    assert _parse_action('{"action":"scroll","direction":"sideways"}') == {
        "action": "scroll", "direction": "down"
    }
    # tolerates a code fence and surrounding prose
    assert _parse_action('```json\n{"action":"done","reason":"y"}\n```') == {"action": "done", "reason": "y"}


# ---------------------------------------------------------------- login walls
def _obs(elements, url="https://site.test/"):
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title="", element_total=len(elements),
        elements=elements, page_text="", text_truncated=False,
    )


def _El(**kw):
    kw.setdefault("index", 1)
    kw.setdefault("role", "link")
    kw.setdefault("name", "x")
    return browser_loop.dom_observe.Element(**kw)


def test_detect_login_wall_on_a_password_field():
    """The universal, site-agnostic tell: a visible password field → login."""
    wall = browser_loop.detect_login_wall(
        _obs([_El(role="password", name="Password")], url="https://some-site.test/in")
    )
    assert wall == ("login", "some-site.test")


def test_detect_login_wall_on_a_dedicated_auth_host():
    """The belt for an email-first step showing no password field yet → login."""
    wall = browser_loop.detect_login_wall(
        _obs([_El(role="input", name="Email")], url="https://accounts.google.com/signin")
    )
    assert wall == ("login", "accounts.google.com")


def test_a_normal_page_is_not_a_login_wall():
    """Conservative: an ordinary content page must never trip the guard, or a
    false wall would abort a working task."""
    assert browser_loop.detect_login_wall(
        _obs([_El(role="link", name="Home"), _El(index=2, role="searchbox", name="Search")],
             url="https://youtube.com/results")
    ) is None


def test_detect_signup_wall_on_a_signup_route():
    """A dedicated signup route hands off as 'signup' even with no password
    field yet (the email-first / multi-step account-creation case)."""
    wall = browser_loop.detect_login_wall(
        _obs([_El(role="input", name="Email")], url="https://shop.test/register")
    )
    assert wall == ("signup", "shop.test")


def test_detect_signup_wall_on_account_creation_button_plus_email():
    """Label signal: an account-creation submit button AND an email field, on a
    page whose URL is not itself a signup route."""
    wall = browser_loop.detect_login_wall(
        _obs(
            [
                _El(index=1, role="input", name="Email address"),
                _El(index=2, role="input", name="Full name"),
                _El(index=3, role="button", name="Create account"),
            ],
            url="https://shop.test/onboarding",
        )
    )
    assert wall == ("signup", "shop.test")


def test_signup_with_a_password_is_messaged_as_signup():
    """A signup form that DOES have a password is still a wall, and the more
    accurate 'signup' kind wins over a plain 'login'."""
    wall = browser_loop.detect_login_wall(
        _obs(
            [
                _El(index=1, role="input", name="Email"),
                _El(index=2, role="password", name="Password"),
            ],
            url="https://shop.test/signup",
        )
    )
    assert wall == ("signup", "shop.test")


def test_a_job_application_form_is_not_a_signup_wall():
    """The load-bearing non-goal: the 15.2 autofill flow (job applications /
    contact forms) must never trip the signup wall — 'Apply'/'Send' + an email
    field is NOT account creation."""
    assert browser_loop.detect_login_wall(
        _obs(
            [
                _El(index=1, role="input", name="Email"),
                _El(index=2, role="input", name="Full name"),
                _El(index=3, role="button", name="Submit application"),
            ],
            url="https://jobs.test/careers/apply",
        )
    ) is None
    # A contact form is likewise not a wall.
    assert browser_loop.detect_login_wall(
        _obs(
            [
                _El(index=1, role="input", name="Your email"),
                _El(index=2, role="button", name="Send message"),
            ],
            url="https://acme.test/contact",
        )
    ) is None
    # A newsletter "Sign up" box (bare label, no account-creation phrasing) must
    # not misfire either.
    assert browser_loop.detect_login_wall(
        _obs(
            [
                _El(index=1, role="input", name="Email"),
                _El(index=2, role="button", name="Sign up"),
            ],
            url="https://blog.test/posts/hello",
        )
    ) is None


async def test_a_login_wall_halts_the_loop_before_any_action():
    """Wall detected → loop stops cleanly, flags login_required, consults the
    model ZERO times, and — the load-bearing property — types NOTHING (no
    credential is ever handled)."""
    wall = _page(
        [_el(1, role="password", name="Password"), _el(2, role="input", name="Email")],
        url="https://accounts.google.com/signin",
    )
    session = FakeSession(ScriptedPage([wall]))
    # A malicious/naive decision would type a secret; it must never be reached.
    provider = FakeProvider(['{"action":"type","index":1,"text":"hunter2","submit":true}'])

    outcome = await run_browse(session, "play jane on youtube", provider)

    assert outcome.login_required is True
    assert outcome.success is False
    assert outcome.login_site == "accounts.google.com"
    assert outcome.wall_kind == "login"
    assert "accounts.google.com" in outcome.login_url
    assert provider.calls == 0        # stopped before any decision
    assert session.page.acted == []   # nothing typed — no credential handled


async def test_a_signup_wall_halts_the_loop_and_flags_signup():
    """An account-creation form (no password, signup route) stops the loop the
    same way a login wall does, flags wall_kind='signup' for the handoff text,
    and never asks the model."""
    wall = _page(
        [_el(1, role="input", name="Email"), _el(2, role="button", name="Create account")],
        url="https://shop.test/register",
    )
    session = FakeSession(ScriptedPage([wall]))
    provider = FakeProvider(['{"action":"type","index":1,"text":"me@x.test","submit":true}'])

    outcome = await run_browse(session, "sign me up on shop.test", provider)

    assert outcome.login_required is True
    assert outcome.wall_kind == "signup"
    assert outcome.login_site == "shop.test"
    assert provider.calls == 0
    assert session.page.acted == []


async def test_a_non_wall_run_leaves_login_required_false():
    session = FakeSession(ScriptedPage([_page([_el(1, role="link", name="Home")])]))
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    outcome = await run_browse(session, "look", provider)
    assert outcome.login_required is False
    assert outcome.success is True


async def test_auth_navigation_target_detects_a_login_host_and_ignores_others():
    """The helper that catches a sign-in the loop is ABOUT to navigate to (a
    click on a 'Sign in' link, or a navigate to an auth host) before it spins on
    the allowlist-blocked navigation. Only navigate/click can target a host."""
    session = FakeSession(ScriptedPage([_page([])]))
    empty = _obs([])
    # A navigate straight to an auth host is caught, URL carried through.
    assert await browser_loop._auth_navigation_target(
        session, empty, {"action": "navigate", "url": "https://accounts.google.com/signin"}
    ) == ("accounts.google.com", "https://accounts.google.com/signin")
    # A subdomain of an auth host counts; a normal site does not.
    assert await browser_loop._auth_navigation_target(
        session, empty, {"action": "navigate", "url": "https://youtube.com/results?q=x"}
    ) is None
    # type/done never target a host.
    assert await browser_loop._auth_navigation_target(
        session, empty, {"action": "type", "index": 1, "text": "x"}
    ) is None
    # A click whose target link points at an auth host is caught (href read live).
    link = _page(
        [_el(1, role="link", name="Sign in", href="https://accounts.google.com/ServiceLogin?x=1")],
        url="https://www.youtube.com/",
    )
    csession = FakeSession(ScriptedPage([link]))
    cobs = await browser_loop.dom_observe.observe(csession.page)
    auth = await browser_loop._auth_navigation_target(csession, cobs, {"action": "click", "index": 1})
    assert auth is not None and auth[0] == "accounts.google.com"


async def test_a_sign_in_link_hands_off_instead_of_spinning():
    """The 'sign in to youtube and play jane' incident. A goal that says to sign
    in sends the model clicking a 'Sign in' link to accounts.google.com — an
    off-allowlist host the interceptor blocks, so the loop used to spin on the
    blocked navigation until the stuck-limit failed the whole task (song and
    all). Now the auth-host TARGET is caught BEFORE acting and handed off exactly
    like a landed login wall: login_required, wall_kind 'login', and the blocked
    navigation is never even attempted."""
    home = _page(
        [_el(1, role="link", name="Sign in",
             href="https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com")],
        url="https://www.youtube.com/",
    )
    session = FakeSession(ScriptedPage([home]))
    provider = FakeProvider(['{"action":"click","index":1}'])

    outcome = await run_browse(
        session, "sign in to youtube and play jane by the long faces", provider
    )

    assert outcome.login_required is True
    assert outcome.success is False
    assert outcome.login_site == "accounts.google.com"
    assert outcome.wall_kind == "login"
    assert "accounts.google.com" in outcome.login_url
    # No spin: the loop never issued the allowlist-blocked navigation.
    assert not any(k == "goto" for (_i, _idx, k, v) in session.page.acted)


# ------------------------------------------ off-site navigation hand-off (2026-07-18)
# The user-chosen loosening of grounding: a page may PROPOSE an off-grounded
# destination (a job board's 'Apply' to an external ATS), but the loop NEVER
# follows it on its own — it stops and the planner asks. An internal/SSRF host is
# never even offered. These pin the loop-level detection + the run halt.
class AllowlistSession(FakeSession):
    """A FakeSession carrying an allowlist, so the off-site check has a defined
    'off' to measure against (production always has ≥1 origin; a bare FakeSession
    has none, which is exactly why the check no-ops on an empty allowlist)."""

    def __init__(self, page, allowlist, stats=None):
        super().__init__(page, stats)
        self.allowlist = set(allowlist)


async def test_offsite_navigation_target_flags_an_off_allowlist_host(monkeypatch):
    monkeypatch.setattr(
        "app.tools.browser_tools._host_is_blocked", lambda h: False
    )
    session = FakeSession(ScriptedPage([_page([])]))
    empty = _obs([], url="https://weworkremotely.com/")
    allowed = {"weworkremotely.com"}
    # A navigate to a page-derived external ATS is caught.
    got = await browser_loop._offsite_navigation_target(
        session, empty, {"action": "navigate", "url": "https://greenhouse.io/apply/1"}, allowed
    )
    assert got == ("greenhouse.io", "https://greenhouse.io/apply/1")
    # A navigate that stays on the named site (incl. subdomain) is NOT off-site.
    assert await browser_loop._offsite_navigation_target(
        session, empty, {"action": "navigate", "url": "https://jobs.weworkremotely.com/x"}, allowed
    ) is None
    # type/done never target a host.
    assert await browser_loop._offsite_navigation_target(
        session, empty, {"action": "type", "index": 1, "text": "x"}, allowed
    ) is None
    # An empty allowlist no-ops (nothing coherent to call 'off-site').
    assert await browser_loop._offsite_navigation_target(
        session, empty, {"action": "navigate", "url": "https://greenhouse.io/x"}, set()
    ) is None


async def test_an_internal_host_is_never_offered_for_approval(monkeypatch):
    """The SSRF bound is not a thing the user can approve away: a blocked/internal
    host returns None (left for the interceptor to refuse), never an approval
    prompt."""
    monkeypatch.setattr(
        "app.tools.browser_tools._host_is_blocked", lambda h: True  # everything blocked
    )
    session = FakeSession(ScriptedPage([_page([])]))
    empty = _obs([], url="https://weworkremotely.com/")
    assert await browser_loop._offsite_navigation_target(
        session, empty, {"action": "navigate", "url": "http://169.254.169.254/latest/meta-data"},
        {"weworkremotely.com"},
    ) is None


async def test_an_off_site_navigation_halts_the_loop_for_approval(monkeypatch):
    """End to end: the model tries to leave the named site for a page-derived
    origin → the loop STOPS with origin_approval_required naming that origin, and
    never issues the navigation (no goto acted). Jarvis asks before it leaves."""
    monkeypatch.setattr("app.tools.browser_tools._host_is_blocked", lambda h: False)
    home = _page([_el(1, role="link", name="Apply", href="https://greenhouse.io/apply/1")],
                 url="https://weworkremotely.com/jobs/1")
    session = AllowlistSession(ScriptedPage([home]), {"weworkremotely.com"})
    provider = FakeProvider(['{"action":"navigate","url":"https://greenhouse.io/apply/1"}'])

    outcome = await run_browse(session, "apply to the job on weworkremotely", provider)

    assert outcome.origin_approval_required is True
    assert outcome.origin_candidate == "greenhouse.io"
    assert "greenhouse.io" in outcome.origin_url
    assert outcome.success is False
    # It never left the site — the off-site navigation was never issued.
    assert not any(k == "goto" for (_i, _idx, k, v) in session.page.acted)


async def test_navigation_within_the_named_site_is_not_an_off_site_handoff(monkeypatch):
    """A move that stays on the allowlisted site proceeds normally — the hand-off
    only fires when the loop would actually LEAVE the named sites."""
    monkeypatch.setattr("app.tools.browser_tools._host_is_blocked", lambda h: False)
    home = _page([_el(1, role="link", name="Job 1", href="/jobs/1")],
                 url="https://weworkremotely.com/")
    detail = _page([_el(1, role="button", name="x")], url="https://weworkremotely.com/jobs/1")
    session = AllowlistSession(ScriptedPage([home, detail]), {"weworkremotely.com"})
    provider = FakeProvider(['{"action":"click","index":1}', '{"action":"done","reason":"ok"}'])

    outcome = await run_browse(session, "open a job on weworkremotely", provider)

    assert outcome.origin_approval_required is False
    assert outcome.success is True


# ------------------------------------- redirect off-site hand-off (2026-07-19)
# The WWR-ad incident: an ad's SAME-SITE click-tracker href passes the pre-act
# off-site check (its host is allowed — the href tells you nothing), then the
# server 302s off the allowlist. The session backs the page out and records the
# landing; the loop must surface the SAME origin-approval pause an off-site
# href gets — not leave the model to re-click the tempting link until the
# stuck-limit fails the whole task (the incident run died exactly there, twice).
class RedirectingSession(AllowlistSession):
    """goto raises like BrowserSession._verify_landing after a refused redirect
    landing: the page stays where it was and the landing is recorded."""

    def __init__(self, page, allowlist, host, url):
        super().__init__(page, allowlist)
        self.last_redirect_offsite = None
        self._redirect = (host, url)

    async def goto(self, url):
        host, target = self._redirect
        self.last_redirect_offsite = {"host": host, "url": target}
        raise RuntimeError(
            f"The page redirected to '{host}', which this task is not allowed to visit."
        )


async def test_a_refused_redirect_landing_pauses_for_origin_approval(monkeypatch):
    monkeypatch.setattr("app.tools.browser_tools._host_is_blocked", lambda h: False)
    listing = _page(
        [_el(20, role="link", name="Open Roles at Partner Companies",
             href="https://weworkremotely.com/listing_ads/13/click")],
        url="https://weworkremotely.com/remote-jobs",
    )
    session = RedirectingSession(
        ScriptedPage([listing]), {"weworkremotely.com"},
        "metana.io", "https://metana.io/opportunities/?utm_medium=homepage-ad",
    )
    provider = FakeProvider(['{"action":"click","index":20}'])

    outcome = await run_browse(session, "find jobs on weworkremotely", provider)

    assert outcome.origin_approval_required is True
    assert outcome.origin_candidate == "metana.io"
    assert "metana.io/opportunities" in outcome.origin_url
    assert outcome.success is False
    # Consumed: a later browse never re-fires on this stale marker.
    assert session.last_redirect_offsite is None


async def test_a_stale_redirect_marker_never_fires(monkeypatch):
    """The marker is cleared before every action — a marker left by an earlier
    action (or a buggy fake) cannot convert an ordinary successful click into
    an approval pause."""
    monkeypatch.setattr("app.tools.browser_tools._host_is_blocked", lambda h: False)
    home = _page([_el(1, role="button", name="Menu")], url="https://weworkremotely.com/")
    after = _page([_el(1, role="button", name="Menu")], url="https://weworkremotely.com/")
    session = AllowlistSession(ScriptedPage([home, after]), {"weworkremotely.com"})
    session.last_redirect_offsite = {"host": "stale.example", "url": "https://stale.example/"}
    provider = FakeProvider(['{"action":"click","index":1}', '{"action":"done","reason":"ok"}'])

    outcome = await run_browse(session, "open weworkremotely", provider)

    assert outcome.origin_approval_required is False
    assert outcome.success is True


# ------------------------------------------------- element paging ("more")
# 2026-07-19, the WWR window trap: 240 elements, ~47 fit the char budget, and
# the section the goal needed sat at index 173 — the model's whole world was
# the wrong window, so it clicked the same visible nav toggles until the
# stuck-limit killed the run. "more" slides the window; these pin that it (a)
# reveals the deeper elements to the model, (b) touches nothing on the page,
# and (c) an exhausted "more" counts as a failure instead of spinning.

class PromptRecordingProvider(FakeProvider):
    """Kept only as a name — FakeProvider records prompts itself now.

    It gained that when a defect turned out to be "the model was never shown the
    data" (2026-07-26), which no call-count assertion can see. Overriding `chat`
    to record as well appended every prompt TWICE, so `prompts[1]` became a copy
    of `prompts[0]` and the paging assertion below silently compared the wrong
    window."""


def _long_page(target_index=80, n=80):
    """A page whose element list overflows the render budget — the target link
    sits past the first window."""
    els = [
        _el(i, name=f"filler element with a long descriptive name for padding the budget number {i:03d}",
            href=f"/filler/{i}")
        for i in range(1, n)
    ]
    els.append(_el(target_index, name="the target job listing", href="/jobs/target"))
    return _page(els, url="https://site.test/", title="Big listing")


async def test_more_slides_the_element_window_without_touching_the_page():
    page = ScriptedPage([
        _long_page(),
        _page([_el(1, name="Target job")], url="https://site.test/jobs/target", title="Target"),
    ])
    provider = PromptRecordingProvider([
        '{"action": "more"}',
        '{"action": "click", "index": 80}',
        '{"action": "done", "reason": "opened"}',
    ])

    outcome = await run_browse(FakeSession(page), "open the target job listing", provider)

    assert outcome.success is True
    # The first window clipped the target's element line out; the second
    # (after "more") shows it. (The goal text names the target too, so the
    # assertion is on the numbered ELEMENT line, not the phrase.)
    assert '[80] link "the target job listing"' not in provider.prompts[0]
    assert '[80] link "the target job listing"' in provider.prompts[1]
    # The "more" action is offered while elements are unshown.
    assert '"action": "more"' in provider.prompts[0]
    # Paging touched nothing: the only page interaction is the click's GET.
    gotos = [a for a in page.acted if a[2] == "goto"]
    assert len(gotos) == 1 and gotos[0][3].endswith("/jobs/target")


async def test_more_with_every_element_shown_counts_as_a_failure():
    small = _page([_el(1, name="only link", href="/x")])
    page = ScriptedPage([small])
    provider = FakeProvider(['{"action": "more"}'] * 4)

    outcome = await run_browse(FakeSession(page), "open the only link", provider)

    assert outcome.success is False
    assert page.acted == []  # nothing was ever touched
    # Names what was actually exhausted — the element list — not "several actions
    # failed", which is untrue here: no action was ever attempted.
    assert "already shown" in outcome.error


def test_parse_action_accepts_more():
    assert _parse_action('{"action": "more"}') == {"action": "more"}


async def test_a_fragment_href_link_is_clicked_not_navigated():
    """A link whose href is '#' is a menu toggle: navigating to it is a
    guaranteed no-op (the WWR 'Find Jobs' incident) — it gets a REAL click, so
    whatever the toggle controls actually opens."""
    page = ScriptedPage([
        _page([_el(1, name="Find Jobs", href="#"), _el(2, name="Jobs", href="/jobs")]),
        _page([_el(1, name="menu open")], url="https://site.test/", title="Menu"),
    ])
    provider = FakeProvider([
        '{"action": "click", "index": 1}',
        '{"action": "done", "reason": "menu open"}',
    ])

    outcome = await run_browse(FakeSession(page), "open the jobs menu", provider)

    assert outcome.success is True
    kinds = [a[2] for a in page.acted]
    assert "click" in kinds and "goto" not in kinds


# -------------------------------------------------- CAPTCHA / challenge (15.4)
def _cobs(url="https://site.test/", title="", challenge=None, elements=None):
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title=title,
        elements=elements or [], element_total=len(elements or []),
        page_text="", text_truncated=False, challenge=challenge,
    )


def test_detect_challenge_from_a_blocking_probe():
    """The strong signal: dom_observe flagged a visible challenge widget/iframe."""
    assert browser_loop.detect_challenge(
        _cobs(url="https://site.test/x", challenge={"kind": "hCaptcha", "blocking": True})
    ) == ("hCaptcha", "site.test")
    assert browser_loop.detect_challenge(
        _cobs(url="https://site.test/x", challenge={"kind": "reCAPTCHA", "blocking": True})
    ) == ("reCAPTCHA", "site.test")


def test_an_invisible_recaptcha_badge_is_not_a_challenge():
    """The dominant false positive: a v3 badge on an ordinary form blocks nothing.
    It is excluded in the probe (blocking never set), so an absent/non-blocking
    probe with a normal title is NOT a challenge — a working task is not aborted."""
    assert browser_loop.detect_challenge(_cobs(url="https://site.test/", challenge=None)) is None
    assert browser_loop.detect_challenge(
        _cobs(url="https://site.test/", challenge={"kind": "reCAPTCHA", "blocking": False})
    ) is None


def test_challenge_probe_gates_widgets_on_rendered_visibility():
    """Regression lineage (live false positives 2026-07-18 "i see no recaptcha" and
    2026-07-19 the invisible CF bot-management iframe): a widget merely PRESENT in
    the DOM (a 0×0 anchor in a display:none modal, Cloudflare's background beacon
    iframe) must never count — only one RENDERED on-screen at a real size. The
    probe v2 (2026-07-19, after the live auto-click) centralizes that gate in
    renderedRect and applies it to every vendor signal. The JS runs only in a real
    browser, so pin the structural contract here."""
    from app.core import dom_observe

    js = dom_observe._EXTRACT_JS
    # the visibility gate, applied to iframes AND containers AND field climbs.
    assert "renderedRect" in js
    assert "getBoundingClientRect" in js
    # the v3/invisible exclusions must not regress.
    assert "size=invisible" in js
    assert ".grecaptcha-badge" in js
    # vendor iframes are matched WIDE (any /recaptcha/ path — api2 AND enterprise —
    # plus recaptcha.net), never the old api2-only string.
    assert "google.com/recaptcha/" in js
    assert "recaptcha.net/recaptcha/" in js
    assert "google.com/recaptcha/api2/" not in js
    # the old src-only trip ("recaptcha in src ⇒ blocking") must not return.
    assert "s.includes('google.com/recaptcha') && !s.includes('size=invisible')" not in js
    # the real-interstitial signals are still present.
    assert "#cf-please-wait" in js
    assert "'interstitial'" in js


def test_challenge_probe_detects_shadow_and_custom_widgets_by_response_field():
    """The 2026-07-19 auto-click lesson: Turnstile's iframe lives in a CLOSED
    shadow root querySelectorAll cannot pierce, and a custom container needs no
    known class — but every major vendor injects a hidden RESPONSE FIELD into the
    host page's light DOM. Pin that detection channel, its zone output (the
    element walk skips anything overlapping a zone), and the widget-size bound
    that keeps a zone from swallowing the whole form."""
    from app.core import dom_observe

    js = dom_observe._EXTRACT_JS
    assert "g-recaptcha-response" in js
    assert "cf-turnstile-response" in js
    assert "h-captcha-response" in js
    # known containers still checked (explicit renders).
    assert ".cf-turnstile" in js
    assert ".g-recaptcha" in js
    assert ".h-captcha" in js
    # zones feed the element walk's no-touch skip.
    assert "inChallengeZone" in js
    assert "widgetRect" in js
    # the solved tell for the resumed commit.
    assert "solved" in js


def test_detect_challenge_on_the_cloudflare_host():
    assert browser_loop.detect_challenge(
        _cobs(url="https://challenges.cloudflare.com/turnstile/if")
    ) == ("Cloudflare", "challenges.cloudflare.com")


def test_detect_challenge_on_an_interstitial_title():
    """The full-page interstitial fallback — by document title, host-agnostic."""
    assert browser_loop.detect_challenge(
        _cobs(url="https://shop.test/", title="Just a moment...")
    ) == ("CAPTCHA", "shop.test")
    assert browser_loop.detect_challenge(
        _cobs(url="https://shop.test/", title="Verify you are human")
    ) == ("CAPTCHA", "shop.test")


def test_a_normal_page_is_not_a_challenge():
    assert browser_loop.detect_challenge(
        _cobs(url="https://youtube.com/results", title="lofi hip hop - YouTube")
    ) is None


async def test_a_captcha_halts_the_loop_and_never_interacts():
    """The core 15.4 behavior: a challenge stops the loop cleanly, flags
    challenge_required, consults the model ZERO times, and — the hard rule —
    touches NOTHING on the challenge (never auto-solved or auto-interacted)."""
    page = {
        "url": "https://site.test/verify", "title": "T",
        "elements": [_el(1, role="checkbox", name="I'm not a robot")],
        "total": 1, "text": "",
        "challenge": {"kind": "reCAPTCHA", "blocking": True},
    }
    session = FakeSession(ScriptedPage([page]))
    # A naive decision would click the checkbox; it must never be reached.
    provider = FakeProvider(['{"action":"click","index":1}'])

    outcome = await run_browse(session, "sign up on site.test", provider)

    assert outcome.challenge_required is True
    assert outcome.success is False
    assert outcome.challenge_kind == "reCAPTCHA"
    assert outcome.challenge_site == "site.test"
    assert "site.test/verify" in outcome.challenge_url
    assert provider.calls == 0          # stopped before any decision
    assert session.page.acted == []     # nothing clicked — the challenge is untouched


async def test_a_cloudflare_interstitial_title_halts_the_loop():
    """A whole-page 'Just a moment...' interstitial with no probe still stops the
    loop (the title fallback), never interacting with the check."""
    page = {
        "url": "https://shop.test/", "title": "Just a moment...",
        "elements": [], "total": 0, "text": "", "challenge": None,
    }
    session = FakeSession(ScriptedPage([page]))
    provider = FakeProvider(['{"action":"done","reason":"x"}'])

    outcome = await run_browse(session, "open the shop", provider)

    assert outcome.challenge_required is True
    assert outcome.challenge_kind == "CAPTCHA"
    assert provider.calls == 0
    assert session.page.acted == []


async def test_a_non_challenge_run_leaves_challenge_required_false():
    session = FakeSession(ScriptedPage([_page([_el(1, role="link", name="Home")])]))
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    outcome = await run_browse(session, "look", provider)
    assert outcome.challenge_required is False
    assert outcome.success is True


# ------------------------------------- embedded widgets, mode-split (2026-07-19)
def _embedded_challenge(solved=False, kind="reCAPTCHA"):
    return {
        "kind": kind, "mode": "embedded", "blocking": False, "solved": solved,
        "zones": [{"x": 100, "y": 400, "w": 304, "h": 78}],
    }


def test_detect_challenge_ignores_an_embedded_widget():
    """The mode split: an embedded widget does NOT stop observation — its
    controls are structurally untouchable (never stamped, act refused), so the
    loop works around it and the commit submit gate pauses at the one moment it
    gates progress. Only an interstitial halts here."""
    assert browser_loop.detect_challenge(
        _cobs(url="https://site.test/form", challenge=_embedded_challenge())
    ) is None
    interstitial = {"kind": "Cloudflare", "mode": "interstitial", "blocking": True}
    assert browser_loop.detect_challenge(
        _cobs(url="https://site.test/", challenge=interstitial)
    ) == ("Cloudflare", "site.test")


def test_unsolved_embedded_challenge_matrix():
    unsolved = browser_loop.unsolved_embedded_challenge
    # visible + unsolved → the gate fires.
    assert unsolved(
        _cobs(url="https://site.test/form", challenge=_embedded_challenge())
    ) == ("reCAPTCHA", "site.test")
    # solved (the human ticked it — a response field carries the token) → clear.
    assert unsolved(
        _cobs(url="https://site.test/form", challenge=_embedded_challenge(solved=True))
    ) is None
    # no zones (invisible v3 / hidden modal) must never pause a submit.
    assert unsolved(
        _cobs(url="https://site.test/form",
              challenge={"kind": "reCAPTCHA", "mode": "embedded", "zones": []})
    ) is None
    # an interstitial belongs to detect_challenge, not this gate.
    assert unsolved(
        _cobs(url="https://site.test/",
              challenge={"kind": "Cloudflare", "mode": "interstitial", "blocking": True})
    ) is None
    assert unsolved(_cobs(url="https://site.test/", challenge=None)) is None


async def test_a_read_browse_continues_around_an_embedded_widget():
    """The false-positive class killed: a page that merely CONTAINS a captcha
    widget no longer pauses a read-only browse — the loop keeps working (the
    widget itself is untouchable by construction)."""
    page = {
        "url": "https://site.test/jobs", "title": "Jobs",
        "elements": [_el(1, role="link", name="Open the posting", href="/p/1")],
        "total": 1, "text": "",
        "challenge": _embedded_challenge(),
    }
    session = FakeSession(ScriptedPage([page]))
    provider = FakeProvider(['{"action":"done","reason":"read it"}'])

    outcome = await run_browse(session, "read the job posting", provider)

    assert outcome.challenge_required is False
    assert outcome.success is True
    assert provider.calls == 1          # the loop proceeded to a real decision


async def test_act_refuses_an_element_overlapping_a_challenge_zone():
    """The act-time backstop: even if an element overlapping the widget's box
    reaches the loop (drift — the widget rendered between observation and act),
    the click is refused in code. The hard rule does not rest on the probe
    firing first."""
    from app.core import dom_observe

    page = ScriptedPage([_page([_el(1, role="checkbox", name="I'm not a robot")])])
    session = FakeSession(page)
    obs = dom_observe.Observation(
        observation_id="o", url="https://site.test/form", title="",
        elements=[dom_observe.Element(
            index=1, role="checkbox", name="I'm not a robot", rect=(120, 410, 200, 40),
        )],
        element_total=1, page_text="", text_truncated=False,
        challenge=_embedded_challenge(),
    )
    ok, note = await browser_loop._act(session, obs, {"action": "click", "index": 1})
    assert ok is False
    assert "verification widget" in note
    assert page.acted == []             # the click never landed


# ------------------------- the submit-gesture gate (action-level safety)
# With the network open to page traffic, what keeps the agent from acting is the
# refusal in _act: a click on a form's submit control, a send/post/upload/like/
# delete/buy control, or Enter in a non-search field, dies in code. A genuine
# SEARCH submit is exempt (submitting a search IS reading). The old method=GET
# exemption was REMOVED (2026-07-22): a JS/contenteditable send has no <form
# method> and read as GET, so trusting GET let LinkedIn's message send through.
def _form_obs(**form_kwargs):
    from app.core import dom_observe

    return dom_observe.Observation(
        observation_id="o", url="https://site.test/job", title="",
        elements=[dom_observe.Element(
            index=1, role="button", name="Apply now", **form_kwargs,
        )],
        element_total=1, page_text="", text_truncated=False,
    )


async def test_act_refuses_clicking_a_submit_control_in_read_mode():
    page = ScriptedPage([_page([_el(1, role="button", name="Apply now")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="POST")
    ok, note = await browser_loop._act(session, obs, {"action": "click", "index": 1})
    assert ok is False
    assert "never acts without your approval" in note
    assert page.acted == []             # the click never landed


async def test_act_refuses_clicking_a_submit_control_in_commit_mode_too():
    """In commit mode the ONLY sanctioned submit is submit_commit() after the
    signature approval armed the permit — a direct click would bypass the
    approved contract."""
    page = ScriptedPage([_page([_el(1, role="button", name="Apply now")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="POST")
    ok, note = await browser_loop._act(
        session, obs, {"action": "click", "index": 1}, commit=True
    )
    assert ok is False
    assert "approved submit" in note
    assert page.acted == []


async def test_act_refuses_enter_that_would_submit_a_form():
    page = ScriptedPage([_page([_el(1, role="input", name="Email")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=False, form_method="POST")
    ok, note = await browser_loop._act(
        session, obs, {"action": "type", "index": 1, "text": "x", "submit": True}
    )
    assert ok is False
    assert "never acts without your approval" in note
    assert page.acted == []


async def test_act_allows_submitting_a_search_form():
    """Submitting a search is reading — the exemption that keeps ordinary site
    search (the model filling a search box and pressing Enter) working."""
    page = ScriptedPage([_page([_el(1, role="searchbox", name="Search")])])
    session = FakeSession(page)
    obs = _form_obs(
        form_member=True, form_submit=False, form_method="POST", form_search=True
    )
    ok, _ = await browser_loop._act(
        session, obs, {"action": "type", "index": 1, "text": "jane", "submit": True}
    )
    assert ok is True


async def test_act_gates_a_non_search_get_form_submit():
    """The method=GET loophole is CLOSED (2026-07-22). A non-search form's submit
    is refused whatever its method — a JS/contenteditable send carries no <form
    method> and read as GET, so trusting GET was the LinkedIn message-send hole.
    A real GET *search* stays exempt via form_search / a search role, not method."""
    page = ScriptedPage([_page([_el(1, role="button", name="Send")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="GET")
    ok, note = await browser_loop._act(session, obs, {"action": "click", "index": 1})
    assert ok is False
    assert "never acts without your approval" in note


async def test_act_allows_the_approved_action_on_resume():
    """After the user's yes, the resumed browse carries the PERMIT for that exact
    control and the backstop lets it through (the run_browse hand-off that stopped
    it is skipped upstream)."""
    page = ScriptedPage([_page([_el(1, role="button", name="Send")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="POST")
    action = {"action": "click", "index": 1}
    permit = browser_loop.gesture_fingerprint(
        action, obs.index_map()[1], obs.url
    )
    ok, _ = await browser_loop._act(
        session, obs, action, approved_gesture=permit
    )
    assert ok is True


async def test_act_refuses_a_DIFFERENT_gesture_than_the_one_approved():
    """THE POINT of a fingerprint. A permit for one control does not authorise
    another — the boolean it replaced authorised every gesture in the run."""
    page = ScriptedPage([_page([_el(1, role="button", name="Send")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="POST")
    ok, note = await browser_loop._act(
        session, obs, {"action": "click", "index": 1},
        approved_gesture="a-permit-for-something-else",
    )
    assert ok is False
    assert "without your approval" in note


async def test_act_gates_a_js_send_button_by_label():
    """A JS send control with NO <form> membership (a contenteditable messenger's
    Send) is still caught — by its action-verb label, the secondary net for the
    structural form_submit signal."""
    from app.core import dom_observe

    page = ScriptedPage([_page([_el(1, role="button", name="Send")])])
    session = FakeSession(page)
    # No form_* → not a form member; the action-verb label is what fires.
    obs = dom_observe.Observation(
        observation_id="o", url="https://site.test/", title="",
        elements=[dom_observe.Element(index=1, role="button", name="Send")],
        element_total=1, page_text="", text_truncated=False,
    )
    ok, note = await browser_loop._act(session, obs, {"action": "click", "index": 1})
    assert ok is False
    assert "never acts without your approval" in note


def test_is_action_gesture_matrix():
    """The positive action-gesture detector (the guarantee): a form's submit
    control, Enter in a non-search field, and JS action-verb controls are
    actions; a genuine search submit and plain navigation are not."""
    from app.core import dom_observe

    E = dom_observe.Element
    send_btn = E(index=1, role="button", name="Send", form_member=True, form_submit=True)
    assert browser_loop._is_action_gesture({"action": "click"}, send_btn) is True

    msg = E(index=2, role="textbox", name="Write a message", form_member=True)
    assert browser_loop._is_action_gesture(
        {"action": "type", "submit": True}, msg
    ) is True
    # a plain (non-submit) type into the same box is not an action — drafting is fine
    assert browser_loop._is_action_gesture(
        {"action": "type", "submit": False}, msg
    ) is False

    box = E(index=3, role="searchbox", name="Search")
    assert browser_loop._is_search_target(box) is True
    assert browser_loop._is_action_gesture(
        {"action": "type", "submit": True}, box
    ) is False

    # form_search (positively detected in the JS) → submitting a search is reading
    sform = E(index=4, role="textbox", name="q", form_member=True, form_search=True)
    assert browser_loop._is_action_gesture(
        {"action": "type", "submit": True}, sform
    ) is False

    # a JS control with no <form> is caught by its action-verb label…
    assert browser_loop._is_action_gesture(
        {"action": "click"}, E(index=5, role="button", name="Post")
    ) is True
    assert browser_loop._is_action_gesture(
        {"action": "click"}, E(index=6, role="button", name="Like")
    ) is True
    # …but a plain navigation/reading label is not an action
    assert browser_loop._is_action_gesture(
        {"action": "click"}, E(index=7, role="button", name="Show more")
    ) is False
    assert browser_loop._is_action_gesture(
        {"action": "click"}, E(index=8, role="button", name="Next page")
    ) is False


async def test_run_browse_pauses_for_approval_before_a_send():
    """The READ loop STOPS at a world-acting gesture and returns the
    action-approval hand-off — nothing is typed or sent (the LinkedIn incident:
    a message send in a READ browse, now caught structurally)."""
    page = ScriptedPage([
        _page(
            [_el(1, role="textbox", name="Write a message",
                 form={"submit": False, "method": "POST", "search": False})],
            url="https://linkedin.com/messaging/thread/new/",
        ),
    ])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"type","index":1,"text":"hi anas","submit":true}'])
    outcome = await browser_loop.run_browse(session, "message anas hi", provider)
    assert outcome.action_approval_required is True
    assert "hi anas" in outcome.action_description
    assert outcome.action_site == "linkedin.com"
    assert page.acted == []             # nothing typed, nothing sent


async def test_run_browse_performs_the_action_once_approved():
    """With the PERMIT for that gesture (the resume after the user's yes) the gate
    stands down and the one approved gesture fires — fill + Enter."""
    page = ScriptedPage([
        _page(
            [_el(1, role="textbox", name="Write a message",
                 form={"submit": False, "method": "POST"})],
            url="https://linkedin.com/messaging/thread/new/",
        ),
        _page([_el(1, role="link", name="Message sent")],
              url="https://linkedin.com/messaging/thread/123/"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([
        '{"action":"type","index":1,"text":"hi anas","submit":true}',
        '{"action":"done","reason":"the message was sent"}',
    ])
    # The permit the pause would have handed back: the same control, same site.
    element = browser_loop.dom_observe.Element(
        index=1, role="textbox", name="Write a message"
    )
    permit = browser_loop.gesture_fingerprint(
        {"action": "type", "submit": True}, element,
        "https://linkedin.com/messaging/thread/new/",
    )
    outcome = await browser_loop.run_browse(
        session, "message anas hi", provider, approved_gesture=permit
    )
    assert outcome.action_approval_required is False
    assert outcome.success is True
    kinds = [a[2] for a in page.acted]
    assert "fill" in kinds and "press" in kinds


async def test_a_permit_for_one_gesture_does_not_authorise_a_second():
    """ONE APPROVAL, ONE GESTURE. The permit is spent when it fires, so a SECOND
    world-acting gesture in the same run pauses again — even the identical one.
    Under the boolean this replaced, a yes to "send this message" also authorised
    whatever the loop chose next."""
    page = ScriptedPage([
        _page([_el(1, role="button", name="Send",
                   form={"submit": True, "method": "POST"})],
              url="https://site.test/a"),
        _page([_el(1, role="button", name="Send",
                   form={"submit": True, "method": "POST"})],
              url="https://site.test/a"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([
        '{"action":"click","index":1}',      # the approved one — fires
        '{"action":"click","index":1}',      # a second act — must pause
    ])
    element = browser_loop.dom_observe.Element(index=1, role="button", name="Send")
    permit = browser_loop.gesture_fingerprint(
        {"action": "click"}, element, "https://site.test/a"
    )

    outcome = await browser_loop.run_browse(
        session, "send it", provider, approved_gesture=permit
    )

    assert outcome.action_approval_required is True     # paused on the SECOND
    assert len([a for a in page.acted if a[2] == "click"]) == 1   # acted once


async def test_a_performed_gesture_is_recorded_for_the_audit():
    """A mutation that cannot be found in the audit trail is not auditable, and
    every other write in this codebase is. The outcome names what it did."""
    page = ScriptedPage([
        _page([_el(1, role="button", name="Send",
                   form={"submit": True, "method": "POST"})],
              url="https://site.test/a"),
        _page([_el(1, role="link", name="Sent")], url="https://site.test/done"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([
        '{"action":"click","index":1}',
        '{"action":"done","reason":"sent"}',
    ])
    element = browser_loop.dom_observe.Element(index=1, role="button", name="Send")
    permit = browser_loop.gesture_fingerprint(
        {"action": "click"}, element, "https://site.test/a"
    )

    outcome = await browser_loop.run_browse(
        session, "send it", provider, approved_gesture=permit
    )

    assert outcome.success is True
    assert "Send" in outcome.performed_gesture or "click" in outcome.performed_gesture


async def test_a_read_only_run_records_no_performed_gesture():
    """Empty on the overwhelming majority of runs — a READ browse acts on nothing,
    and the audit field must not imply otherwise."""
    page = ScriptedPage([_page([_el(1, role="link", name="Somewhere", href="/x")])])
    provider = FakeProvider(['{"action":"done","reason":"just looked"}'])

    outcome = await browser_loop.run_browse(FakeSession(page), "just look", provider)

    assert outcome.success is True
    assert outcome.performed_gesture == ""


async def test_a_permit_does_not_travel_to_another_site():
    """The host is in the fingerprint, so an approval on one site cannot authorise
    the same-looking control on another."""
    element = browser_loop.dom_observe.Element(index=1, role="button", name="Send")
    here = browser_loop.gesture_fingerprint(
        {"action": "click"}, element, "https://linkedin.com/x"
    )
    there = browser_loop.gesture_fingerprint(
        {"action": "click"}, element, "https://evil.test/x"
    )
    assert here != there


async def test_the_fingerprint_ignores_the_element_index():
    """Indices are re-assigned every observation, so an index-keyed permit would
    authorise whatever happened to be third on the page next time."""
    a = browser_loop.dom_observe.Element(index=1, role="button", name="Send")
    b = browser_loop.dom_observe.Element(index=47, role="button", name="Send")
    url = "https://site.test/"
    assert browser_loop.gesture_fingerprint({"action": "click"}, a, url) == (
        browser_loop.gesture_fingerprint({"action": "click"}, b, url)
    )


async def test_commit_submit_gates_on_an_unsolved_embedded_widget():
    """The submit-time gate: form filled, model says submit, the widget carries
    no token → the loop stops with challenge_required + mode 'embedded' (the
    tool then HOLDS the session for the human to solve in this window). With the
    widget SOLVED, the same submit proceeds to the commit pause."""

    class GateSession(FakeSession):
        def __init__(self, page):
            super().__init__(page)
            self.uploads = []

        async def read_commit_target(self, obs, index):
            return {"action": "https://site.test/apply", "method": "POST",
                    "fields": [], "has_password": False}

    unsolved_page = {
        "url": "https://site.test/form", "title": "Apply",
        "elements": [_el(1, role="button", name="Submit application")],
        "total": 1, "text": "",
        "challenge": _embedded_challenge(),
    }
    session = GateSession(ScriptedPage([unsolved_page]))
    provider = FakeProvider(['{"action":"submit","index":1}'])
    outcome = await run_browse(session, "apply", provider, commit=True)

    assert outcome.challenge_required is True
    assert outcome.challenge_mode == "embedded"
    assert outcome.challenge_kind == "reCAPTCHA"
    assert outcome.commit_required is False
    assert session.page.acted == []      # nothing was submitted or touched

    solved_page = dict(unsolved_page, challenge=_embedded_challenge(solved=True))
    session2 = GateSession(ScriptedPage([solved_page]))
    provider2 = FakeProvider(['{"action":"submit","index":1}'])
    outcome2 = await run_browse(session2, "apply", provider2, commit=True)

    assert outcome2.challenge_required is False
    assert outcome2.commit_required is True
    assert outcome2.commit_state["url"] == "https://site.test/apply"


# ---------------------------------------------------------------- media registry
class FakeMediaSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_media_registry_keeps_one_session_and_stops_it():
    first, second = FakeMediaSession(), FakeMediaSession()

    await browser_session.register_media(first, title="Song A", url="https://youtube.com/a")
    assert browser_session.active_media() == {"title": "Song A", "url": "https://youtube.com/a"}

    # A second play STOPS the first — one window at a time.
    await browser_session.register_media(second, title="Song B", url="https://youtube.com/b")
    assert first.closed is True
    assert browser_session.active_media()["title"] == "Song B"

    # stop_media closes the live one and clears the registry; idempotent after.
    assert await browser_session.stop_media() is True
    assert second.closed is True
    assert browser_session.active_media() is None
    assert await browser_session.stop_media() is False


async def test_active_media_is_none_when_nothing_plays():
    await browser_session.stop_media()
    assert browser_session.active_media() is None


# ----------------------------------------------------- form fills (15.2)
from app.core.autofill import to_snapshot
from app.db.models import AutofillField


class RecordingProvider(FakeProvider):
    """A FakeProvider that keeps every decision PROMPT it was handed — so a test
    can assert a secret value never appeared in one."""

    def __init__(self, responses):
        super().__init__(responses)
        self.prompts = []

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.prompts.append(messages[0].content)
        return await super().chat(messages, temperature, max_tokens)


def _profile(*fields):
    return to_snapshot(
        [AutofillField(key=k, label=lbl, value=v, kind=kind) for (k, lbl, v, kind) in fields]
    )


async def test_a_grounded_form_value_fills():
    """A commit-mode `type` of a value in the user's PROFILE proceeds — the fill
    lands on the page and the loop does not pause."""
    page = _page([_el(1, role="textbox", name="Full name")])
    session = FakeSession(ScriptedPage([page]))
    provider = RecordingProvider([
        '{"action":"type","index":1,"text":"Khawar Mohiuddin","submit":false}',
        '{"action":"done","reason":"filled"}',
    ])
    profile = _profile(("name", "Name", "Khawar Mohiuddin", "text"))

    outcome = await run_browse(session, "apply to a job", provider, commit=True, profile=profile)

    assert outcome.success is True
    assert outcome.fill_required is False
    assert ("fill", "Khawar Mohiuddin") in [(a[2], a[3]) for a in session.page.acted]


async def test_an_ungrounded_form_value_pauses_to_ask():
    """A value that is NOT in the profile or the user's words (only a page could
    have supplied it) STOPS the loop with fill_required — nothing is typed."""
    page = _page([_el(1, role="textbox", name="Email")])
    session = FakeSession(ScriptedPage([page]))
    provider = FakeProvider(['{"action":"type","index":1,"text":"attacker@evil.com","submit":false}'])

    outcome = await run_browse(session, "fill the form", provider, commit=True, profile=_profile())

    assert outcome.fill_required is True
    assert outcome.fill_field == "Email"          # names the field to ask about
    assert all(a[2] != "fill" for a in session.page.acted)   # nothing was typed


async def test_read_mode_typing_is_never_fill_grounded():
    """Grounding applies to FORM fills (commit mode). A read-mode `type` takes a
    query that is obviously not 'profile data' and must not be blocked. Two
    search boxes make the fast path defer, so this exercises the model path."""
    home = _page(
        [_el(1, role="searchbox", name="Search"), _el(2, role="searchbox", name="Site search")],
        url="https://site.test/",
    )
    session = FakeSession(ScriptedPage([home]))
    provider = FakeProvider([
        '{"action":"type","index":1,"text":"quarterly earnings report","submit":false}',
        '{"action":"done","reason":"found"}',
    ])
    # commit defaults False → no fill grounding; the search proceeds.
    outcome = await run_browse(session, "look something up", provider)
    assert outcome.success is True
    assert ("fill", "quarterly earnings report") in [(a[2], a[3]) for a in session.page.acted]


async def test_a_secret_is_filled_by_code_and_never_seen_by_the_model():
    """The password-never-read rule: the model types a {{secret:key}} placeholder,
    code substitutes the REAL value onto the page, and the real value appears in
    NO prompt and NO history (only the placeholder does)."""
    page = _page([_el(1, role="textbox", name="API key")])
    session = FakeSession(ScriptedPage([page]))
    provider = RecordingProvider([
        '{"action":"type","index":1,"text":"{{secret:api_key}}","submit":false}',
        '{"action":"done","reason":"filled"}',
    ])
    profile = _profile(("api_key", "API key", "s3cr3t-value", "secret"))

    outcome = await run_browse(session, "fill the api key form", provider, commit=True, profile=profile)

    assert outcome.success is True
    # the REAL secret reached the page
    assert ("fill", "s3cr3t-value") in [(a[2], a[3]) for a in session.page.acted]
    # but never a prompt (the profile block lists only the key + placeholder)
    assert all("s3cr3t-value" not in p for p in provider.prompts)
    # and history carries the PLACEHOLDER, never the value
    assert any("{{secret:api_key}}" in p for p in provider.prompts[1:])


# ------------------------------ vision-first hybrid (2026-07-21, was 15.3)
class VisionPage(ScriptedPage):
    """A ScriptedPage the loop can also SCREENSHOT — the hybrid decision needs a
    JPEG. Its scripted payloads carry element rects + a viewport, so a fractional
    point the vision model returns maps back to a real element."""

    async def screenshot(self, **kwargs):
        return b"\xff\xd8\xff\xe0-fake-jpeg"


class FakeVision:
    """A scripted image-in / text-out model. Records how many times it was asked
    — the cost/degradation claims below are call-count assertions."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.closed = False

    async def describe(self, *, prompt, image_jpeg):
        self.calls += 1
        return self.replies.pop(0) if self.replies else ""

    async def aclose(self):
        self.closed = True


def _vel(index, rect, role="button", name="", href=""):
    """An element dict carrying an on-screen box (x, y, w, h) for point mapping."""
    return {
        "index": index, "role": role, "name": name, "value": "", "href": href,
        "rect": {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]},
    }


def _vpage(elements, url="https://site.test/", viewport=(1000, 1000)):
    return {
        "url": url, "title": "T", "elements": list(elements), "total": len(elements),
        "text": "", "viewport": {"width": viewport[0], "height": viewport[1]},
    }


async def test_vision_is_the_primary_decision_channel():
    """The hybrid (owner decision, 2026-07-21): with a vision provider
    configured, EVERY decision step goes to it with the marked screenshot —
    the text provider is the fallback, not the first call. Here vision answers
    'done' and the text provider's scripted reply is never consumed."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])]))
    provider = FakeProvider(['{"action":"click","index":1}'])   # must stay unread
    vision = FakeVision(['{"action":"done","reason":"ok"}'])

    outcome = await run_browse(
        session, "just look", provider, vision=vision, vision_first=True
    )

    assert outcome.success is True
    assert vision.calls == 1
    assert outcome.vision_calls == 1
    assert provider.calls == 0             # text provider never consulted


async def test_a_vision_point_maps_to_an_element_through_the_index_contract():
    """Vision LOCATES, DOM ACTS — unchanged under the hybrid. The vision reply
    names a fractional POINT over an icon-only button; code maps it to the REAL
    element index and the click runs through the ordinary index contract
    (resolve → the fake handle), never a coordinate click."""
    icon = _vpage([_vel(1, (400, 400, 200, 200), name="")], url="https://site.test/app")
    nextp = _vpage([_vel(1, (0, 0, 100, 40), name="Done marker")], url="https://site.test/next")
    session = FakeSession(VisionPage([icon, nextp]))
    provider = FakeProvider([])            # never needed — vision answers both steps
    vision = FakeVision([
        '{"action":"click","x":0.5,"y":0.5}',    # (500,500) ∈ (400..600)
        '{"action":"done","reason":"ok"}',
    ])

    outcome = await run_browse(
        session, "click the icon", provider, vision=vision, vision_first=True
    )

    assert outcome.success is True
    assert vision.calls == 2
    # The vision-located click hit element 1 THROUGH the index contract (the fake
    # handle records a click with the real index — a point never clicks directly).
    assert any(k == "click" and idx == 1 for (_i, idx, k, v) in session.page.acted)


# ------------------------------------------------------ the DOM-first posture
# THE DEFAULT since 2026-07-26, reversing the 2026-07-21 vision-first decision on
# measured grounds: vision spent up to 12s per step waiting on cooling keys and
# returned "unusable", while the DOM channel did all the real work. So vision is
# consulted only where the DOM genuinely cannot help.
async def test_dom_first_does_not_consult_vision_on_a_healthy_page():
    """The saving. A page with actionable elements is the DOM's job; a configured
    vision provider sits idle and costs neither a screenshot nor a call."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])]))
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    vision = FakeVision(['{"action":"done","reason":"vision"}'])   # must stay unread

    outcome = await run_browse(session, "just look", provider, vision=vision)

    assert outcome.success is True
    assert provider.calls == 1
    assert vision.calls == 0
    assert outcome.vision_calls == 0


async def test_dom_first_escalates_to_vision_when_the_text_channel_cannot_decide():
    """Vision earns its call exactly where DOM failed — the evidence_resolver
    escalation shape, applied to perception. The text provider returns junk twice
    (its own reply plus the one retry), then vision answers and the run completes."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])]))
    provider = FakeProvider(["not json", "still not json"])
    vision = FakeVision(['{"action":"done","reason":"vision saw it"}'])

    outcome = await run_browse(session, "just look", provider, vision=vision)

    assert outcome.success is True
    assert provider.calls == 2      # tried, and retried
    assert vision.calls == 1        # then escalated
    assert outcome.vision_calls == 1


async def test_dom_first_uses_vision_immediately_on_a_page_with_no_elements():
    """A canvas or pure-image UI: the DOM has nothing to offer, so waiting for the
    text channel to fail first would just spend a call to learn that."""
    session = FakeSession(VisionPage([_vpage([])]))
    provider = FakeProvider(['{"action":"done","reason":"text"}'])
    vision = FakeVision(['{"action":"done","reason":"vision"}'])

    outcome = await run_browse(session, "look at the canvas", provider, vision=vision)

    assert outcome.success is True
    assert vision.calls == 1
    assert provider.calls == 0


async def test_vision_calls_are_hard_capped_per_run():
    """A ceiling that did not exist before: vision was bounded only by a 12s
    timeout and a 2-strike FAILURE breaker, so a provider answering slowly but
    USABLY could be consulted on all 25 steps — minutes nobody asked for. The
    breaker never fires here precisely because every reply is usable."""
    page = VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    vision = FakeVision(['{"action":"scroll","direction":"down"}'] * 40)

    outcome = await run_browse(
        session, "look around", provider, vision=vision, vision_first=True,
        max_actions=browser_loop.MAX_VISION_CALLS + 4,
    )

    assert vision.calls == browser_loop.MAX_VISION_CALLS
    assert outcome.vision_calls == browser_loop.MAX_VISION_CALLS
    assert provider.calls >= 1      # the text channel carried the rest


async def test_vision_off_stays_dom_only_and_stops():
    """Unconfigured (vision=None) → the text-only loop, zero screenshot work; a
    stuck decide stops honestly, no crash, zero vision anything."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (400, 400, 200, 200), name="")])]))
    provider = FakeProvider(['{"action":"click","index":99}'])

    outcome = await run_browse(session, "click the icon", provider, vision=None)

    assert outcome.success is False
    assert "safe next action" in outcome.error
    assert outcome.vision_calls == 0


async def test_a_vision_failure_falls_back_to_the_text_provider_in_the_same_step():
    """Degradation is PER STEP, never per run: a junk vision reply hands the
    SAME decision to the text provider, which completes the goal — one flaky
    vision call costs one fallback, not the browse."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])]))
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    vision = FakeVision(["not json at all"])

    outcome = await run_browse(
        session, "just look", provider, vision=vision, vision_first=True
    )

    assert outcome.success is True
    assert vision.calls == 1               # tried first...
    assert outcome.vision_calls == 1
    assert provider.calls == 1             # ...text provider decided the step


async def test_vision_circuit_breaker_stops_retrying_a_dead_provider():
    """Live 2026-07-21: an out-of-quota Gemini key 429'd on EVERY step, and the
    per-step fallback dutifully retried it each time — a screenshot plus a
    doomed API call per step, for the whole run. After _VISION_FAILURE_LIMIT
    consecutive unusable vision decisions the run goes text-only for its
    remainder; the text provider still carries every step."""
    page = VisionPage([_vpage([_vel(1, (0, 0, 100, 40), name="Go")])])
    session = FakeSession(page)
    provider = FakeProvider([
        '{"action":"scroll","direction":"down"}',
        '{"action":"scroll","direction":"down"}',
        '{"action":"scroll","direction":"down"}',
        '{"action":"scroll","direction":"down"}',
        '{"action":"done","reason":"ok"}',
    ])
    vision = FakeVision(["junk"] * 10)      # a dead key never returns an action

    outcome = await run_browse(
        session, "look around", provider, vision=vision, vision_first=True
    )

    assert outcome.success is True
    # tripped after the limit — the later steps never paid the doomed call
    assert vision.calls == browser_loop._VISION_FAILURE_LIMIT
    assert provider.calls == 5              # the text provider decided every step


async def test_a_vision_point_over_no_element_never_fabricates_a_click():
    """A point over nothing clickable maps to NO element → the vision decision
    is unusable and the step falls to the text provider; with that also junk,
    the loop stops honestly. Nothing is ever acted on."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (400, 400, 100, 100), name="")])]))
    provider = FakeProvider(["not json", "still not json"])  # incl. the one retry
    vision = FakeVision(['{"action":"click","x":0.9,"y":0.9}'])  # (900,900) — outside the box

    outcome = await run_browse(session, "stuck", provider, vision=vision)

    assert outcome.success is False
    assert vision.calls == 1
    assert session.page.acted == []  # no fabricated click


async def test_new_motion_and_element_actions_execute():
    """The richer action space (Phase 5): scroll/select_option parse and
    execute — select through the element handle (the index contract), scroll as
    read-only page motion exempt from the repeat dedupe."""
    page = VisionPage([
        _vpage([_vel(1, (0, 0, 100, 40), role="combobox", name="Country")]),
    ])
    session = FakeSession(page)
    provider = FakeProvider([
        '{"action":"scroll","direction":"down"}',
        '{"action":"scroll","direction":"down"}',
        '{"action":"scroll","direction":"down"}',   # repeats never trip the dedupe
        '{"action":"select_option","index":1,"value":"Pakistan"}',
        '{"action":"done","reason":"ok"}',
    ])

    outcome = await run_browse(session, "pick the country", provider)

    assert outcome.success is True
    assert any(k == "select" for (_i, _idx, k, _v) in session.page.acted)


# -------------------------------------- filter/facet surfacing (the daraz.pk fix)
# A chrome-heavy marketplace buries its filter controls below the ~80-element
# render window; observe() DID stamp them, so the fix surfaces them from the FULL
# list by their true index. These pin: the intent detector, that a buried filter
# control is surfaced (and a plain media goal surfaces nothing), the goal-specific
# ranking, that the block carries the TRUE stamped index, and the end-to-end wiring
# into the decision prompt + the guidance append.

def _filler(n, start=1):
    """n navigation-chrome links whose names share NO filter vocabulary — they
    fill the render window (like a marketplace header/rail) so a control after
    them lands OUTSIDE it. Long names so ~120 of them exceed the 6000-char budget."""
    return [
        browser_loop.dom_observe.Element(
            index=start + i, role="link",
            name=f"Navigation menu item number {start + i} placeholder link",
        )
        for i in range(n)
    ]


def test_wants_filtering_fires_on_a_filter_goal_not_a_media_goal():
    wf = browser_loop._wants_filtering
    assert wf("on daraz.pk find items with price under 10000")
    assert wf("show me the cheapest phones")
    assert wf("sort the results by price low to high")
    assert wf("filter by Samsung brand and 4 star rating")
    assert wf("laptops under Rs. 50000")
    # media / plain-search goals must stay OFF (guidance + block never fire)
    assert not wf("play the latest episode of one piece on anikoto.cz")
    assert not wf("search jane by the long faces on youtube")
    assert not wf("open my linkedin profile")


def test_relevant_controls_surfaces_a_buried_filter_control():
    """A price filter stamped at index ~201 (well past the render window) is pulled
    from the full list and surfaced — the model can act on it by index without ever
    asking for "more"."""
    els = _filler(200) + [
        browser_loop.dom_observe.Element(index=201, role="input", name="Min price"),
        browser_loop.dom_observe.Element(index=202, role="input", name="Max price"),
        browser_loop.dom_observe.Element(index=203, role="button", name="Apply filter"),
    ]
    obs = _obs(els, url="https://www.daraz.pk/catalog/?q=phones")
    # the filler really does fill the window, so the price controls are outside it
    _, end = browser_loop.dom_observe.visible_span(obs, 0)
    assert end < 201
    got = browser_loop._relevant_controls("find phones under 10000", obs)
    got_idx = {e.index for e in got}
    assert {201, 202, 203} <= got_idx
    # nav chrome (no filter vocabulary) is never surfaced
    assert all(e.index >= 201 for e in got)


def test_relevant_controls_empty_for_a_plain_media_goal():
    """No filter intent → nothing surfaced, even on a huge page with filter-shaped
    controls: the block and guidance must never fire for a play/watch goal."""
    els = _filler(200) + [
        browser_loop.dom_observe.Element(index=201, role="input", name="Min price"),
    ]
    obs = _obs(els)
    assert browser_loop._relevant_controls("play jane by the long faces", obs) == []


def test_relevant_controls_ranks_the_goal_specific_facet_first():
    """Goal-word overlap outranks a generic filter-vocabulary hit: 'sort by price'
    ranks a Sort control above an unrelated Brand filter."""
    els = _filler(200) + [
        browser_loop.dom_observe.Element(index=201, role="link", name="Brand filter"),
        browser_loop.dom_observe.Element(index=202, role="combobox", name="Sort by price"),
    ]
    obs = _obs(els)
    got = browser_loop._relevant_controls("sort the phones by price", obs)
    assert got[0].index == 202  # the Sort control the goal named comes first


def test_relevant_block_carries_the_true_stamped_index():
    """The rendered block reuses Element.render(), so a surfaced control shows its
    real index — exactly what the model answers with."""
    control = browser_loop.dom_observe.Element(index=207, role="input", name="Max price")
    block = browser_loop._relevant_block([control])
    assert "RELEVANT CONTROLS" in block
    assert "[207]" in block and "Max price" in block
    assert browser_loop._relevant_block([]) == ""


class _RecordingProvider(FakeProvider):
    """Captures the last decision prompt so we can assert what the model saw."""

    def __init__(self, responses):
        super().__init__(responses)
        self.last_prompt = ""

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.last_prompt = " ".join(getattr(m, "content", "") for m in messages)
        return await super().chat(messages, temperature=temperature, max_tokens=max_tokens)


async def test_filter_goal_injects_relevant_controls_and_guidance_into_the_prompt():
    """End to end: a filter goal on a page with a buried price control makes the
    decision prompt carry BOTH the RELEVANT CONTROLS block (with the true index)
    and the filter guidance — no search box, so the fast path defers and _decide
    runs. A plain media goal gets neither."""
    els = (
        [_el(i, role="link", name=f"Navigation menu item number {i} placeholder link")
         for i in range(1, 201)]
        + [_el(201, role="input", name="Min price"),
           _el(202, role="input", name="Max price"),
           _el(203, role="button", name="Apply filter")]
    )
    page = ScriptedPage([_page(els, url="https://www.daraz.pk/catalog/?q=phones")])
    session = FakeSession(page)
    provider = _RecordingProvider(['{"action":"done","reason":"filtered"}'])

    await run_browse(session, "on daraz.pk find phones under 10000", provider)

    assert provider.calls == 1  # no search box → fast path deferred to the model
    assert "RELEVANT CONTROLS" in provider.last_prompt
    assert "[201]" in provider.last_prompt  # the buried Min-price control, by index
    assert "slider you can only drag" in provider.last_prompt  # guidance

    # A plain media goal on the SAME page gets neither block nor guidance.
    page2 = ScriptedPage([_page(els, url="https://www.daraz.pk/catalog/?q=phones")])
    prov2 = _RecordingProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(FakeSession(page2), "play a phone review video", prov2)
    assert "RELEVANT CONTROLS" not in prov2.last_prompt
    assert "slider you can only drag" not in prov2.last_prompt


# ============================================================================
# STRUCTURED EXTRACTION + working memory (Skyvern/Atlas parity, DOM-only)
# ----------------------------------------------------------------------------
# `extract` reads structured data off the current page into working memory the
# loop carries across steps and returns in the outcome — the missing half of
# "add the highest-rated item under 10k to the cart" (gather → compare → act)
# and of list/research goals. Strictly READ (one LLM call, touches nothing).
# ============================================================================


def _text_obs(page_text, url="https://shop.test/"):
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title="", element_total=0,
        elements=[], page_text=page_text, text_truncated=False,
    )


def test_parse_action_accepts_extract_with_and_without_fields():
    a = _parse_action('{"action":"extract","fields":["name","price","rating"]}')
    assert a == {"action": "extract", "fields": ["name", "price", "rating"]}
    # No fields → an empty list (the extractor picks the page's key fields).
    b = _parse_action('{"action":"extract"}')
    assert b == {"action": "extract", "fields": []}
    # Junk fields are dropped; the field count is bounded.
    c = _parse_action('{"action":"extract","fields":["a","",null,"b"]}')
    assert c == {"action": "extract", "fields": ["a", "b"]}


def test_coerce_record_keeps_scalars_stringifies_nesting_drops_null():
    rec = _coerce_record(
        {"name": "Phone A", "price": 100, "in_stock": True, "meta": {"x": 1}, "gap": None, "": "z"}
    )
    assert rec["name"] == "Phone A"
    assert rec["price"] == 100
    assert rec["in_stock"] is True
    assert isinstance(rec["meta"], str)        # nested → stringified
    assert "gap" not in rec                     # null dropped
    assert "" not in rec                        # blank key dropped
    assert _coerce_record("not a dict") is None
    assert _coerce_record({"gap": None}) is None  # nothing usable → None


def test_extract_what_uses_fields_or_a_default():
    assert "name, price" in _extract_what(["name", "price"])
    assert "products" in _extract_what([])       # default names likely items


async def test_extract_data_pulls_records_from_the_page():
    obs = _text_obs("Phone A — $100 — 4.5 stars\nPhone B — $200 — 4.8 stars")
    provider = FakeProvider(['[{"name":"Phone A","price":"$100","rating":"4.5"},'
                             '{"name":"Phone B","price":"$200","rating":"4.8"}]'])
    records, note = await _extract_data(obs, ["name", "price", "rating"], provider)
    assert note == ""
    assert records == [
        {"name": "Phone A", "price": "$100", "rating": "4.5"},
        {"name": "Phone B", "price": "$200", "rating": "4.8"},
    ]
    assert provider.calls == 1


async def test_extract_data_no_page_text_makes_no_call():
    provider = FakeProvider(['[{"x":1}]'])
    records, note = await _extract_data(_text_obs(""), [], provider)
    assert records == []
    assert provider.calls == 0          # nothing to read → never bothers the LLM
    assert "no readable text" in note


# --------------------------------------------------------- structural-first
# THE LIVE DEFECT (2026-07-26): on daraz.pk's real results page `extract` returned
# ZERO records three times running and the run died reporting "the page didn't
# respond". The page had responded; the extractor was reading a 4000-char prose
# prefix (header/nav/filters) while the 158 product cards sat in the ELEMENT LIST
# it never looked at. app/browser/extract.py reads that list in code — so a grid
# now costs no LLM call at all, and cannot be fabricated.
def _grid_obs(url="https://www.daraz.pk/catalog/?q=yonex"):
    def card(name, href):
        return browser_loop.dom_observe.Element(
            index=len(cards) + 1, role="item", name=name[:120], name_full=name, href=href
        )

    cards = []
    for name, href in (
        ("Yonex Astrox 99 Pro Badminton Racket Rs. 24,999 4.7 (128)", "/p/astrox"),
        ("Yonex Nanoflare 001 Feel Racket Rs. 8,499 4.2 (31)", "/p/nanoflare"),
        ("Yonex Arcsaber 11 Pro Racket Rs. 41,500 4.9 (12)", "/p/arc11"),
    ):
        cards.append(card(name, href))
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title="Buy Yonex badminton racket Online",
        element_total=len(cards), elements=cards,
        page_text="Daraz nav, categories, filters — and no products.",
        text_truncated=True,
        text_full="Daraz nav, categories, filters — and no products.",
    )


async def test_extract_reads_a_results_grid_with_no_llm_call_at_all():
    provider = FakeProvider(['[{"never":"used"}]'])
    records, note = await _extract_data(_grid_obs(), ["name", "price"], provider)
    assert provider.calls == 0            # the whole point: the grid is read in code
    assert note == ""
    assert records == [
        {"name": "Yonex Astrox 99 Pro Badminton Racket", "price": "Rs. 24,999"},
        {"name": "Yonex Nanoflare 001 Feel Racket", "price": "Rs. 8,499"},
        {"name": "Yonex Arcsaber 11 Pro Racket", "price": "Rs. 41,500"},
    ]


async def test_a_field_the_structural_reader_cannot_parse_still_asks_the_llm():
    """`seller` is not something a text pattern can find, so the LLM runs — the
    structural rows exist but do not answer the question that was asked."""
    provider = FakeProvider(['[{"name":"Astrox","price":"Rs. 24,999","seller":"YonexPK"}]'])
    records, _ = await _extract_data(_grid_obs(), ["name", "price", "seller"], provider)
    assert provider.calls == 1
    assert records == [{"name": "Astrox", "price": "Rs. 24,999", "seller": "YonexPK"}]


async def test_structural_rows_survive_an_llm_path_that_finds_nothing():
    """Evidence is not a deletion — the same rule the truncated-array salvage and
    the browse tool's own failure path follow. Real rows beat reporting nothing."""
    records, note = await _extract_data(
        _grid_obs(), ["name", "price", "seller"], FakeProvider(["not json at all"])
    )
    assert note == ""
    assert [r["price"] for r in records] == ["Rs. 24,999", "Rs. 8,499", "Rs. 41,500"]


async def test_the_llm_path_is_shown_the_element_list_and_the_full_prose():
    """The other half of the defect: even when the LLM path runs, it used to see
    only the prompt-clipped prose. It now sees the items AND the whole text."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://shop.test/", title="",
        element_total=1,
        elements=[browser_loop.dom_observe.Element(
            index=1, role="item", name="Widget", name_full="Widget Deluxe Rs. 100"
        )],
        page_text="CLIPPED PROSE", text_truncated=True,
        text_full="CLIPPED PROSE plus THE REST OF THE PAGE",
    )
    provider = FakeProvider(['[{"seller":"x"}]'])
    await _extract_data(obs, ["seller"], provider)
    prompt = provider.prompts[-1]
    assert "Widget Deluxe Rs. 100" in prompt      # the element list, un-clipped
    assert "THE REST OF THE PAGE" in prompt       # the prose past the prompt budget


async def test_extract_data_junk_reply_is_a_clean_empty():
    obs = _text_obs("some content")
    records, note = await _extract_data(obs, [], FakeProvider(["not json at all"]))
    assert records == []
    assert note                          # a note, not a crash


async def test_extract_data_caps_the_record_count():
    obs = _text_obs("lots of rows")
    big = "[" + ",".join(f'{{"n":{i}}}' for i in range(_EXTRACT_MAX_RECORDS + 20)) + "]"
    records, _ = await _extract_data(obs, [], FakeProvider([big]))
    assert len(records) == _EXTRACT_MAX_RECORDS


def test_memory_block_renders_gathered_items_and_is_empty_when_none():
    assert _memory_block([]) == ""
    block = _memory_block([{"name": "A", "price": "$1"}, {"name": "B", "price": "$2"}])
    assert "DATA YOU HAVE GATHERED (2 item(s)" in block
    assert "name: A, price: $1" in block
    assert "name: B, price: $2" in block


async def test_extract_gathers_into_the_outcome_and_working_memory():
    """End to end: the model extracts, then finishes. The gathered records reach
    outcome.extracted; the second decision prompt carries the working-memory
    block so the model can compare before finishing."""
    page = ScriptedPage([{
        "url": "https://shop.test/phones", "title": "Phones",
        "elements": [_el(1, role="link", name="Phone A"), _el(2, role="link", name="Phone B")],
        "total": 2,
        "text": "Phone A $100 4.5 stars\nPhone B $200 4.8 stars",
    }])
    provider = _RecordingProvider([
        '{"action":"extract","fields":["name","price"]}',       # decide #1
        '[{"name":"Phone A","price":"$100"},{"name":"Phone B","price":"$200"}]',  # the extract
        '{"action":"done","reason":"compared them"}',           # decide #2
    ])
    outcome = await run_browse(FakeSession(page), "list the phones on shop.test with prices", provider)

    assert outcome.success
    assert outcome.extracted == [
        {"name": "Phone A", "price": "$100"},
        {"name": "Phone B", "price": "$200"},
    ]
    # decide #1 + the extract call + decide #2 = 3 provider calls.
    assert provider.calls == 3
    # The FINAL decision prompt saw the gathered data (working memory).
    assert "DATA YOU HAVE GATHERED" in provider.last_prompt
    assert "Phone A" in provider.last_prompt


# ---------------------------------------- the page-quality gate (2026-07-26)
#
# THE LIVE DEFECT. The loop handed the model whatever the observer returned and
# asked what to do next. Live, that meant asking about daraz.pk's results page
# reporting ZERO elements, and about eBay's 2-element Imperva bot wall titled
# "Pardon Our Interruption…" — which detect_challenge missed entirely, because
# its vendor list was Cloudflare-shaped. The model shrugged at the wall, and the
# whole browse died on the shrug.
#
# What KIND of page this is, is knowable in code. Deciding it first costs
# nothing and is what stops a decision being spent on a page that has no answer.
def test_assess_page_reads_a_bot_wall():
    """The eBay page, frozen. Structural (tiny) AND vendor prose — both required."""
    wall = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://www.ebay.com/sch/i.html", title="Pardon Our Interruption...",
        element_total=2, elements=[], text_truncated=False,
        page_text=(
            "Pardon Our Interruption. As you were browsing, something about your "
            "browser made us think you were a bot. Reference #18.91"
        ),
    )
    assert browser_loop.assess_page(wall) == "interstitial"
    assert browser_loop.detect_challenge(wall) is not None


def test_a_real_page_that_merely_mentions_a_wall_word_is_not_a_wall():
    """The conjunction is what makes reading page prose acceptable here: a real
    page can say anything, and only a STRUCTURALLY tiny one is ever tested."""
    article = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://news.test/a", title="How DataDome works",
        element_total=40, elements=[], text_truncated=False,
        page_text="Pardon our interruption " + ("real article body. " * 200),
    )
    assert browser_loop.assess_page(article) == "ready"
    assert browser_loop.detect_challenge(article) is None


def test_assess_page_distinguishes_empty_from_thin_from_ready():
    def obs(total, text=""):
        return browser_loop.dom_observe.Observation(
            observation_id="o", url="https://x.test/", title="T",
            element_total=total, elements=[], page_text=text, text_truncated=False,
        )

    assert browser_loop.assess_page(obs(0)) == "empty"
    assert browser_loop.assess_page(obs(2)) == "thin"
    assert browser_loop.assess_page(obs(30)) == "ready"


async def test_an_empty_page_is_re_read_before_a_decision_is_spent():
    """THE daraz.pk STEP. A fully-navigated results page observed with ZERO
    elements. Looking again costs a second; asking the model about nothing costs
    a step, an LLM call, and usually the run."""
    pages = ScriptedPage([
        _page([], url="https://www.daraz.pk/catalog/?q=racket", title="Rackets"),
        _page([_el(1, role="link", name="Yonex Astrox")],
              url="https://www.daraz.pk/catalog/?q=racket", title="Rackets"),
    ])
    provider = FakeProvider(['{"action":"done","reason":"found it"}'])

    outcome = await run_browse(FakeSession(pages), "find rackets", provider)

    assert outcome.success is True
    # ONE decision: the empty observation never reached the model.
    assert provider.calls == 1


async def test_a_thin_page_is_NOT_re_read():
    """Deliberately narrow. A 1-2 element page is an ordinary shape — a redirect
    stub, a bare search box, a 'continue' page — and 57 tests in this suite use
    single-element pages, which is a fair sample of how normal that is. Only
    ZERO is unambiguous enough to spend time on."""
    page = ScriptedPage([_page([_el(1, role="link", name="Continue")])])
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])

    outcome = await run_browse(FakeSession(page), "continue", provider)

    assert outcome.success is True
    assert provider.calls == 1


async def test_re_reading_an_empty_page_is_bounded():
    """A page that is genuinely empty costs a couple of seconds, not the run."""
    pages = ScriptedPage([_page([], url="https://void.test/", title="V")])
    provider = FakeProvider(['{"action":"done","reason":"nothing here"}'])

    outcome = await run_browse(FakeSession(pages), "look", provider)

    # It gave up re-reading and asked the model, which is the honest end state.
    assert provider.calls == 1
    assert outcome is not None


# ------------------------------------------- the 16-extract spin (2026-07-26)
#
# THE LIVE DEFECT. On eBay's real results page the model emitted the IDENTICAL
# {'action': 'extract', 'fields': ['name','price','shipping']} on sixteen
# consecutive steps, burning the whole 25-action budget and five minutes of wall
# clock. Nothing stopped it. The reason was PLACEMENT, not policy: both guards
# lived ~200 lines below, and `extract` (like `more`) `continue`s before reaching
# either — so it was structurally exempt from the machinery meant to bound it.
# It also reset consecutive_failures unconditionally, scoring a read that
# returned NOTHING as a success, so the failure cap could not fire either.
#
# The decision prompt already said "do not repeat an action that did not change
# the page". It was ignored sixteen times. A rule with nothing to check it is a
# suggestion.
async def test_a_repeated_extract_on_an_unchanged_page_is_refused():
    """THE INCIDENT, frozen. Sixteen identical extracts must not cost sixteen
    steps."""
    page = ScriptedPage([{
        "url": "https://www.ebay.com/sch/i.html?_nkw=racket", "title": "Rackets",
        "elements": [_el(1, role="link", name="Yonex Astrox 88D")],
        "total": 1,
        "text": "Yonex Astrox 88D Pro $94.00 Free shipping",
    }])
    # Alternating: a decision, then the extraction reply it triggers.
    script = []
    for _ in range(16):
        script.append('{"action":"extract","fields":["name","price","shipping"]}')
        script.append('[{"name":"Yonex Astrox 88D","price":"$94.00"}]')
    provider = FakeProvider(script)

    outcome = await run_browse(
        FakeSession(page), "extract the listings and say which is cheapest", provider
    )

    assert outcome.success is False
    # It stopped WELL short of the 16 the live run spent. Each extract costs two
    # provider calls (decide + extract), so the old behaviour was 32.
    assert provider.calls < 14, f"still spinning: {provider.calls} provider calls"
    # And the evidence it DID gather survives — that is the salvage contract.
    assert outcome.extracted


async def test_a_refused_extract_tells_the_model_why():
    """Refusing beats stopping only if the model is TOLD. The refusal lands in
    `history`, which is rendered into the next decision prompt, so the model can
    route around a dead end instead of re-choosing it."""
    page = ScriptedPage([{
        "url": "https://shop.test/x", "title": "Shop",
        "elements": [_el(1, role="link", name="A")],
        "total": 1, "text": "A $1",
    }])
    provider = _RecordingProvider([
        '{"action":"extract","fields":["name"]}', '[{"name":"A"}]',
        '{"action":"extract","fields":["name"]}', '[{"name":"A"}]',
        '{"action":"extract","fields":["name"]}', '[{"name":"A"}]',
        '{"action":"done","reason":"got it"}',
    ])
    await run_browse(FakeSession(page), "list the items", provider)

    assert "refused" in provider.last_prompt
    assert "already extracted this exact page" in provider.last_prompt


async def test_two_different_extractions_are_not_the_same_action():
    """The signature is keyed on the FIELDS, so asking for different data is not
    a repeat. Sorted, so field order alone is never a difference."""
    page = _page([_el(1, name="A")])
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://x.test/", title="T", element_total=0,
        elements=[], page_text="", text_truncated=False,
    )
    a = browser_loop._action_signature({"action": "extract", "fields": ["name", "price"]}, obs)
    b = browser_loop._action_signature({"action": "extract", "fields": ["price", "name"]}, obs)
    c = browser_loop._action_signature({"action": "extract", "fields": ["rating"]}, obs)
    assert a == b, "field ORDER is not a difference"
    assert a != c, "different fields are a different action"


async def test_an_extract_that_returns_nothing_counts_as_a_failure():
    """A read that gathered NOTHING used to reset the failure counter, scoring
    it as a success — so no number of fruitless reads could ever trip the cap."""
    page = ScriptedPage([{
        "url": "https://empty.test/", "title": "Empty",
        "elements": [_el(1, name="A"), _el(2, name="B")],
        "total": 2, "text": "nothing structured here at all",
    }])
    # Each extract returns an empty array. Vary the fields so the REPEAT guard
    # is not what stops it — the failure counter must be.
    provider = FakeProvider([
        '{"action":"extract","fields":["name"]}', '[]',
        '{"action":"extract","fields":["price"]}', '[]',
        '{"action":"extract","fields":["rating"]}', '[]',
        '{"action":"extract","fields":["seller"]}', '[]',
    ])
    outcome = await run_browse(FakeSession(page), "extract the items", provider)

    assert outcome.success is False
    assert outcome.extracted == []
    # And the message names the layer that FAILED. It used to say "several actions
    # in a row failed on this page" / "the page didn't respond" — which is how the
    # 2026-07-26 investigation was sent to the browser while the reader was the
    # thing that was blind. A stop must be self-diagnosing.
    assert "read this page" in outcome.error
    assert "element list" in outcome.error
    assert "didn't respond" not in outcome.error


async def test_a_page_that_read_empty_is_not_offered_the_read_again():
    """STEER, don't just count. The repeat guard refuses a duplicate extract — but
    only AFTER it happens, and each attempt is a real LLM call (~15s live). The
    daraz run spent three of them re-reading a page that had already answered
    "nothing". The model cannot choose what it is not shown, so the capability is
    withheld for that page fingerprint."""
    page = ScriptedPage([{
        "url": "https://empty.test/", "title": "Empty",
        "elements": [_el(1, role="link", name="somewhere else", href="/x")],
        "total": 1, "text": "no items at all on this page",
    }])
    provider = PromptRecordingProvider([
        '{"action":"extract","fields":["name"]}', "[]",
        '{"action":"done","reason":"nothing here"}',
    ])

    await run_browse(FakeSession(page), "extract the items", provider)

    # First decision was offered the read; the one after the empty read was not.
    assert '"action": "extract"' in provider.prompts[0]
    assert '"action": "extract"' not in provider.prompts[-1]


async def test_a_CHANGED_page_gets_the_read_back():
    """The withholding is keyed on the page fingerprint, not the run — page 2 of a
    listing is a genuinely new place to read."""
    page = ScriptedPage([
        {"url": "https://shop.test/p1", "title": "Page 1",
         "elements": [_el(1, role="link", name="next page", href="/p2")],
         "total": 1, "text": "page one has nothing structured"},
        {"url": "https://shop.test/p2", "title": "Page 2",
         "elements": [_el(1, role="item", name="Widget One Deluxe Rs. 100"),
                      _el(2, role="item", name="Widget Two Deluxe Rs. 200")],
         "total": 2, "text": "page two"},
    ])
    provider = PromptRecordingProvider([
        '{"action":"extract","fields":["name"]}', "[]",
        '{"action":"click","index":1}',
        '{"action":"extract","fields":["name","price"]}',
        '{"action":"done","reason":"got them"}',
    ])

    outcome = await run_browse(FakeSession(page), "extract the widgets", provider)

    assert '"action": "extract"' in provider.prompts[-1]     # offered again on page 2
    assert len(outcome.extracted) == 2                        # and it worked


async def test_the_same_action_on_a_CHANGED_page_is_not_a_repeat():
    """The guard is scoped to a page FINGERPRINT — extracting page 1 then page 2
    of a listing is normal work, not a spin."""
    pages = ScriptedPage([
        {"url": "https://shop.test/p1", "title": "Page 1",
         "elements": [_el(1, role="link", name="Next", href="/p2")],
         "total": 1, "text": "Phone A $100"},
        {"url": "https://shop.test/p2", "title": "Page 2",
         "elements": [_el(1, role="link", name="Prev", href="/p1")],
         "total": 1, "text": "Phone B $200"},
    ])
    provider = FakeProvider([
        '{"action":"extract","fields":["name","price"]}', '[{"name":"Phone A"}]',
        '{"action":"click","index":1}',
        '{"action":"extract","fields":["name","price"]}', '[{"name":"Phone B"}]',
        '{"action":"done","reason":"both pages"}',
    ])
    outcome = await run_browse(FakeSession(pages), "list every phone", provider)

    assert outcome.success is True, f"a legitimate second extract was refused: {outcome.error}"
    assert len(outcome.extracted) == 2


def test_the_fingerprint_ignores_a_ticking_counter():
    """Prose length is BUCKETED, not exact. A live clock or price ticker would
    otherwise move the hash every step and silently disable the guard on exactly
    the busy commercial pages that need it."""
    def obs_with(text):
        return browser_loop.dom_observe.Observation(
            observation_id="o", url="https://x.test/", title="T", element_total=0,
            elements=[], page_text=text, text_truncated=False,
        )

    base = "x" * 1000
    assert browser_loop._page_fingerprint(obs_with(base)) == \
        browser_loop._page_fingerprint(obs_with(base + "12:04:31"))
    # A real content change still moves it.
    assert browser_loop._page_fingerprint(obs_with(base)) != \
        browser_loop._page_fingerprint(obs_with(base + "y" * 500))


async def test_extract_action_offered_in_read_not_commit_mode():
    """The extract action + its rule appear in a read-mode decision prompt and are
    absent in commit mode (a commit task fills a specific form, it does not
    gather)."""
    page = ScriptedPage([_page(
        [_el(1, role="link", name="A result link"), _el(2, role="button", name="Open")],
        url="https://shop.test/",
    )])
    prov = _RecordingProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(FakeSession(page), "find something on shop.test", prov)
    assert '"action": "extract"' in prov.last_prompt

    page2 = ScriptedPage([_page(
        [_el(1, role="textbox", name="Name"), _el(2, role="button", name="Submit",
             form={"method": "POST", "submit": True, "search": False})],
        url="https://shop.test/contact",
    )])
    prov2 = _RecordingProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(FakeSession(page2), "fill the contact form on shop.test", prov2, commit=True)
    assert '"action": "extract"' not in prov2.last_prompt


# ----------------------------------------------------- drag / range-slider (#2)
# The one gesture the vocabulary lacked and vision could not add (vision LOCATES
# a point for a click; there is no drag). Two forms: slide a range-slider handle
# to a fraction of its track, or drop one element onto another. Read-safe (its
# request is governed by the interceptor like a click), so it needs no approval;
# a drag over a verification widget is refused like any other touch.
def test_parse_action_accepts_drag_forms():
    assert _parse_action('{"action":"drag","index":2,"to_fraction":0.3}') == {
        "action": "drag", "index": 2, "axis": "x", "to_fraction": 0.3
    }
    assert _parse_action('{"action":"drag","index":2,"to_index":5}') == {
        "action": "drag", "index": 2, "axis": "x", "to_index": 5
    }
    assert _parse_action('{"action":"drag","index":2,"to_fraction":0.9,"axis":"y"}') == {
        "action": "drag", "index": 2, "axis": "y", "to_fraction": 0.9
    }
    # to_index wins when both are present (dropping onto an element is explicit).
    assert _parse_action('{"action":"drag","index":2,"to_index":5,"to_fraction":0.3}')["to_index"] == 5
    # a percentage-style fraction normalizes through _as_frac (30 → 0.3).
    assert _parse_action('{"action":"drag","index":1,"to_fraction":30}')["to_fraction"] == 0.3
    # missing target / bad index → None (the loop stops honestly).
    assert _parse_action('{"action":"drag","index":1}') is None
    assert _parse_action('{"action":"drag","to_fraction":0.5}') is None
    assert _parse_action('{"action":"drag","index":"x","to_fraction":0.5}') is None


class _DragRecordingSession(FakeSession):
    def __init__(self, page):
        super().__init__(page)
        self.dragged = []

    async def drag(self, obs, index, *, to_index=None, to_fraction=None, axis="x"):
        self.dragged.append((index, to_index, to_fraction, axis))
        return True, ""


def _drag_obs(*, challenge=None):
    from app.core import dom_observe

    return dom_observe.Observation(
        observation_id="o", url="https://shop.test/catalog", title="",
        elements=[
            dom_observe.Element(index=1, role="slider", name="Min price", rect=(120, 410, 20, 40)),
            dom_observe.Element(index=2, role="listitem", name="Item", rect=(600, 200, 120, 40)),
        ],
        element_total=2, page_text="", text_truncated=False, challenge=challenge,
    )


async def test_act_drag_to_fraction_delegates_to_the_session():
    session = _DragRecordingSession(ScriptedPage([_page([_el(1)])]))
    ok, note = await browser_loop._act(
        session, _drag_obs(), {"action": "drag", "index": 1, "to_fraction": 0.4, "axis": "x"}
    )
    assert ok and note == ""
    assert session.dragged == [(1, None, 0.4, "x")]


async def test_act_drag_to_index_passes_both_endpoints():
    session = _DragRecordingSession(ScriptedPage([_page([_el(1), _el(2)])]))
    ok, _ = await browser_loop._act(
        session, _drag_obs(), {"action": "drag", "index": 1, "to_index": 2, "axis": "x"}
    )
    assert ok and session.dragged == [(1, 2, None, "x")]


async def test_act_refuses_a_drag_over_a_challenge_zone():
    """The NO-TOUCH backstop covers drag too — a handle overlapping a
    verification widget's box is never dragged, and the session is never asked."""
    session = _DragRecordingSession(ScriptedPage([_page([_el(1)])]))
    obs = _drag_obs(challenge=_embedded_challenge())  # zone (100,400,304,78) overlaps [1]
    ok, note = await browser_loop._act(
        session, obs, {"action": "drag", "index": 1, "to_fraction": 0.5}
    )
    assert ok is False and "verification widget" in note
    assert session.dragged == []


async def test_drag_target_index_off_page_is_refused():
    """A drag whose DROP target is not a listed element is refused at decode —
    the loop stops rather than dropping onto whatever happens to be there."""
    page = ScriptedPage([_page([_el(1, role="listitem", name="Item")], url="https://shop.test/")])
    provider = _RecordingProvider(['{"action":"drag","index":1,"to_index":99}'])
    outcome = await run_browse(FakeSession(page), "rearrange the items on shop.test", provider)
    assert outcome.success is False
    assert "safe next action" in (outcome.error or "")


async def test_drag_action_offered_in_read_not_commit_mode():
    page = ScriptedPage([_page([_el(1, role="slider", name="Price")], url="https://shop.test/")])
    prov = _RecordingProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(FakeSession(page), "narrow the results on shop.test", prov)
    assert '"action": "drag"' in prov.last_prompt

    page2 = ScriptedPage([_page(
        [_el(1, role="textbox", name="Name"), _el(2, role="button", name="Submit",
             form={"method": "POST", "submit": True, "search": False})],
        url="https://shop.test/contact",
    )])
    prov2 = _RecordingProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(FakeSession(page2), "fill the contact form on shop.test", prov2, commit=True)
    assert '"action": "drag"' not in prov2.last_prompt


# --------------------------- session.drag geometry (the real mouse-drag method)
class _FakeMouse:
    def __init__(self):
        self.events = []

    async def move(self, x, y, steps=1):
        self.events.append(("move", round(x, 1), round(y, 1), steps))

    async def down(self):
        self.events.append(("down",))

    async def up(self):
        self.events.append(("up",))


class _FakeDragPage:
    def __init__(self):
        self.mouse = _FakeMouse()


class _FakeDragHandle:
    def __init__(self, box, track=None):
        self._box = box
        self._track = track

    async def bounding_box(self):
        return self._box

    async def evaluate(self, js):
        return self._track


class _DragSelf:
    def __init__(self, page):
        self.page = page

    async def settle(self):
        pass


async def test_session_drag_to_fraction_slides_along_the_track(monkeypatch):
    from app.core import dom_observe

    handle = _FakeDragHandle(
        box={"x": 100, "y": 50, "width": 20, "height": 20},
        track={"x": 100, "y": 55, "w": 200, "h": 10},
    )

    async def fake_resolve(page, obs, index):
        return handle

    monkeypatch.setattr(dom_observe, "resolve", fake_resolve)
    page = _FakeDragPage()
    ok, note = await browser_session.BrowserSession.drag(
        _DragSelf(page), _drag_obs(), 1, to_fraction=0.5, axis="x"
    )
    assert ok and note == ""
    # target x = 100 + 0.5*200 = 200; y stays the handle's own center (50 + 10).
    assert ("move", 200.0, 60.0, 12) in page.mouse.events
    assert ("down",) in page.mouse.events and ("up",) in page.mouse.events
    # a real down→move→up ordering
    kinds = [e[0] for e in page.mouse.events]
    assert kinds.index("down") < kinds.index("up")


async def test_session_drag_to_fraction_on_the_y_axis(monkeypatch):
    from app.core import dom_observe

    handle = _FakeDragHandle(
        box={"x": 100, "y": 50, "width": 20, "height": 20},
        track={"x": 90, "y": 0, "w": 40, "h": 400},
    )
    monkeypatch.setattr(dom_observe, "resolve", lambda p, o, i: _await(handle))
    page = _FakeDragPage()
    ok, _ = await browser_session.BrowserSession.drag(
        _DragSelf(page), _drag_obs(), 1, to_fraction=0.25, axis="y"
    )
    assert ok
    # target y = 0 + 0.25*400 = 100; x stays the handle center (100 + 10).
    assert ("move", 110.0, 100.0, 12) in page.mouse.events


async def test_session_drag_without_a_track_fails_honestly(monkeypatch):
    """A drag-only slider whose track can't be located is refused with an honest
    note — never slid to a made-up position."""
    from app.core import dom_observe

    handle = _FakeDragHandle(box={"x": 100, "y": 50, "width": 20, "height": 20}, track=None)
    monkeypatch.setattr(dom_observe, "resolve", lambda p, o, i: _await(handle))
    page = _FakeDragPage()
    ok, note = await browser_session.BrowserSession.drag(
        _DragSelf(page), _drag_obs(), 1, to_fraction=0.5
    )
    assert ok is False and "track" in note
    assert page.mouse.events == []  # nothing was dragged


async def test_session_drag_to_index_uses_both_boxes(monkeypatch):
    from app.core import dom_observe

    src = _FakeDragHandle(box={"x": 100, "y": 100, "width": 20, "height": 20})
    dst = _FakeDragHandle(box={"x": 400, "y": 300, "width": 40, "height": 40})

    async def fake_resolve(page, obs, index):
        return src if index == 1 else dst

    monkeypatch.setattr(dom_observe, "resolve", fake_resolve)
    page = _FakeDragPage()
    ok, _ = await browser_session.BrowserSession.drag(
        _DragSelf(page), _drag_obs(), 1, to_index=2
    )
    assert ok
    # source center (110,110) → drop-target center (420,320).
    assert ("move", 110.0, 110.0, 1) in page.mouse.events
    assert ("move", 420.0, 320.0, 12) in page.mouse.events


def _await(value):
    async def _coro(*a, **k):
        return value
    return _coro()


# ------------------------------- truncated extraction salvage (2026-07-26)
#
# THE LIVE DEFECT. daraz.pk's real results page carries ~13 products across 4000
# chars of prose. The extraction reply did not fit in max_tokens, so it was cut
# before its closing bracket — and the parser, finding no `]`, threw away the
# ENTIRE array and reported "no structured data was found on the page" for a page
# that plainly had it. Measured end to end: 0 records before, 10 after.
def test_close_truncated_array_keeps_every_complete_object():
    f = browser_loop._close_truncated_array
    assert f('[{"a":1},{"a":2},{"a":') == '[{"a":1},{"a":2}]'
    assert f('[{"a":1}') == '[{"a":1}]'


def test_close_truncated_array_is_string_aware():
    """A brace INSIDE a value must never be read as the end of an object, or the
    salvage would happily produce invalid JSON from valid data."""
    f = browser_loop._close_truncated_array
    assert f('[{"n":"a}b"},{"n":"c') == '[{"n":"a}b"}]'
    assert f('[{"n":"x\\"y"},{') == '[{"n":"x\\"y"}]'


def test_close_truncated_array_gives_up_when_nothing_completed():
    """Nothing finished ⇒ nothing to salvage. Returning a bare '[]' would dress a
    total failure up as an empty page."""
    f = browser_loop._close_truncated_array
    assert f('[{') is None
    assert f('[') is None


def test_the_salvaged_prefix_is_valid_json():
    import json

    salvaged = browser_loop._close_truncated_array('[{"a":1},{"b":"}"},{"c":')
    assert json.loads(salvaged) == [{"a": 1}, {"b": "}"}]


async def test_a_truncated_extraction_reply_still_yields_records():
    """End to end through _extract_data: the reply is cut mid-array, and the
    records that did arrive must survive."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://shop.test/x", title="Shop",
        element_total=20, elements=[], text_truncated=True,
        page_text="Phone A Rs. 100 Phone B Rs. 200 Phone C Rs. 300",
    )
    cut = '[{"name":"Phone A","price":"Rs. 100"},{"name":"Phone B","price":"Rs. 200"},{"name":"Pho'
    provider = FakeProvider([cut])

    records, note = await browser_loop._extract_data(obs, ["name", "price"], provider)

    assert len(records) == 2, f"the salvage dropped everything: {records}"
    assert records[0]["name"] == "Phone A"
    assert records[1]["price"] == "Rs. 200"
    assert note == ""


# ===================== an optional sign-in offer is decided ONCE PER SITE
# 2026-07-26: auth_seen holds URLs and the gate tested `obs.url not in auth_seen`,
# but a storefront offers an account in its header on EVERY page — so answering
# "continue as guest" on /search bought nothing the moment the loop opened
# /products/... Live that cost two identical interrupts on one add-to-cart.
def test_auth_offer_is_not_re_asked_on_another_page_of_the_same_site():
    from app.browser.loop import _auth_site_decided

    seen = {"https://shop.test/search?q=perfume"}
    assert _auth_site_decided("https://shop.test/products/janan-sport", seen) is True
    assert _auth_site_decided("https://www.shop.test/cart", seen) is True   # subdomain


def test_auth_offer_is_still_asked_for_a_different_site():
    from app.browser.loop import _auth_site_decided

    seen = {"https://shop.test/search"}
    assert _auth_site_decided("https://other.test/apply", seen) is False
    assert _auth_site_decided("https://shop.test.evil.test/x", seen) is False


def test_auth_site_decided_falls_back_to_exact_membership_on_junk():
    from app.browser.loop import _auth_site_decided

    assert _auth_site_decided("not-a-url", {"not-a-url"}) is True
    assert _auth_site_decided("not-a-url", {"https://shop.test/x"}) is False
    assert _auth_site_decided("https://shop.test/x", set()) is False


# ------------------------------------- a navigation goal is not a search goal (2026-08-01)
# THE INCIDENT. The user said "open youtube". The planner authored the correct
# tool call — browse(goal="Open the YouTube homepage", start_url=youtube.com,
# keep_open=True) — and the loop, with ZERO LLM calls, typed "the YouTube
# homepage" into YouTube's own search box, then clicked a result. keep_open
# handed that video to the playback window, so the user watched a video start
# playing from a goal that only asked to open a page.
#
# Both bad moves were code fast paths. The trace:
#   step 0 fast-path {"action":"type","index":4,"text":"the YouTube homepage",...}
#   step 1 fast-path {"action":"click","index":23}   on /results?search_query=...
def _dest_obs(elements, url, title="T"):
    return browser_loop.dom_observe.Observation(
        observation_id="o", url=url, title=title, element_total=len(elements),
        elements=elements, page_text="", text_truncated=False,
    )


def _searchbox(index=4, name="Search"):
    return browser_loop.dom_observe.Element(index=index, role="searchbox", name=name)


def test_the_incident_a_destination_goal_never_fast_paths_into_a_search():
    """Frozen: the goal's own words must not become a query on the site it names."""
    obs = _dest_obs([_searchbox()], "https://www.youtube.com/", "YouTube")
    assert _fast_path_action("Open the YouTube homepage", obs) is None


@pytest.mark.parametrize(
    "goal, url",
    [
        # Every phrasing measured against the pre-fix code, each of which searched
        # the destination site for its own name.
        ("Open the YouTube homepage", "https://www.youtube.com/"),
        ("open youtube", "https://www.youtube.com/"),
        ("open google", "https://www.google.com/"),
        ("pull up amazon", "https://www.amazon.com/"),
        ("visit amazon", "https://www.amazon.com/"),
        # The bare navigation verbs the verb-chain never stripped, so the WHOLE
        # sentence used to be typed into the box verbatim.
        ("go to youtube.com", "https://www.youtube.com/"),
        ("navigate to amazon", "https://www.amazon.com/"),
        ("head to outfitters", "https://outfitters.com.pk/"),
        # A goal naming nothing but the place.
        ("open the homepage", "https://www.youtube.com/"),
        ("open the site", "https://anikoto.cz/"),
        # A subdomain is the same destination.
        ("open youtube", "https://m.youtube.com/"),
    ],
)
def test_navigation_goals_do_not_search_the_site_for_its_own_name(goal, url):
    obs = _dest_obs([_searchbox()], url)
    assert _fast_path_action(goal, obs) is None


@pytest.mark.parametrize(
    "goal, url, expected",
    [
        # A real query names something the site is NOT — untouched by the guard.
        ("search for cats on youtube", "https://www.youtube.com/", "cats"),
        ("play jane by the long faces on youtube", "https://www.youtube.com/",
         "jane by the long faces"),
        ("play lofi hip hop on youtube", "https://www.youtube.com/", "lofi hip hop"),
        # `open` is still a SEARCH verb when a title follows it — which is why
        # deleting it from the verb list was the wrong fix.
        ("open the dangers in my heart", "https://anikoto.cz/",
         "the dangers in my heart"),
        ("play ep 4 of the dangers in my heart season 2 on anikoto.cz",
         "https://anikoto.cz/", "the dangers in my heart"),
        # The site's name plus a real term is a query, not a destination.
        ("open amazon deals", "https://www.amazon.com/", "amazon deals"),
    ],
)
def test_real_search_goals_still_fast_path(goal, url, expected):
    obs = _dest_obs([_searchbox()], url)
    assert _fast_path_action(goal, obs) == {
        "action": "type", "index": 4, "text": expected, "submit": True,
    }


def test_the_guard_reads_the_final_term_not_the_site_adapted_one():
    """_fast_path_action falls back to _extract_search_term whenever `query` is
    None, so gating inside _search_query_for would be routed around. The gate has
    to sit on the term that is about to be typed."""
    obs = _dest_obs([_searchbox()], "https://www.youtube.com/")
    # An explicit site-adapted query naming only the destination is refused too.
    assert _fast_path_action("whatever", obs, query="the youtube homepage") is None
    # ...and a real one still fires.
    assert _fast_path_action("whatever", obs, query="lofi hip hop") == {
        "action": "type", "index": 4, "text": "lofi hip hop", "submit": True,
    }


def test_top_result_never_clicks_on_a_bare_site_name_overlap():
    """The second half of the incident. On a YouTube results page EVERY row
    mentions YouTube, so a 'query' of {youtube, homepage} scored 1 against an
    arbitrary video and clicked it. The old guard was `best_score == 0 -> defer`."""
    els = [
        browser_loop.dom_observe.Element(
            index=23, role="link", name="Some Unrelated Song - YouTube Music",
            href="/watch?v=abc123",
        ),
    ]
    obs = _dest_obs(
        els, "https://www.youtube.com/results?search_query=the+YouTube+homepage"
    )
    assert browser_loop._top_result_action(obs, "Open the YouTube homepage") is None


def test_top_result_still_picks_a_genuine_title_match():
    """Regression: the humrahi/top-result path is untouched by the guard."""
    els = [
        browser_loop.dom_observe.Element(
            index=7, role="link", name="Shorts", href="/watch?v=zzz"
        ),
        browser_loop.dom_observe.Element(
            index=11, role="link", name="Jane - The Long Faces (Official)",
            href="/watch?v=HydkjjDNTmY",
        ),
    ]
    obs = _dest_obs(els, "https://www.youtube.com/results?search_query=jane")
    assert browser_loop._top_result_action(
        obs, "play jane by the long faces on youtube"
    ) == {"action": "click", "index": 11}


# ------------------------------------------------------- the arrival terminator
def test_destination_reached_is_true_only_when_the_goal_is_only_a_place():
    from app.browser.loop import _destination_reached

    yt = _dest_obs([], "https://www.youtube.com/", "YouTube")
    assert _destination_reached("Open the YouTube homepage", yt) is True
    assert _destination_reached("open youtube", yt) is True
    assert _destination_reached("go to youtube.com", yt) is True
    # A goal with work left in it is NOT arrival.
    assert _destination_reached("open youtube and find the video about rust", yt) is False
    assert _destination_reached("play jane by the long faces on youtube", yt) is False
    # The destination named is not where we are.
    assert _destination_reached("open amazon", yt) is False
    # No host to compare against -> never call it done.
    assert _destination_reached("open youtube", _dest_obs([], "")) is False


def test_destination_reached_declines_a_goal_it_cannot_reduce():
    """A compose goal and a multi-clause instruction both yield no term. Silence
    there must mean 'not arrival', never 'nothing left to do'."""
    from app.browser.loop import _destination_reached

    yt = _dest_obs([], "https://www.youtube.com/", "YouTube")
    assert _destination_reached(
        "open the messages area and type 'hi' but do not send it", yt
    ) is False
    assert _destination_reached(
        "open youtube, then find the trailer that was posted today", yt
    ) is False


async def test_opening_a_site_finishes_immediately_and_costs_no_llm_call():
    """End-to-end: the incident goal on the incident page. The run must FINISH on
    step 0 — no search typed, no result clicked, no model call."""
    page = ScriptedPage([
        _page([_el(4, "searchbox", "Search")], url="https://www.youtube.com/",
              title="YouTube"),
    ])
    session = FakeSession(page)
    provider = FakeProvider([])  # any call would fall through to a canned "done"

    outcome = await run_browse(
        session, "Open the YouTube homepage", provider, keep_open=True
    )

    assert outcome.success
    assert provider.calls == 0, "arrival is decided in code — no model call"
    assert page.acted == [], "nothing was typed and nothing was clicked"
    # The tool reads this to skip the media hand-off (which would lift Rule 1 and
    # press .play() on the homepage's preview videos).
    assert outcome.destination_only is True


async def test_a_media_goal_is_not_marked_destination_only():
    """Regression: the flag gates the media hand-off, so a play goal must never
    set it — a false positive here would stop videos playing."""
    page = ScriptedPage([
        _page([_el(4, "searchbox", "Search")], url="https://www.youtube.com/",
              title="YouTube"),
        _page([], url="https://www.youtube.com/watch?v=abc", title="Jane"),
    ])
    outcome = await run_browse(
        FakeSession(page), "play jane by the long faces on youtube",
        FakeProvider([]), keep_open=True,
    )
    assert outcome.success
    assert outcome.destination_only is False


# ===========================================================================
# DID THE GOAL ASK FOR PLAYBACK? (2026-08-01, the add-to-cart incident)
# ===========================================================================
# The keep_open hand-off lifts Rule 1 and presses .play(), and it was gated
# NEGATIVELY — `keep_open and not destination_only`. A negative test over an
# LLM-authored goal string fails OPEN, and it did, live, the same evening the
# destination_only gate shipped.


@pytest.mark.parametrize(
    "goal",
    [
        "play jane by the long faces on youtube",
        "Find and play the latest episode of One Piece",
        "go to anikoto.cz and find and play the dangers in my heart",
        "watch the latest episode of one piece",
        "listen to lofi on spotify",
        "put on some jazz",
        "stream the match on espn",
    ],
)
def test_a_play_goal_asks_for_playback(goal):
    assert browser_loop.goal_wants_playback(goal) is True


@pytest.mark.parametrize(
    "goal",
    [
        # The incident: the planner's own wording, which defeats the
        # destination reduction and so opened the negative gate.
        "Open the junaidjamshed.com homepage so it is visible in the browser.",
        "Open the YouTube homepage",
        "open junaidjamshed.com",
        # THE VERB MUST LEAD. "watch" is a noun here, and both of these are
        # shopping goals on sites that sell them.
        "find a watch under 200 dollars on amazon",
        "buy a watch on amazon",
        "Find the product 'Janan Sports 100ml' on junaidjamshed.com and add it to the cart.",
        # Word-boundary neighbours that must not read as the verb.
        "search for a playstation 5 on daraz",
        "open my youtube playlist",
        "find the video player settings",
    ],
)
def test_a_goal_that_never_asked_for_playback_does_not_get_it(goal):
    assert browser_loop.goal_wants_playback(goal) is False


# ------------------------------------ a brand that concatenates in its domain
# THE INCIDENT (2026-08-02). "openjunetjamshed.com" was corrected to
# junaidjamshed.com, the page loaded perfectly (121 elements) — and the loop
# typed the goal's own words, "the Junaid Jamshed website", into the storefront's
# SEARCH BOX. That is a world-acting gesture, so the run paused for approval on a
# page that was already exactly where the goal asked to be.
#
# The subset test is right when the brand is one word and blind when it is two:
#     "the Junaid Jamshed website" -> {junaid, jamshed}
#     www.junaidjamshed.com        -> {junaidjamshed}
# so it read "there is something to look for" and the fast path obliged.
def test_the_incident_a_two_word_brand_still_names_only_its_own_site():
    from app.browser.loop import _destination_reached, _names_only_the_destination

    jj = _dest_obs([], "https://www.junaidjamshed.com/", "J. Junaid Jamshed")
    assert _names_only_the_destination("the Junaid Jamshed website", jj.url) is True
    assert _destination_reached("Open the Junaid Jamshed website", jj) is True


@pytest.mark.parametrize(
    "term,url,expected",
    [
        # Two words that spell one label — the incident's shape, and two others.
        ("the Junaid Jamshed website", "https://www.junaidjamshed.com/", True),
        ("the book depository website", "https://www.bookdepository.com/", True),
        ("the stack overflow site", "https://stackoverflow.com/", True),
        # One word: the subset test already decided these and still does.
        # (These are TERMS — _extract_search_term has already taken the verb off;
        # _destination_reached is what feeds this the goal.)
        ("the YouTube homepage", "https://www.youtube.com/", True),
        ("youtube", "https://www.youtube.com/", True),
        # ⚠️ THE REGRESSIONS. A real query must stay a real query.
        ("junaid jamshed perfume", "https://www.junaidjamshed.com/", False),
        ("jane by the long faces", "https://www.youtube.com/", False),
        ("cats", "https://www.youtube.com/", False),
        ("the dangers in my heart", "https://www.youtube.com/", False),
        # The brand names a site we are NOT on.
        ("the Junaid Jamshed website", "https://www.daraz.pk/", False),
        # EQUALITY, never containment: one token that happens to sit inside the
        # label is not the label, or "open jam" would arrive at junaidjamshed.
        ("jam", "https://www.junaidjamshed.com/", False),
        ("shed", "https://www.junaidjamshed.com/", False),
        # A documented limit, pinned so a change to it is deliberate: the tokens
        # are compared against the host we are ON, and "gmail" is not how
        # mail.google.com spells itself. Costs one model call, the safe direction.
        ("my gmail inbox", "https://mail.google.com/", False),
    ],
)
def test_the_destination_matrix(term, url, expected):
    from app.browser.loop import _names_only_the_destination

    assert _names_only_the_destination(term, url) is expected


def test_ordered_query_tokens_agrees_with_the_set_it_orders():
    """The two views of the same words must never disagree — one decides
    membership, the other decides spelling."""
    from app.browser.loop import _ordered_query_tokens, _query_tokens

    for term in [
        "the Junaid Jamshed website",
        "junaid jamshed perfume",
        "jane by the long faces",
        "the YouTube homepage",
        "",
    ]:
        assert set(_ordered_query_tokens(term)) == _query_tokens(term)
        assert _ordered_query_tokens(term) == list(dict.fromkeys(_ordered_query_tokens(term)))

    # And it keeps the order the domain spells them in.
    assert _ordered_query_tokens("the Junaid Jamshed website") == ["junaid", "jamshed"]


async def test_the_incident_a_two_word_brand_site_finishes_without_acting():
    """THE 2026-08-02 INCIDENT, END TO END, on the incident's own page. Before
    the concatenated-brand test, this run typed "the Junaid Jamshed website" into
    the storefront's search box and submitted it — a world-acting gesture, so it
    paused for approval on a page that already WAS the goal, and that pause then
    closed the tab. The run must now FINISH on step 0."""
    page = ScriptedPage([
        _page(
            [_el(9, "searchbox", "Search for products")],
            url="https://www.junaidjamshed.com/",
            title="J. Junaid Jamshed Official Website",
        ),
    ])
    session = FakeSession(page)
    provider = FakeProvider([])

    outcome = await run_browse(
        session, "Open the Junaid Jamshed website", provider, keep_open=True
    )

    assert outcome.success
    assert provider.calls == 0, "arrival is decided in code — no model call"
    assert page.acted == [], "nothing was typed into the storefront's search box"
    assert outcome.action_approval_required is False, (
        "no gesture, so no approval pause — the second 'options' card the user saw"
    )
    # And the storefront is never handed to the media path with Rule 1 lifted.
    assert outcome.destination_only is True
