"""
Phase 15.3 — the vision fallback's unit pieces.

Covers the parts that make "DOM-first, vision when stuck" safe and cheap in
isolation: the provider seam (build_vision_provider returns None unless opted-in
AND configured, and never touches the network in the suite), the screenshot
capture (best-effort, never raises), the point→element mapping (the "vision
LOCATES, DOM ACTS" hinge), the config coercer, and the coordinate parsing. The
loop-level behaviors (stuck→escalate, budget, honest miss) live in
test_browser_loop.py.
"""
import pytest

from app.agents.browser_loop import _as_frac, _parse_action
from app.core import dom_observe
from app.core.app_settings import (
    BrowserVisionConfig,
    _coerce_browser_vision,
    default_browser_vision_config,
)
from app.core.config import settings
from app.core.dom_observe import Element, Observation
from app.providers import vision as vision_mod
from app.providers.vision import (
    GroqVisionProvider,
    QuotaError,
    RotatingVisionProvider,
    VisionProvider,
    _VisionCredential,
    build_vision_provider,
    is_quota_error,
    reset_vision_cooldowns,
)


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    """The key-cooldown registry is process-global; clear it around every test."""
    reset_vision_cooldowns()
    yield
    reset_vision_cooldowns()


def _clear_vision_keys(monkeypatch):
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", None)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "VISION_GROQ_API_KEYS", "")
    monkeypatch.setattr(settings, "VISION_GEMINI_API_KEYS", "")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "VISION_MODEL", "gem-model")
    monkeypatch.setattr(settings, "VISION_GROQ_MODEL", "groq-model")


# ----------------------------------------------------------------- config
def test_coerce_browser_vision_defaults_off_and_survives_junk():
    assert default_browser_vision_config().enabled is False
    assert _coerce_browser_vision(None).enabled is False
    assert _coerce_browser_vision("not a dict").enabled is False
    assert _coerce_browser_vision({}).enabled is False
    assert _coerce_browser_vision({"enabled": True}).enabled is True


# ------------------------------------------------------ build_vision_provider
def test_build_vision_provider_disabled_returns_none_without_touching_factory(monkeypatch):
    """Disabled short-circuits to None BEFORE the factory — so the disabled
    default suite is free, and a refuser factory (conftest's belt) never fires."""
    def _boom(_config):
        raise AssertionError("factory must not be called when disabled")

    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", _boom)
    assert build_vision_provider(BrowserVisionConfig(enabled=False)) is None
    assert build_vision_provider(None) is None


def test_build_vision_provider_uses_the_factory_when_enabled(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", lambda _c: sentinel)
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is sentinel


def test_build_vision_provider_swallows_a_factory_failure(monkeypatch):
    """A broken vision setup must never break a browse that would otherwise run
    DOM-only — a factory that raises degrades to None."""
    def _boom(_config):
        raise RuntimeError("no vision here")

    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", _boom)
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is None


def test_build_vision_provider_default_no_key_returns_none(monkeypatch):
    """Enabled but NO key configured → None (DOM-only). The default resolution
    path, factory cleared."""
    _clear_vision_keys(monkeypatch)
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is None


def test_build_vision_provider_single_gemini_key_builds_a_pool(monkeypatch):
    """Enabled + one Gemini key → a RotatingVisionProvider with that one
    credential (LAZILY built — no real SDK client is constructed here)."""
    _clear_vision_keys(monkeypatch)
    monkeypatch.setattr(settings, "VISION_API_KEY", "test-key")
    provider = build_vision_provider(BrowserVisionConfig(enabled=True))
    assert isinstance(provider, RotatingVisionProvider)
    assert [(c.kind, c.api_key, c.model) for c in provider._creds] == [
        ("gemini", "test-key", "gem-model")
    ]


def test_build_vision_provider_groq_keys_come_before_gemini(monkeypatch):
    """The whole point of the rotation: Groq (generous free tier) is tried before
    Gemini. Comma-separated, whitespace-tolerant, order preserved."""
    _clear_vision_keys(monkeypatch)
    monkeypatch.setattr(settings, "VISION_GROQ_API_KEYS", "g1, g2 , g3")
    monkeypatch.setattr(settings, "VISION_GEMINI_API_KEYS", "e1,e2")
    provider = build_vision_provider(BrowserVisionConfig(enabled=True))
    assert [(c.kind, c.api_key) for c in provider._creds] == [
        ("groq", "g1"), ("groq", "g2"), ("groq", "g3"),
        ("gemini", "e1"), ("gemini", "e2"),
    ]
    assert provider._creds[0].model == "groq-model"


def test_build_vision_provider_dedupes_and_falls_back_to_gemini_key(monkeypatch):
    """VISION_API_KEY/GEMINI_API_KEY extend the Gemini pool, and a key already in
    the list is not added twice."""
    _clear_vision_keys(monkeypatch)
    monkeypatch.setattr(settings, "VISION_GEMINI_API_KEYS", "e1")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "e1")  # duplicate of the list entry
    provider = build_vision_provider(BrowserVisionConfig(enabled=True))
    assert [(c.kind, c.api_key) for c in provider._creds] == [("gemini", "e1")]


