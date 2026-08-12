"""
Phase 14 Part 1 — DOM observation: the index contract and the render budget.
"""
import pytest

from app.agents.rendering import _STEP_RESULT_CAPS
from app.core import dom_observe
from app.core.dom_observe import (
    _ELEMENT_BUDGET,
    _NAME_MAX,
    _PAGE_TEXT_BUDGET,
    _PAGE_TEXT_CAPTURE,
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


# --------------------------------------------- cross-frame observation (Phase 6)
#
# Frames are where checkout forms, booking widgets and player controls live, and
# before 2026-07-26 they were invisible: the walk ran on one document and a page
# whose whole purpose sat inside an iframe read as empty.
#
# The security-critical half is the CHALLENGE-ZONE UNION. The top document's probe
# descends only into SAME-ORIGIN subframes (a cross-origin contentDocument throws),
# but Playwright's frame.evaluate works cross-origin — so walking frames without
# carrying their zones up would make a CAPTCHA widget in a cross-origin frame
# listable and clickable by an agent that must never touch one.
class FakeFrame:
    def __init__(self, url, payload, box, *, raises=False):
        self.url = url
        self._payload = payload
        self._box = box
        self._raises = raises
        self.evaluated = 0

    async def evaluate(self, js, arg=None):
        self.evaluated += 1
        if self._raises:
            raise RuntimeError("cross-origin refused")
        base = (arg or {}).get("base", 0) if isinstance(arg, dict) else 0
        out = dict(self._payload)
        out["elements"] = [
            {**e, "index": base + i + 1}
            for i, e in enumerate(self._payload.get("elements", []))
        ]
        return out

    async def frame_element(self):
        box = self._box

        class _Handle:
            async def bounding_box(self):
                return box

        return _Handle()

    async def query_selector(self, selector):
        return "frame-handle" if selector in self._payload.get("_present", set()) else None


class FramedPage(FakePage):
    def __init__(self, payload=None, present=(), frames=()):
        super().__init__(payload, present)
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]


async def test_a_content_frame_extends_the_element_list():
    """Indices CONTINUE the top document's numbering, so they stay globally
    unique — an index means one element in one document."""
    frame = FakeFrame(
        "https://pay.test/widget",
        {"elements": [_element(1, role="button", name="Pay now")], "total": 1},
        {"x": 40.0, "y": 200.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1), _element(2)]), frames=[frame])

    obs = await observe(page)

    assert obs.element_total == 3
    assert [e.index for e in obs.elements] == [1, 2, 3]
    pay = obs.elements[-1]
    assert pay.name == "Pay now"
    assert pay.frame_id.endswith("https://pay.test/widget")
    assert pay.frame_url == "https://pay.test/widget"


async def test_a_frame_elements_rect_is_translated_into_top_page_space():
    """The silent-corruption case: overlay_marks draws badges in TOP-page
    coordinates, so an untranslated frame rect puts every badge in the wrong
    place and hands the vision model a mislabelled screenshot."""
    frame = FakeFrame(
        "https://pay.test/w",
        {"elements": [{**_element(1), "rect": {"x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0}}],
         "total": 1},
        {"x": 40.0, "y": 200.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1)]), frames=[frame])

    obs = await observe(page)

    assert obs.elements[-1].rect == (50.0, 220.0, 100.0, 30.0)


async def test_a_frames_challenge_zones_are_unioned_and_translated():
    """NON-NEGOTIABLE. A CAPTCHA widget inside a frame must contribute its
    no-touch zones to the top-page probe, translated — otherwise walking frames
    would make it clickable."""
    frame = FakeFrame(
        "https://captcha.test/w",
        {
            "elements": [_element(1)],
            "total": 1,
            "challenge": {
                "kind": "reCAPTCHA", "blocking": True, "mode": "interstitial",
                "zones": [{"x": 5.0, "y": 5.0, "w": 300.0, "h": 80.0}],
            },
        },
        {"x": 100.0, "y": 300.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1)]), frames=[frame])

    obs = await observe(page)

    assert obs.challenge is not None
    zones = obs.challenge["zones"]
    assert {"x": 105.0, "y": 305.0, "w": 300.0, "h": 80.0} in zones
    # `blocking` must NOT be promoted: a challenge INSIDE a frame is an EMBEDDED
    # widget from the page's point of view. Promoting it would make every page
    # carrying a reCAPTCHA read as a full-page interstitial and pause the run.
    assert obs.challenge.get("blocking") is not True
    assert obs.challenge.get("mode") != "interstitial"


