"""
Phase 14 Part 2 — the browse loop: bounded, terminal, non-spinning, and cheap.

These pin the properties that make an LLM-driven browser safe to run in the
background: it stops (action cap), it does not spin on a dead button (dedupe —
the ended-stream bug), the fast path costs no model call, a hallucinated index
cannot be acted on, and the media registry keeps exactly one window playing.
"""
import asyncio
import re

import pytest

from app.agents import browser_loop
from app.agents.browser_loop import (
    BrowseOutcome,
    _extract_search_term,
    _fast_path_action,
    _parse_action,
    detect_auth_offer,
    run_browse,
)
from app.core import browser_session
from app.providers.base import LLMResponse


def _auth_obs(url, *elements):
    els = [
        browser_loop.dom_observe.Element(index=i + 1, role=r, name=n)
        for i, (r, n) in enumerate(elements)
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

    def record(self, index, kind, value):
        self.acted.append((self.i, index, kind, value))
        # A click or Enter is a navigation — advance to the next scripted page.
        if kind in ("click", "press") and self.i < len(self.payloads) - 1:
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
    applied to browsing)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        content = self.responses.pop(0) if self.responses else '{"action":"done","reason":"end"}'
        return LLMResponse(content=content, model="fake", provider="fake")


def _el(index, role="link", name="x", value="", href="", form=None):
    item = {"index": index, "role": role, "name": name, "value": value, "href": href}
    if form is not None:
        item["form"] = form
    return item


def _page(elements, url="https://site.test/", title="T"):
    return {"url": url, "title": title, "elements": list(elements), "total": len(elements), "text": ""}


# ---------------------------------------------------------------- fast path
def test_extract_search_term():
    assert _extract_search_term("search 'jane by the long faces' and play it") == "jane by the long faces"
    assert _extract_search_term("search jane by the long faces on youtube") == "jane by the long faces"
    assert _extract_search_term("play lofi hip hop on youtube") == "lofi hip hop"
    assert _extract_search_term("") is None


def test_fast_path_fires_only_with_a_single_search_box():
    obs_one = browser_loop.dom_observe.Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[browser_loop.dom_observe.Element(index=1, role="searchbox", name="Search")],
        page_text="", text_truncated=False,
    )
    action = _fast_path_action("play lofi on youtube", obs_one)
    assert action == {"action": "type", "index": 1, "text": "lofi", "submit": True}

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


async def test_the_fast_path_search_costs_no_llm_call():
    """THE THESIS, the evidence_resolver call-count test restated: search-and-go
    with one search box does the search in CODE. Home page + results page + ONE
    'done' response ⇒ exactly ONE provider call. Without the fast path the search
    itself would have cost a second call."""
    home = _page([_el(1, role="searchbox", name="Search")], url="https://youtube.com/")
    results = _page([_el(1, role="link", name="lofi hip hop", href="/watch?v=a")],
                    url="https://youtube.com/results")
    session = FakeSession(ScriptedPage([home, results]))
    provider = FakeProvider(['{"action":"done","reason":"the video is playing"}'])

    outcome = await run_browse(session, "search 'lofi' on youtube and play it", provider)

    assert outcome.success is True
    assert provider.calls == 1  # the search was free; only the 'done' cost a call
    # The fast path actually filled and submitted the box.
    kinds = [(a[2], a[3]) for a in session.page.acted]
    assert ("fill", "lofi") in kinds
    assert ("press", "Enter") in kinds


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
    provider = FakeProvider(['{"action":"click","index":1}', '{"action":"done","reason":"playing"}'])

    outcome = await run_browse(session, "play the song", provider)

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
    assert provider.calls <= browser_loop._MAX_REPEAT + 1


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
    provider = FakeProvider(["not json at all"])
    outcome = await run_browse(session, "do something", provider)
    assert outcome.success is False
    assert provider.calls == 1


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
    assert _parse_action('{"action":"drag","index":1}') is None   # unknown verb
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
    def __init__(self, responses):
        super().__init__(responses)
        self.prompts: list[str] = []

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.prompts.append(messages[0].content)
        return await super().chat(messages, temperature, max_tokens)


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
    assert "failed" in outcome.error
    assert page.acted == []  # nothing was ever touched


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
# With the network open to page traffic, what keeps the agent from submitting
# is the refusal in _act: a click on a form's submit control, or Enter in its
# fields, dies in code. Search-shaped and GET forms are exempt (submitting a
# search IS reading; a GET submit is an allowlist-governed navigation).
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
    assert "submit" in note and "commit" in note
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
    assert "Enter" in note and "submit" in note
    assert page.acted == []


async def test_act_allows_submitting_a_search_form():
    """Submitting a search is reading — the exemption that keeps the fast path
    (fill + Enter on a search box) and ordinary site search working."""
    page = ScriptedPage([_page([_el(1, role="searchbox", name="Search")])])
    session = FakeSession(page)
    obs = _form_obs(
        form_member=True, form_submit=False, form_method="POST", form_search=True
    )
    ok, _ = await browser_loop._act(
        session, obs, {"action": "type", "index": 1, "text": "jane", "submit": True}
    )
    assert ok is True


async def test_act_allows_a_get_form_submit():
    """A GET form submit is a navigation with query params — the allowlist
    already governs it, so the gate stands down."""
    page = ScriptedPage([_page([_el(1, role="button", name="Filter")])])
    session = FakeSession(page)
    obs = _form_obs(form_member=True, form_submit=True, form_method="GET")
    ok, _ = await browser_loop._act(session, obs, {"action": "click", "index": 1})
    assert ok is True


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

    outcome = await run_browse(session, "just look", provider, vision=vision)

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

    outcome = await run_browse(session, "click the icon", provider, vision=vision)

    assert outcome.success is True
    assert vision.calls == 2
    # The vision-located click hit element 1 THROUGH the index contract (the fake
    # handle records a click with the real index — a point never clicks directly).
    assert any(k == "click" and idx == 1 for (_i, idx, k, v) in session.page.acted)


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

    outcome = await run_browse(session, "just look", provider, vision=vision)

    assert outcome.success is True
    assert vision.calls == 1               # tried first...
    assert outcome.vision_calls == 1
    assert provider.calls == 1             # ...text provider decided the step


async def test_a_vision_point_over_no_element_never_fabricates_a_click():
    """A point over nothing clickable maps to NO element → the vision decision
    is unusable and the step falls to the text provider; with that also junk,
    the loop stops honestly. Nothing is ever acted on."""
    session = FakeSession(VisionPage([_vpage([_vel(1, (400, 400, 100, 100), name="")])]))
    provider = FakeProvider(["not json"])
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