def test_build_vision_provider_unknown_provider_returns_none(monkeypatch):
    _clear_vision_keys(monkeypatch)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "acme-vision")
    monkeypatch.setattr(settings, "VISION_API_KEY", "k")
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is None


# ---------------------------------------------------- quota classification
def test_is_quota_error_classifies_key_failures():
    assert is_quota_error(QuotaError("spent"))
    assert is_quota_error(RuntimeError("429 Too Many Requests"))
    assert is_quota_error(Exception("ResourceExhausted: quota exceeded, limit: 0"))
    assert is_quota_error(RuntimeError("invalid api key"))
    # a status-carrying error (httpx.HTTPStatusError shape)
    class _Resp:
        status_code = 403
    class _HTTPErr(Exception):
        response = _Resp()
    assert is_quota_error(_HTTPErr("forbidden"))
    # a plain transient/safety error is NOT a key failure
    assert not is_quota_error(RuntimeError("safety block"))
    assert not is_quota_error(ValueError("bad json"))


# ------------------------------------------------------ rotation behaviour
class _FakeVision(VisionProvider):
    """A pre-scripted vision provider: each _invoke pops the next reply, and an
    Exception reply is raised (so quota/transient paths can be driven)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    @property
    def model_name(self):
        return "fake"

    async def _invoke(self, *, prompt, image_jpeg):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply


def _cred(key, provider):
    c = _VisionCredential("gemini", key, "m")
    c._provider = provider  # pre-built → provider() returns the fake, no real client
    return c


async def test_rotation_cools_a_spent_key_and_advances():
    """A key that 429s is cooled and the NEXT key answers — in the same call."""
    p1 = _FakeVision([QuotaError("429 limit: 0")])
    p2 = _FakeVision(['{"action":"done"}'])
    rot = RotatingVisionProvider([_cred("k1", p1), _cred("k2", p2)])
    out = await rot.describe(prompt="p", image_jpeg=b"img")
    assert out == '{"action":"done"}'
    assert p1.calls == 1 and p2.calls == 1
    assert vision_mod._is_cooling("k1") and not vision_mod._is_cooling("k2")


async def test_all_keys_cooling_returns_empty_without_calling():
    """When every key is spent, describe returns "" (DOM-only) and a later call
    makes NO network attempt — the loop's circuit breaker then stops retrying."""
    p1 = _FakeVision([QuotaError("429")])
    p2 = _FakeVision([QuotaError("rate limit")])
    rot = RotatingVisionProvider([_cred("k1", p1), _cred("k2", p2)])
    assert await rot.describe(prompt="p", image_jpeg=b"i") == ""
    p1.calls = p2.calls = 0
    assert await rot.describe(prompt="p", image_jpeg=b"i") == ""
    assert p1.calls == 0 and p2.calls == 0  # both cooling → not even attempted