async def test_a_tiny_frame_is_never_walked():
    """Tracking beacons and 1x1 ad slots must not cost a round-trip each."""
    beacon = FakeFrame(
        "https://ads.test/beacon",
        {"elements": [_element(1, name="Tracker")], "total": 1},
        {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
    )
    page = FramedPage(_payload([_element(1)]), frames=[beacon])

    obs = await observe(page)

    assert beacon.evaluated == 0
    assert obs.element_total == 1


async def test_a_non_http_frame_is_skipped():
    """about:blank / data: / srcdoc stubs carry nothing worth a round-trip."""
    stub = FakeFrame(
        "about:blank",
        {"elements": [_element(1, name="Stub")], "total": 1},
        {"x": 0.0, "y": 0.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1)]), frames=[stub])

    await observe(page)
    assert stub.evaluated == 0


async def test_a_frame_that_refuses_never_breaks_the_observation():
    """A frame that navigated mid-read, or a cross-origin one that refuses, is
    normal. The top document's observation stands on its own."""
    hostile = FakeFrame(
        "https://x.test/f", {"elements": [], "total": 0},
        {"x": 0.0, "y": 0.0, "width": 600.0, "height": 400.0}, raises=True,
    )
    page = FramedPage(_payload([_element(1, name="Top link")]), frames=[hostile])

    obs = await observe(page)
    assert obs.element_total == 1
    assert obs.elements[0].name == "Top link"


async def test_resolve_tries_the_top_document_before_any_frame():
    """Ordering matters: the top document is where almost every element lives,
    and it is the only arm a fake page without frames ever reaches."""
    frame = FakeFrame(
        "https://pay.test/w",
        {"elements": [_element(1, name="In frame")], "total": 1},
        {"x": 0.0, "y": 0.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1, name="Top")]), frames=[frame])
    obs = await observe(page)

    top = obs.elements[0]
    sel = f'[data-jarvis-obs="{obs.observation_id}"][data-jarvis-idx="{top.index}"]'
    page._present.add(sel)

    assert await resolve(page, obs, top.index) == "handle"
    assert page.queries[-1] == sel


async def test_a_frame_element_resolves_in_its_own_frame():
    frame = FakeFrame(
        "https://pay.test/w",
        {"elements": [_element(1, name="In frame")], "total": 1},
        {"x": 0.0, "y": 0.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1, name="Top")]), frames=[frame])
    obs = await observe(page)

    target = obs.elements[-1]
    sel = f'[data-jarvis-obs="{obs.observation_id}"][data-jarvis-idx="{target.index}"]'
    frame._payload["_present"] = {sel}

    assert await resolve(page, obs, target.index) == "frame-handle"


async def test_a_frame_element_whose_frame_is_gone_is_stale():
    """The staleness promise holds structurally across documents: a frame that
    navigated away took its stamps with it."""
    frame = FakeFrame(
        "https://pay.test/w",
        {"elements": [_element(1, name="In frame")], "total": 1},
        {"x": 0.0, "y": 0.0, "width": 600.0, "height": 400.0},
    )
    page = FramedPage(_payload([_element(1)]), frames=[frame])
    obs = await observe(page)
    target = obs.elements[-1]

    page.frames = [page.main_frame]  # the frame is gone
    with pytest.raises(StaleObservation):
        await resolve(page, obs, target.index)


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


# ------------------------------------------------------- capture vs render
# A PROMPT budget is not a CAPTURE budget (2026-07-26). These were the same
# number, so nothing downstream could see past what the decision model was shown:
# `_extract_data` reads the prose and clipped it to its own 9000-char ceiling, a
# ceiling it could never reach. Live consequence — on daraz.pk's real results page
# extraction saw only header/nav/filters and returned ZERO records three times.
async def test_the_page_text_is_captured_past_the_prompt_budget():
    page = FakePage(_payload(text="y" * (_PAGE_TEXT_BUDGET * 3)))
    obs = await observe(page)
    assert len(obs.page_text) == _PAGE_TEXT_BUDGET          # what the prompt shows
    assert len(obs.text_full) == _PAGE_TEXT_BUDGET * 3      # what code can read
    assert obs.text_full.startswith(obs.page_text)


async def test_capture_is_itself_bounded():
    page = FakePage(_payload(text="z" * (_PAGE_TEXT_CAPTURE * 2)))
    obs = await observe(page)
    assert len(obs.text_full) == _PAGE_TEXT_CAPTURE


async def test_a_short_page_has_identical_capture_and_render_text():
    page = FakePage(_payload(text="a short page"))
    obs = await observe(page)
    assert obs.page_text == obs.text_full == "a short page"
    assert obs.text_truncated is False


