"""
Jarvis OS — Vision Provider seam (Phase 15.3; vision-first since 2026-07-21)

The browser loop (app/browser/loop.py) reads a page as DOM text and acts on it,
AND — when a vision provider is configured — sees it as a set-of-marks screenshot
(vision LOCATES, DOM ACTS). This module is the image-in / text-out seam behind
that: one method that takes a decision prompt + one JPEG and returns the model's
raw text, which the loop parses exactly as it parses a text `_decide` reply.

Why a separate seam and not the LLMProvider ABC
-----------------------------------------------
providers/base.py declares `LLMMessage.content: str` — a bare string, so a
multimodal content list fails validation before it reaches a provider, and the
primary deepseek-chat has no image input at all. Widening the whole ABC for one
narrow, opt-in feature is the wrong trade. Instead this is a small, dedicated
interface.

Multi-key rotation (2026-07-23)
-------------------------------
Vision is a metered third-party API on a free tier, so a single key runs dry mid-
session and every step then 429s. `build_vision_provider` now assembles a POOL of
credentials — Groq keys first (a far more generous free tier, Llama-4 accepts
images), then Gemini keys — wrapped in a `RotatingVisionProvider`:

  - a key that returns a quota / rate-limit / auth error is set COOLING-DOWN (a
    process-global registry keyed by the key itself, TTL from settings) and the
    next live key is tried IN THE SAME describe() call;
  - when EVERY key is cooling, describe() returns "" and the loop degrades to
    DOM-text (its own circuit breaker then stops re-trying vision for the run) —
    so quota exhaustion NEVER fails a browse, it only removes the screenshot.

The rotator distinguishes a QUOTA/AUTH error (cool the key, rotate) from a benign
empty reply (a safety block, or reasoning that ate the token cap → "" with the
key left live) so a transient miss never burns a good key.

The seam mirrors providers/factory.py + browser_session.BROWSER_FACTORY:
- VISION_PROVIDER_FACTORY is the injectable hook (tests swap a fake / a refuser).
- build_vision_provider(config) returns None when disabled OR unconfigured, so
  the loop degrades cleanly to DOM-only — vision is never required.
- The provider is BUILT INSIDE the browser coroutine (like factory.build_provider)
  so any client it holds binds to the dedicated browser loop, not the main one.
"""
from __future__ import annotations

import asyncio
import base64
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from loguru import logger

from app.core.config import settings

# A working floor for the vision model's output cap — the reading_enumerator /
# task_router landmine: on thinking models reasoning tokens count against the
# cap, so a tiny cap returns ZERO text and the loop would read that as "no usable
# action" and never benefit from vision.
_VISION_MAX_TOKENS = 512


class QuotaError(Exception):
    """A vision call that failed because the KEY is out of quota / rate-limited /
    unauthorized — i.e. rotating to another key is the right response, not
    retrying this one. Raised by a concrete provider's `_invoke`; the rotator
    catches it to cool the key. Any OTHER exception means a transient/benign miss
    (the key stays live and only this one call is lost)."""


# Signals that a failure is the KEY's fault (rotate away), not a transient hiccup.
# Checked against both the exception class name and its stringified message, plus
# an HTTP status code when the exception carries one (httpx.HTTPStatusError).
_QUOTA_STATUS = {401, 403, 429}
_QUOTA_MARKERS = (
    "resourceexhausted",
    "ratelimit",
    "toomanyrequests",
    "permissiondenied",
    "unauthenticated",
    "unauthorized",
    "forbidden",
    "quota",
    "rate limit",
    "rate_limit",
    "insufficient",
    "exceeded",
    "limit: 0",
    "invalid api key",
    "invalid_api_key",
    "429",
    "401",
    "403",
)


