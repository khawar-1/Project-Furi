"""
Phase 14 Part 2 — the browse loop: bounded, terminal, non-spinning, and cheap.

These pin the properties that make an LLM-driven browser safe to run in the
background: it stops (action cap), it does not spin on a dead button (dedupe —
the ended-stream bug), the fast path costs no model call, a hallucinated index
cannot be acted on, and the media registry keeps exactly one window playing.
"""
import re

import pytest

from app.agents import browser_loop
from app.agents.browser_loop import (
    BrowseOutcome,
    _extract_search_term,
    _fast_path_action,
    _parse_action,
    run_browse,
)
from app.core import browser_session
from app.providers.base import LLMResponse


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

    async def evaluate(self, js, obs_id):
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


def _el(index, role="link", name="x", value="", href=""):
    return {"index": index, "role": role, "name": name, "value": value, "href": href}


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
    assert _parse_action('{"action":"scroll","index":1}') is None
    assert _parse_action('{"action":"click"}') is None  # no index
    # tolerates a code fence and surrounding prose
    assert _parse_action('```json\n{"action":"done","reason":"y"}\n```') == {"action": "done", "reason": "y"}


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
