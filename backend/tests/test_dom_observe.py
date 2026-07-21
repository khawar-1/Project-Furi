"""
Phase 14 Part 1 — DOM observation: the index contract and the render budget.
"""
import pytest

from app.agents.rendering import _STEP_RESULT_CAPS
from app.core import dom_observe
from app.core.dom_observe import (
    _ELEMENT_BUDGET,
    _PAGE_TEXT_BUDGET,
    Element,
    Observation,
    StaleObservation,
    observe,
    render,
    resolve,
    summarize,
)


class FakePage:
    """Stands in for a live page. `payload` is what the injected JS would have
    returned; `elements_present` is what query_selector can still find."""

    def __init__(self, payload=None, present=()):
        self.url = (payload or {}).get("url", "https://example.com")
        self._payload = payload or {}
        self._present = set(present)
        self.queries = []

    async def evaluate(self, js, obs_id):
        self._payload.setdefault("url", self.url)
        self._payload["_obs"] = obs_id
        return self._payload

    async def query_selector(self, selector):
        self.queries.append(selector)
        return "handle" if selector in self._present else None


def _payload(elements=(), text="", title="Example", url="https://example.com", total=None):
    return {
        "url": url,
        "title": title,
        "elements": list(elements),
        "total": len(elements) if total is None else total,
        "text": text,
    }


def _element(index, role="link", name="x", value="", href=""):
    return {"index": index, "role": role, "name": name, "value": value, "href": href}


# -------------------------------------------------------- the index contract
async def test_observe_surfaces_the_challenge_probe():
    """15.4: the in-page CAPTCHA probe rides on the same observe() evaluate as the
    elements, so a challenge is visible to the loop without a second round-trip.
    A blocking probe is carried through; a normal page's None stays None."""
    page = FakePage({**_payload([_element(1)]), "challenge": {"kind": "hCaptcha", "blocking": True}})
    obs = await observe(page)
    assert obs.challenge == {"kind": "hCaptcha", "blocking": True}

    plain = await observe(FakePage(_payload([_element(1)])))
    assert plain.challenge is None


async def test_an_index_resolves_only_within_its_own_observation():
    """Both the observation id AND the index must match. This is the whole
    contract: it is what stops a re-rendered page's element 3 being clicked in
    place of the element 3 the model actually chose."""
    page = FakePage(_payload([_element(1), _element(2), _element(3)]))
    obs = await observe(page)
    page._present = {f'[data-jarvis-obs="{obs.observation_id}"][data-jarvis-idx="3"]'}

    assert await resolve(page, obs, 3) == "handle"
    assert f'[data-jarvis-obs="{obs.observation_id}"]' in page.queries[-1]


async def test_a_stale_index_raises_instead_of_clicking_the_wrong_element():
    """A navigation destroys the document and every stamped attribute with it,
    so the selector matches NOTHING. Structural invalidation — a property of how
    documents work, not a check someone must remember to write."""
    page = FakePage(_payload([_element(1), _element(2), _element(3)]))
    obs = await observe(page)
    page._present = set()  # navigated: the old document is gone

    with pytest.raises(StaleObservation, match="no longer on the page"):
        await resolve(page, obs, 3)


async def test_two_observations_get_different_ids():
    page = FakePage(_payload([_element(1)]))
    first = await observe(page)
    second = await observe(page)
    assert first.observation_id != second.observation_id


async def test_an_index_from_a_previous_observation_does_not_resolve():
    """The concrete near-miss: same page, same index, stale observation."""
    page = FakePage(_payload([_element(1)]))
    old = await observe(page)
    new = await observe(page)
    page._present = {f'[data-jarvis-obs="{new.observation_id}"][data-jarvis-idx="1"]'}

    assert await resolve(page, new, 1) == "handle"
    with pytest.raises(StaleObservation):
        await resolve(page, old, 1)


# ---------------------------------------------------------------- the budget
async def test_elements_are_rendered_before_page_text():
    """The _fmt_search_files aggregate-line lesson: the actionable half is
    rendered first so a per-step clip can never eat it."""
    page = FakePage(_payload([_element(1, name="Search")], text="lorem ipsum"))
    out = render(await observe(page))
    assert out.index("ELEMENTS") < out.index("PAGE TEXT")