async def test_the_rendered_prompt_did_not_grow_with_the_wider_capture():
    """The whole point of splitting them: the decision prompt must be exactly as
    tight as it was, or a perception fix silently becomes a cost regression."""
    page = FakePage(_payload(text="w" * (_PAGE_TEXT_CAPTURE * 2)))
    out = render(await observe(page))
    assert len(out) < _ELEMENT_BUDGET + _PAGE_TEXT_BUDGET + 500
    assert "… (truncated)" in out


async def test_an_elements_full_name_survives_the_prompt_clip():
    """A results-grid card carries title, price and rating inside ONE element's
    innerText; cutting that at the prompt's _NAME_MAX is how the data went
    missing. `name` stays clipped so the prompt, the action signature and the page
    fingerprint do not move; `name_full` is what extraction reads."""
    long_name = "Yonex Astrox 99 Pro Badminton Racket " * 5 + "Rs. 24,999 4.7 (128)"
    page = FakePage(_payload([_element(1, name=long_name)]))
    element = (await observe(page)).elements[0]
    assert len(element.name) == _NAME_MAX
    assert len(element.name_full) > _NAME_MAX
    assert "Rs. 24,999" in element.name_full          # the price survived
    assert element.name_full.startswith(element.name)
    assert element.name in element.render()           # the prompt sees the clip
    assert element.name_full not in element.render()


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
    """Furi does not handle credentials. The JS never reads a password value;
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
# The structural half of "Furi never touches a CAPTCHA": the probe reports
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
    never be handed it).

    Re-pinned 2026-07-26 when the walk was rewritten for shadow DOM and the wide
    tier. The check now lives in eligible(), which is the SINGLE gate both tiers
    pass through — that is a stronger guarantee than the old inline test, but
    only while it stays single. These assertions are what keep it so: a future
    tier that lists elements without calling eligible() would list a CAPTCHA
    control, and that must fail loudly here rather than in a live browser."""
    js = dom_observe._EXTRACT_JS
    assert "inChallengeZone" in js
    # Zones are computed before anything is listed.
    assert js.index("challengeZones") < js.index("const eligible")
    # The one gate carries the check...
    gate = js.split("const eligible = (el) => {")[1].split("};")[0]
    assert "inChallengeZone(r)" in gate

    # ...and there is EXACTLY ONE place an element can enter a candidate list, so
    # a tier added later cannot slip past the gate. The walk classifies into a
    # tier first and admits through a single eligible() call; if that ever
    # becomes two calls, this fails and the reviewer has to re-argue the
    # guarantee rather than discovering the hole in a live browser.
    walk = js.split("while (stack.length")[1].split("let listed")[0]
    assert walk.count("eligible(el)") == 1, (
        "the walk must admit candidates through ONE eligible() gate; found "
        f"{walk.count('eligible(el)')}"
    )
    assert "if (tier >= 0 && eligible(el)) {" in walk


# ------------------------- form membership (action-level safety, 2026-07-21)
def test_form_of_parses_the_js_shape():
    from app.core.dom_observe import _form_of

    assert _form_of(None) == {}
    assert _form_of("junk") == {}
    assert _form_of({"method": "post", "submit": True, "search": False}) == {
        "form_member": True,
        "form_submit": True,
        "form_method": "POST",
        "form_search": False,
    }


def test_render_marks_a_posting_submit_control_but_not_a_search_one():
    from app.core.dom_observe import Element

    apply_btn = Element(
        index=1, role="button", name="Apply",
        form_member=True, form_submit=True, form_method="POST",
    )
    assert "(submits a form)" in apply_btn.render()
    search_btn = Element(
        index=2, role="button", name="Go",
        form_member=True, form_submit=True, form_method="POST", form_search=True,
    )
    assert "(submits a form)" not in search_btn.render()
    get_btn = Element(
        index=3, role="button", name="Filter",
        form_member=True, form_submit=True, form_method="GET",
    )
    assert "(submits a form)" not in get_btn.render()


# ----------------------- set-of-marks capture (vision-first hybrid)
async def test_capture_marked_overlays_captures_and_removes():
    """The mark overlay is injected from the SAME rects the element list
    carries, the viewport is captured, and the overlay is removed even when the
    capture path raises — the page is never left wearing badges."""
    from app.core.dom_observe import Element, Observation, capture_marked

    calls = []

    class _Page:
        async def evaluate(self, js, arg=None):
            calls.append((js[:40], arg))
            return None

        async def screenshot(self, **kwargs):
            return b"\xff\xd8\xff\xe0-jpeg"

    obs = Observation(
        observation_id="o", url="u", title="", element_total=2,
        elements=[
            Element(index=1, role="button", name="Go", rect=(10, 10, 100, 30)),
            Element(index=2, role="link", name="Zero", rect=(0, 0, 0, 0)),  # no box
        ],
        page_text="", text_truncated=False,
    )
    image = await capture_marked(_Page(), obs)

    assert image == b"\xff\xd8\xff\xe0-jpeg"
    assert len(calls) == 2                      # mark + unmark
    mark_items = calls[0][1]
    assert [it["i"] for it in mark_items] == [1]   # zero-size boxes never marked
    assert calls[1][1] is None                  # the unmark pass