async def test_a_non_quota_error_falls_through_to_the_next_key():
    """The live 2026-07-23 bug: the first key fails with a NON-quota error (a
    deprecated Groq model → 404). It must rotate to the next key in the SAME call,
    not abort with "" — the working key answers, and the dead one is cooled briefly
    so later steps skip it instead of re-uploading the image and 404-ing again."""
    p1 = _FakeVision([RuntimeError("404 Not Found")])
    p2 = _FakeVision(['{"action":"done"}'])
    rot = RotatingVisionProvider([_cred("k1", p1), _cred("k2", p2)])
    out = await rot.describe(prompt="p", image_jpeg=b"img")
    assert out == '{"action":"done"}'
    assert p1.calls == 1 and p2.calls == 1
    assert vision_mod._is_cooling("k1") and not vision_mod._is_cooling("k2")


async def test_a_lone_non_quota_error_cools_briefly_and_returns_empty():
    """With no other key to fall through to, a non-quota failure returns "" (the
    loop goes DOM-only) AND cools the key briefly so the next step doesn't re-probe
    a dead model — bounded waste, self-healing after the short cooldown."""
    p1 = _FakeVision([RuntimeError("404 Not Found")])
    rot = RotatingVisionProvider([_cred("k1", p1)])
    assert await rot.describe(prompt="p", image_jpeg=b"i") == ""
    assert vision_mod._is_cooling("k1")


async def test_empty_image_short_circuits():
    p1 = _FakeVision(['{"action":"done"}'])
    rot = RotatingVisionProvider([_cred("k1", p1)])
    assert await rot.describe(prompt="p", image_jpeg=b"") == ""
    assert p1.calls == 0


