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
from app.providers.vision import build_vision_provider


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
    """Enabled but no key configured → None (DOM-only). The default resolution
    path, factory cleared."""
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", None)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is None


def test_build_vision_provider_default_with_key_builds_gemini(monkeypatch):
    """Enabled + a key → the Gemini provider, built from the configured key (a
    stub stands in so the suite never constructs the real SDK client)."""
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", None)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "VISION_API_KEY", "test-key")
    monkeypatch.setattr(settings, "VISION_MODEL", "gemini-vision-test")

    built = {}

    class StubVision:
        def __init__(self, api_key, model):
            built["api_key"] = api_key
            built["model"] = model

    monkeypatch.setattr(vision_mod, "GeminiVisionProvider", StubVision)

    provider = build_vision_provider(BrowserVisionConfig(enabled=True))
    assert isinstance(provider, StubVision)
    assert built == {"api_key": "test-key", "model": "gemini-vision-test"}


def test_build_vision_provider_falls_back_to_gemini_key(monkeypatch):
    """VISION_API_KEY falls back to GEMINI_API_KEY — a user who already has Gemini
    configured only has to flip the toggle."""
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", None)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "gem-key")

    seen = {}

    class StubVision:
        def __init__(self, api_key, model):
            seen["api_key"] = api_key

    monkeypatch.setattr(vision_mod, "GeminiVisionProvider", StubVision)
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is not None
    assert seen["api_key"] == "gem-key"


def test_build_vision_provider_unknown_provider_returns_none(monkeypatch):
    monkeypatch.setattr(vision_mod, "VISION_PROVIDER_FACTORY", None)
    monkeypatch.setattr(settings, "VISION_PROVIDER", "acme-vision")
    monkeypatch.setattr(settings, "VISION_API_KEY", "k")
    assert build_vision_provider(BrowserVisionConfig(enabled=True)) is None


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