async def test_capture_marked_survives_a_failing_overlay():
    """A page whose evaluate raises still yields a plain screenshot — the marks
    are a quality upgrade, never a new failure mode."""
    from app.core.dom_observe import Element, Observation, capture_marked

    class _Page:
        async def evaluate(self, js, arg=None):
            raise RuntimeError("CSP blocked the overlay")

        async def screenshot(self, **kwargs):
            return b"\xff\xd8\xff\xe0-plain"

    obs = Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[Element(index=1, role="button", name="Go", rect=(1, 1, 5, 5))],
        page_text="", text_truncated=False,
    )
    assert await capture_marked(_Page(), obs) == b"\xff\xd8\xff\xe0-plain"


# --------------------------------------- Phase 6: pipelined Python-side marks
def test_overlay_marks_draws_in_python_from_a_base_image():
    """The pipelining win: given a base screenshot pre-captured concurrently with
    observe(), the numbered marks are drawn in Python (Pillow) from the obs rects
    — a valid JPEG comes back and it differs from the blank base (something drew)."""
    Image = pytest.importorskip("PIL.Image")
    import io

    from app.core.dom_observe import Element, Observation, overlay_marks

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), (255, 255, 255)).save(buf, format="JPEG")
    base = buf.getvalue()
    obs = Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[Element(index=7, role="button", name="Go", rect=(10, 10, 100, 30))],
        page_text="", text_truncated=False, viewport=(200, 100),
    )
    out = overlay_marks(base, obs)
    assert out is not None and out[:2] == b"\xff\xd8"   # a JPEG came back
    assert out != base                                  # marks were drawn on it


def test_overlay_marks_returns_none_on_an_unknown_viewport():
    """Without a viewport there is no CSS→image scale factor, so overlay_marks
    bows out (returns None) and the caller falls back to the in-page path."""
    from app.core.dom_observe import Element, Observation, overlay_marks

    obs = Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[Element(index=1, role="button", name="Go", rect=(1, 1, 5, 5))],
        page_text="", text_truncated=False, viewport=(0, 0),
    )
    assert overlay_marks(b"anything", obs) is None


async def test_capture_marked_uses_the_python_overlay_when_given_a_base_image():
    """With a base_image, capture_marked draws marks in Python and never touches
    the page — no _MARK_JS evaluate, no re-screenshot (the round-trips it saves)."""
    pytest.importorskip("PIL.Image")
    import io

    from PIL import Image

    from app.core.dom_observe import Element, Observation, capture_marked

    buf = io.BytesIO()
    Image.new("RGB", (120, 80), (0, 0, 0)).save(buf, format="JPEG")

    class _Page:
        def __init__(self):
            self.evaluate_calls = 0
            self.screenshot_calls = 0

        async def evaluate(self, js, arg=None):
            self.evaluate_calls += 1

        async def screenshot(self, **kwargs):
            self.screenshot_calls += 1
            return b"should-not-be-used"

    page = _Page()
    obs = Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[Element(index=1, role="button", name="Go", rect=(5, 5, 20, 10))],
        page_text="", text_truncated=False, viewport=(120, 80),
    )
    out = await capture_marked(page, obs, base_image=buf.getvalue())
    assert out is not None and out[:2] == b"\xff\xd8"
    assert page.evaluate_calls == 0      # Python overlay — no in-page marks
    assert page.screenshot_calls == 0    # base reused — no second capture


async def test_capture_marked_falls_back_to_in_page_when_overlay_cannot_draw():
    """A base_image the overlay can't use (unknown viewport → overlay None) still
    yields marks via the in-page path — a vision decision is never dropped for it."""
    from app.core.dom_observe import Element, Observation, capture_marked

    calls = []

    class _Page:
        async def evaluate(self, js, arg=None):
            calls.append(js[:20])

        async def screenshot(self, **kwargs):
            return b"\xff\xd8\xff\xe0-plain"

    obs = Observation(
        observation_id="o", url="u", title="", element_total=1,
        elements=[Element(index=1, role="button", name="Go", rect=(1, 1, 5, 5))],
        page_text="", text_truncated=False, viewport=(0, 0),   # overlay → None
    )
    out = await capture_marked(_Page(), obs, base_image=b"not-a-real-jpeg")
    assert out == b"\xff\xd8\xff\xe0-plain"   # in-page path ran
    assert len(calls) == 2                    # mark + unmark