# ---------------------------------------------------------- Groq provider
async def test_groq_vision_builds_the_openai_image_request(monkeypatch):
    """The Groq backend posts the OpenAI multimodal shape (text part + base64
    data-URI image_url) and returns choices[0].message.content. No network — the
    client's post is replaced."""
    prov = GroqVisionProvider("k", "groq-model", "https://api.groq.com/openai/v1")
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    async def _post(url, json=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    prov._client.post = _post
    out = await prov._invoke(prompt="P", image_jpeg=b"abc")
    assert out == "hi"
    assert captured["url"].endswith("/chat/completions")
    content = captured["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "P"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    await prov.aclose()


# --------------------------------------------------------- point → element
def _obs(*elements, viewport=(1000.0, 1000.0)):
    return Observation(
        observation_id="o", url="u", title="", elements=list(elements),
        element_total=len(elements), page_text="", text_truncated=False,
        viewport=viewport,
    )


def test_resolve_point_to_index_contains_the_point():
    obs = _obs(Element(index=1, role="button", name="a", rect=(0, 0, 100, 100)))
    assert dom_observe.resolve_point_to_index(obs, 0.05, 0.05) == 1   # (50, 50) inside
    assert dom_observe.resolve_point_to_index(obs, 0.5, 0.5) is None  # (500, 500) outside


def test_resolve_point_to_index_smallest_area_wins_on_overlap():
    """Nested boxes — a label inside a button. The tightest (smallest-area)
    element under the point is the most specific target."""
    big = Element(index=1, role="group", name="big", rect=(0, 0, 1000, 1000))
    small = Element(index=2, role="button", name="small", rect=(400, 400, 100, 100))
    obs = _obs(big, small)
    assert dom_observe.resolve_point_to_index(obs, 0.45, 0.45) == 2


def test_resolve_point_to_index_zero_viewport_is_none():
    obs = _obs(Element(index=1, role="button", name="a", rect=(0, 0, 100, 100)), viewport=(0, 0))
    assert dom_observe.resolve_point_to_index(obs, 0.5, 0.5) is None


def test_resolve_point_to_index_ignores_zero_area_elements():
    obs = _obs(Element(index=1, role="button", name="hidden", rect=(0, 0, 0, 0)))
    assert dom_observe.resolve_point_to_index(obs, 0.0, 0.0) is None


def test_resolve_point_inside_a_challenge_zone_maps_to_nothing():
    """The no-touch rule on the vision path (2026-07-19): vision may locate the
    "I'm not a robot" checkbox, but a point inside a challenge widget's zone
    maps to NO element — an honest miss, never a click on whatever overlaps."""
    obs = Observation(
        observation_id="o", url="u", title="",
        elements=[Element(index=1, role="button", name="x", rect=(400, 400, 200, 100))],
        element_total=1, page_text="", text_truncated=False,
        viewport=(1000.0, 1000.0),
        challenge={
            "kind": "reCAPTCHA", "mode": "embedded", "blocking": False,
            "solved": False, "zones": [{"x": 380, "y": 380, "w": 304, "h": 140}],
        },
    )
    # the point sits over element 1 — but element 1 is inside the widget's box.
    assert dom_observe.resolve_point_to_index(obs, 0.45, 0.45) is None
    # outside the zone, resolution works normally.
    obs.challenge = None
    assert dom_observe.resolve_point_to_index(obs, 0.45, 0.45) == 1


# ------------------------------------------------------------- screenshot
class _ShotPage:
    def __init__(self, data=b"jpeg-bytes", raises=False):
        self._data = data
        self._raises = raises

    async def screenshot(self, **kwargs):
        if self._raises:
            raise RuntimeError("boom")
        return self._data


async def test_capture_screenshot_returns_the_bytes():
    # An invalid JPEG cannot be downscaled by PIL, so it comes back unchanged —
    # capture is best-effort and never fails on the downscale.
    assert await dom_observe.capture_screenshot(_ShotPage(b"abc")) == b"abc"


async def test_capture_screenshot_is_best_effort():
    assert await dom_observe.capture_screenshot(_ShotPage(raises=True)) is None
    assert await dom_observe.capture_screenshot(_ShotPage(b"")) is None

    class _NoShot:
        pass

    assert await dom_observe.capture_screenshot(_NoShot()) is None


# ------------------------------------------------------- coordinate parsing
def test_as_frac_normalizes_by_magnitude():
    assert _as_frac(0.5) == 0.5      # a fraction
    assert _as_frac(1) == 1.0
    assert _as_frac(50) == 0.5       # a percentage
    assert _as_frac(500) == 0.5      # Gemini's 0-1000 per-mille convention
    assert _as_frac(-1) is None
    assert _as_frac(5000) is None
    assert _as_frac("x") is None
    assert _as_frac(None) is None


def test_parse_action_accepts_a_point_only_with_allow_point():
    # a fractional click is refused on the strict (DOM decide) path
    assert _parse_action('{"action":"click","x":0.5,"y":0.3}') is None
    # but accepted on the vision path
    assert _parse_action('{"action":"click","x":0.5,"y":0.3}', allow_point=True) == {
        "action": "click", "x": 0.5, "y": 0.3,
    }
    typed = _parse_action('{"action":"type","x":0.5,"y":0.3,"text":"hi"}', allow_point=True)
    assert typed == {"action": "type", "x": 0.5, "y": 0.3, "text": "hi", "submit": True}
    # an explicit index still works with allow_point on
    assert _parse_action('{"action":"click","index":2}', allow_point=True) == {
        "action": "click", "index": 2,
    }
    # submit/upload never accept a bare point — they need a concrete listed element
    assert _parse_action('{"action":"submit","x":0.5,"y":0.3}', allow_point=True) is None
    assert _parse_action('{"action":"upload","x":0.5,"y":0.3}', allow_point=True) is None


def test_parse_action_point_needs_both_coordinates():
    assert _parse_action('{"action":"click","x":0.5}', allow_point=True) is None
    assert _parse_action('{"action":"click","y":0.5}', allow_point=True) is None