def is_quota_error(exc: BaseException) -> bool:
    """True when `exc` means THIS key is spent/blocked and the pool should rotate.
    Best-effort classification over the class name, message, and any HTTP status
    the exception carries — deliberately broad, because the safe wrong-answer is
    to rotate (one extra key tried) rather than hammer a dead key."""
    if isinstance(exc, QuotaError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status in _QUOTA_STATUS:
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(m in blob for m in _QUOTA_MARKERS)


class VisionProvider(ABC):
    """A minimal image-in / text-out model, used by the browser loop's vision
    channel. `describe` takes the decision prompt `_decide` builds plus one JPEG
    of the current viewport and returns the model's raw text reply (the loop
    parses it into an action). It is BEST-EFFORT — never raises; a failed /
    blocked / empty call returns "" and the loop falls back to DOM-only."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @abstractmethod
    async def _invoke(self, *, prompt: str, image_jpeg: bytes) -> str:
        """One raw vision call. Returns the model's text (possibly "" for a
        benign empty/blocked reply). MAY RAISE — a QuotaError when the key is
        spent, any other exception for a transient miss; the base `describe`
        swallows both, and the rotator distinguishes them."""
        ...

    async def describe(self, *, prompt: str, image_jpeg: bytes) -> str:
        """Best-effort single call: "" on any failure. The rotator overrides this
        to iterate keys; a single-key provider uses this default."""
        if not image_jpeg:
            return ""
        try:
            return (await self._invoke(prompt=prompt, image_jpeg=image_jpeg)) or ""
        except Exception as exc:  # a vision miss is never fatal to the loop
            logger.warning(
                f"vision describe call failed (non-critical): {type(exc).__name__}: {exc}"
            )
            return ""

    async def aclose(self) -> None:
        """Release any client the provider holds. Default no-op; an httpx-backed
        provider overrides this."""
        return None


class GeminiVisionProvider(VisionProvider):
    """Vision via Google's Gemini (google-generativeai — already a dependency).

    Reuses the GeminiProvider.chat shape: the blocking SDK call runs off the
    event loop, and text is extracted defensively (the `.text` accessor RAISES on
    an empty/blocked response). The image is a `{mime_type, data}` part."""

    def __init__(self, api_key: str, model: str) -> None:
        import google.generativeai as genai  # lazy: only when vision is built

        if not api_key:
            raise ValueError("vision provider needs an API key")
        self._model_name = model
        self._api_key = api_key
        self._genai = genai
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model,
            generation_config=genai.GenerationConfig(
                temperature=0.0,
                max_output_tokens=_VISION_MAX_TOKENS,
            ),
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        )
        logger.info(f"Gemini vision provider initialized: {model}")

    @property
    def model_name(self) -> str:
        return self._model_name

    async def _invoke(self, *, prompt: str, image_jpeg: bytes) -> str:
        # genai.configure is process-global (last key wins), so re-assert THIS
        # provider's key before the call — otherwise a rotation to another Gemini
        # key could send this request on the wrong (cooling) credential.
        self._genai.configure(api_key=self._api_key)
        image_part = {"mime_type": "image/jpeg", "data": image_jpeg}
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._model.generate_content([prompt, image_part]),
        )
        return self._response_text(response)

    @staticmethod
    def _response_text(response: Any) -> str:
        """The `.text` accessor raises on an empty-parts response (reasoning ate
        the cap, or a safety block). Extract defensively and return "" — a benign
        empty, NOT a key failure, so the rotator keeps the key live."""
        try:
            return response.text or ""
        except Exception:
            parts: list[str] = []
            for candidate in getattr(response, "candidates", None) or []:
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    text = getattr(part, "text", "")
                    if text:
                        parts.append(text)
            return "".join(parts)


class GroqVisionProvider(VisionProvider):
    """Vision via Groq's OpenAI-compatible chat/completions (Llama-4 Scout /
    Maverick accept images). httpx directly — no SDK coupling — with the OpenAI
    multimodal message shape: a text part + a base64 data-URI image_url part. A
    non-2xx response raises (429/401/403 → the rotator cools this key)."""

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        import httpx  # a hard dependency already; lazy for symmetry

        if not api_key:
            raise ValueError("vision provider needs an API key")
        self._model_name = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0),
        )
        logger.info(f"Groq vision provider initialized: {model}")

    @property
    def model_name(self) -> str:
        return self._model_name

    async def _invoke(self, *, prompt: str, image_jpeg: bytes) -> str:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii")
        payload = {
            "model": self._model_name,
            "temperature": 0.0,
            "max_tokens": _VISION_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        resp = await self._client.post(self._url, json=payload)
        resp.raise_for_status()  # HTTPStatusError carries .response.status_code
        data = resp.json()
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


# --------------------------------------------------------------- key cooldowns
# PROCESS-GLOBAL so a key exhausted in one browse stays cooling across the next
# (a fresh RotatingVisionProvider is built per browse) instead of being re-probed
# every run. Keyed by the api_key itself; value = monotonic expiry. Reset on
# restart — quotas may have reset by then.
_KEY_COOLDOWNS: dict[str, float] = {}


def _is_cooling(api_key: str) -> bool:
    until = _KEY_COOLDOWNS.get(api_key)
    return until is not None and time.monotonic() < until


# A non-quota failure (a wrong/deprecated model → 404, a network blip) cools the
# key only BRIEFLY: long enough that a persistent config error (e.g. a dead Groq
# model) is skipped on every step of THIS run instead of re-uploading the image
# and 404-ing again, short enough that a transient hiccup on an otherwise-good key
# recovers within a couple of minutes.
_NON_QUOTA_COOLDOWN_SECONDS = 120


def _cool_key(api_key: str, seconds: Optional[int] = None) -> None:
    ttl = (
        int(getattr(settings, "VISION_KEY_COOLDOWN_SECONDS", 900))
        if seconds is None
        else seconds
    )
    _KEY_COOLDOWNS[api_key] = time.monotonic() + max(1, ttl)


def reset_vision_cooldowns() -> None:
    """Test/shutdown hook — clear the process-global cooldown registry."""
    _KEY_COOLDOWNS.clear()


class _VisionCredential:
    """One (provider_type, key, model) the pool can build lazily and cool."""

    def __init__(self, kind: str, api_key: str, model: str) -> None:
        self.kind = kind
        self.api_key = api_key
        self.model = model
        self._provider: Optional[VisionProvider] = None

    @property
    def label(self) -> str:
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else "****"
        return f"{self.kind}:…{tail}"

    def provider(self) -> VisionProvider:
        if self._provider is None:
            if self.kind == "groq":
                self._provider = GroqVisionProvider(
                    self.api_key, self.model, settings.VISION_GROQ_BASE_URL
                )
            else:
                self._provider = GeminiVisionProvider(self.api_key, self.model)
        return self._provider

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()


class RotatingVisionProvider(VisionProvider):
    """A VisionProvider over an ordered pool of credentials. `describe` tries each
    LIVE (non-cooling) key in turn until one returns a result. A QUOTA/AUTH failure
    cools that key for the full cooldown and rotates to the next; ANY OTHER failure
    (a wrong/deprecated model → 404, a network blip) cools the key BRIEFLY and ALSO
    rotates — so a dead Groq model falls through to the Gemini keys in the SAME
    call instead of killing vision for the whole run (live 2026-07-23: the first
    Groq key 404'd, the old code returned "" on the spot, and the two working
    Gemini keys were never tried). Only when every key is cooling/exhausted does it
    return "" (the loop goes DOM-only). Never raises."""

    def __init__(self, credentials: list[_VisionCredential]) -> None:
        if not credentials:
            raise ValueError("RotatingVisionProvider needs at least one credential")
        self._creds = credentials
        self._i = 0

    @property
    def model_name(self) -> str:
        if len(self._creds) == 1:
            return self._creds[0].model
        return f"rotating({len(self._creds)} keys)"

    async def _invoke(self, *, prompt: str, image_jpeg: bytes) -> str:  # pragma: no cover
        # The rotator drives keys directly in describe(); this satisfies the ABC.
        return await self._creds[self._i].provider()._invoke(
            prompt=prompt, image_jpeg=image_jpeg
        )

    def _next_live(self) -> Optional[_VisionCredential]:
        """The next credential (round-robin from the last used) whose key is not
        cooling, or None when every key is cooling."""
        n = len(self._creds)
        for offset in range(n):
            cred = self._creds[(self._i + offset) % n]
            if not _is_cooling(cred.api_key):
                self._i = (self._i + offset) % n
                return cred
        return None

    async def describe(self, *, prompt: str, image_jpeg: bytes) -> str:
        if not image_jpeg:
            return ""
        for _ in range(len(self._creds)):
            cred = self._next_live()
            if cred is None:
                logger.info("browse: every vision key is cooling — DOM-only this step")
                return ""
            try:
                provider = cred.provider()
            except Exception as exc:  # a broken credential — cool it, try the next
                logger.warning(
                    f"vision key {cred.label} failed to build ({type(exc).__name__}) "
                    "— cooling and rotating"
                )
                _cool_key(cred.api_key)
                self._i = (self._i + 1) % len(self._creds)
                continue
            try:
                return (await provider._invoke(prompt=prompt, image_jpeg=image_jpeg)) or ""
            except Exception as exc:
                if is_quota_error(exc):
                    logger.info(
                        f"vision key {cred.label} out of quota / rate-limited "
                        f"({type(exc).__name__}) — cooling and rotating to the next key"
                    )
                    _cool_key(cred.api_key)
                    self._i = (self._i + 1) % len(self._creds)
                    continue
                logger.warning(
                    f"vision call on {cred.label} failed ({type(exc).__name__}: {exc}) "
                    "— cooling briefly and rotating to the next key"
                )
                _cool_key(cred.api_key, _NON_QUOTA_COOLDOWN_SECONDS)
                self._i = (self._i + 1) % len(self._creds)
                continue
        return ""

    async def aclose(self) -> None:
        for cred in self._creds:
            await cred.aclose()


def _split_keys(raw: str) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


def _build_credentials() -> list[_VisionCredential]:
    """The ordered credential pool from settings: Groq keys first (generous free
    tier), then Gemini keys (the singular VISION_API_KEY / GEMINI_API_KEY extend
    the Gemini pool for back-compat). Deduped, order preserved."""
    creds: list[_VisionCredential] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, key: str, model: str) -> None:
        if not key or (kind, key) in seen:
            return
        seen.add((kind, key))
        creds.append(_VisionCredential(kind, key, model))

    for key in _split_keys(settings.VISION_GROQ_API_KEYS):
        _add("groq", key, settings.VISION_GROQ_MODEL)
    for key in _split_keys(settings.VISION_GEMINI_API_KEYS):
        _add("gemini", key, settings.VISION_MODEL)
    # Back-compat singletons: only Gemini has ever had one, and it extends the
    # Gemini pool (VISION_API_KEY wins over GEMINI_API_KEY, as before).
    _add("gemini", settings.VISION_API_KEY or settings.GEMINI_API_KEY, settings.VISION_MODEL)
    return creds


# BROWSER_FACTORY / google_services pattern: None = the default resolution below.
# A caller (or a test) may set this to build any VisionProvider (or return None);
# it is only ever consulted when vision is ENABLED, so the hermetic refuser fires
# exactly on the case that would otherwise build a real client.
VISION_PROVIDER_FACTORY: Optional[Callable[[Any], Optional[VisionProvider]]] = None


def build_vision_provider(config: Any) -> Optional[VisionProvider]:
    """A VisionProvider when vision is opted in AND at least one key is configured,
    else None (the loop stays DOM-only). `config` is a BrowserVisionConfig; when
    its `enabled` is false this returns None WITHOUT touching the factory, so the
    disabled state is free and safe.

    The returned provider ROTATES across every configured key (Groq before
    Gemini); a spent key cools and the pool falls through to the next, and to
    DOM-only when all are spent. Best-effort: any construction failure logs and
    returns None — a broken vision setup must never break a DOM-only browse."""
    if config is None or not getattr(config, "enabled", False):
        return None

    factory = VISION_PROVIDER_FACTORY
    if factory is not None:
        try:
            return factory(config)
        except Exception as exc:
            logger.warning(f"vision factory failed: {type(exc).__name__}: {exc}")
            return None

    provider = (settings.VISION_PROVIDER or "gemini").lower().strip()
    # VISION_PROVIDER stays a recognised-backend guard so a typo disables vision
    # loudly rather than silently building a pool the user didn't mean.
    if provider not in ("gemini", "groq", "rotating", "auto"):
        logger.warning(f"unknown VISION_PROVIDER '{provider}' — browser vision disabled")
        return None

    credentials = _build_credentials()
    if not credentials:
        logger.info(
            "browser vision enabled but no vision keys configured "
            "(VISION_GROQ_API_KEYS / VISION_GEMINI_API_KEYS / VISION_API_KEY) — staying DOM-only"
        )
        return None
    try:
        pool = RotatingVisionProvider(credentials)
        logger.info(
            "browser vision pool: "
            + ", ".join(c.label for c in credentials)
            + " (Groq before Gemini; a spent key cools and rotates)"
        )
        return pool
    except Exception as exc:
        logger.warning(f"could not build the vision pool: {type(exc).__name__}: {exc}")
        return None