async def test_a_chatty_page_cannot_crowd_out_the_elements():
    """THE 5-WIDE TRAP, restated. The budgets are a HARD SPLIT, not a shared
    pool — 200k of article prose must not cost one element its place."""
    page = FakePage(
        _payload(
            [_element(i, name=f"Button {i}") for i in range(1, 21)],
            text="blah " * 50_000,
        )
    )
    out = render(await observe(page))
    for i in range(1, 21):
        assert f"Button {i}" in out
    assert "… (truncated)" in out


async def test_page_text_is_capped():
    page = FakePage(_payload(text="x" * (_PAGE_TEXT_BUDGET * 3)))
    obs = await observe(page)
    assert len(obs.page_text) == _PAGE_TEXT_BUDGET
    assert obs.text_truncated is True


async def test_a_huge_element_list_is_cut_and_the_cut_is_marked():
    page = FakePage(_payload([_element(i, name=f"Link number {i}") for i in range(1, 500)]))
    out = render(await observe(page))
    assert "more elements not shown" in out
    assert len(out) < _ELEMENT_BUDGET + _PAGE_TEXT_BUDGET + 500


async def test_a_cut_marker_states_the_fact_and_stops_talking():
    """The _missing_target leak: a marker that also named a remedy got copied
    verbatim into user-facing prose a dozen times. Say what was cut. Stop."""
    page = FakePage(
        _payload([_element(i, name=f"Element number {i} on this page") for i in range(1, 400)])
    )
    out = render(await observe(page))
    marker = [line for line in out.splitlines() if "more elements" in line][0]
    for leak in ("browse_page", "read_webpage", "scroll", "ask", "try"):
        assert leak not in marker.lower()


async def test_skip_elements_slides_the_window_and_keeps_indexes():
    """Element paging (2026-07-19, the WWR window trap): render(skip_elements=N)
    shows the NEXT budget-worth of elements, with their ORIGINAL indexes — an
    element deep in a long page becomes visible without re-stamping anything."""
    from app.core.dom_observe import visible_span

    obs = await observe(
        FakePage(_payload([_element(i, name=f"Link number {i}") for i in range(1, 500)]))
    )
    start, end = visible_span(obs)
    assert start == 0 and 0 < end < obs.element_total

    second = render(obs, skip_elements=end)
    assert f"[{end + 1}]" in second          # the window starts where the first ended
    assert "[1] " not in second              # the first window's elements are gone
    assert f"{end + 1}–" in second           # the header names the span

    start2, end2 = visible_span(obs, end)
    assert start2 == end and end2 > end      # the span helper agrees with render


async def test_skip_elements_past_the_end_renders_an_empty_window():
    obs = await observe(FakePage(_payload([_element(1, name="only")])))
    out = render(obs, skip_elements=50)
    assert "URL:" in out                     # still a valid rendering, no crash


def test_the_render_cap_cannot_starve_an_observation():
    """The 5-wide invariant, pinned: moving a budget without moving the cap
    silently clips the element list's tail. Fail loudly instead."""
    head_and_labels = 500
    worst_case = _ELEMENT_BUDGET + _PAGE_TEXT_BUDGET + head_and_labels
    assert _STEP_RESULT_CAPS["browse_page"] >= worst_case


# ---------------------------------------------------------------- rendering
async def test_the_shape_of_a_rendered_observation():
    page = FakePage(
        _payload(
            [
                _element(1, role="searchbox", name="Search", value="lofi"),
                _element(2, role="link", name="lofi hip hop radio", href="/watch?v=abc"),
            ],
            text="Some page prose.",
            title="lofi - YouTube",
            url="https://www.youtube.com/results?search_query=lofi",
        )
    )
    out = render(await observe(page))
    assert "URL: https://www.youtube.com/results?search_query=lofi" in out
    assert "TITLE: lofi - YouTube" in out
    assert '[1] searchbox "Search" = \'lofi\'' in out
    assert '[2] link "lofi hip hop radio" → /watch?v=abc' in out
    assert "Some page prose." in out


async def test_a_page_with_nothing_actionable_says_so():
    """Silence reads as a bug. An empty element list is an outcome."""
    page = FakePage(_payload([], text="Just an article."))
    out = render(await observe(page))
    assert "nothing on this page can be clicked" in out


async def test_summarize_carries_both_the_prose_and_the_counts():
    page = FakePage(_payload([_element(1)], text="hi", total=42))
    out = summarize(await observe(page))
    assert out["element_count"] == 42
    assert out["elements_shown"] == 1
    assert "ELEMENTS" in out["rendered"]
    assert out["url"] == "https://example.com"


async def test_observe_rejects_a_nonsense_payload():
    page = FakePage()

    async def _garbage(js, obs):
        return "not a dict"

    page.evaluate = _garbage
    with pytest.raises(RuntimeError, match="expected an object"):
        await observe(page)


async def test_long_names_are_clipped_defensively():
    """The JS clips, but the JS runs in a hostile page — never trust its output
    for length any more than for content."""
    page = FakePage(_payload([_element(1, name="n" * 5000, href="h" * 5000)]))
    obs = await observe(page)
    assert len(obs.elements[0].name) <= 120
    assert len(obs.elements[0].href) <= 100


def test_a_password_role_element_renders_without_its_value():
    """Jarvis does not handle credentials. The JS never reads a password value;
    this pins the render side so a future change cannot leak one into a prompt."""
    obs = Observation(
        observation_id="x",
        url="https://example.com",
        title="",
        elements=[Element(index=1, role="password", name="Password", value="")],
        element_total=1,
        page_text="",
        text_truncated=False,
    )
    out = render(obs)
    assert '[1] password "Password"' in out
    assert "=" not in out.split("[1]")[1].split("\n")[0]


def test_the_extract_js_never_reads_a_password_value():
    """Read the source, because the JS runs in the page and no Python test can
    observe what it chose not to collect."""
    assert "role === 'password'" in dom_observe._EXTRACT_JS


# ---------------------------------------------- challenge zones (2026-07-19)
# The structural half of "Jarvis never touches a CAPTCHA": the probe reports
# the widget boxes as `zones`, the element walk skips anything overlapping one,
# and these Python helpers back the act-time and vision-path vetoes.
def _challenge_obs(challenge):
    return Observation(
        observation_id="o", url="https://site.test/form", title="",
        elements=[], element_total=0, page_text="", text_truncated=False,
        challenge=challenge,
    )


def test_challenge_zone_rects_parse_defensively():
    obs = _challenge_obs({
        "kind": "reCAPTCHA", "mode": "embedded",
        "zones": [
            {"x": 10, "y": 20, "w": 304, "h": 78},
            {"x": "bad", "y": {}, "w": 1, "h": 1},      # unparseable → dropped
            {"x": 5, "y": 5, "w": 0, "h": 50},          # zero-width → dropped
            "not-a-dict",                                # wrong shape → dropped
        ],
    })
    assert obs.challenge_zone_rects() == [(10.0, 20.0, 304.0, 78.0)]
    assert _challenge_obs(None).challenge_zone_rects() == []
    assert _challenge_obs({"kind": "x"}).challenge_zone_rects() == []


def test_challenge_mode_defaults_conservative():
    """A challenge dict WITHOUT a mode (an old-shaped fake) reads as
    interstitial — a stop is always safe; continuing on an unknown might not be.
    No challenge at all reads as '' (nothing to decide)."""
    assert _challenge_obs({"kind": "x", "blocking": True}).challenge_mode() == "interstitial"
    assert _challenge_obs({"kind": "x", "mode": "embedded"}).challenge_mode() == "embedded"
    assert _challenge_obs(None).challenge_mode() == ""


def test_challenge_solved_reads_the_probe_flag():
    assert _challenge_obs({"kind": "x", "solved": True}).challenge_solved() is True
    assert _challenge_obs({"kind": "x", "solved": False}).challenge_solved() is False
    assert _challenge_obs(None).challenge_solved() is False


def test_rect_intersects_zones_matrix():
    zones = [(100.0, 100.0, 300.0, 80.0)]
    hits = dom_observe.rect_intersects_zones
    assert hits((150, 120, 50, 20), zones) is True     # fully inside
    assert hits((80, 90, 50, 30), zones) is True       # overlaps the corner
    assert hits((500, 500, 50, 50), zones) is False    # far away
    assert hits((0, 0, 100, 100), zones) is False      # edge-adjacent, no overlap
    assert hits((150, 120, 0, 0), zones) is False      # zero-area element
    assert hits((150, 120, 50, 20), []) is False       # no zones


def test_the_extract_js_skips_elements_inside_challenge_zones():
    """The walk-side half runs only in a real browser — pin the structural
    contract: zones are computed BEFORE the walk and every stamped element is
    checked against them (a challenge control is never listed, so the LLM can
    never be handed it)."""
    js = dom_observe._EXTRACT_JS
    assert "inChallengeZone" in js
    walk = js.split("for (const el of document.querySelectorAll(SELECTOR))")[1]
    assert "inChallengeZone(el.getBoundingClientRect())" in walk
