"""
Jarvis OS — Vision Provider seam (Phase 15.3)

The browser loop (app/agents/browser_loop.py) reads a page as DOM text and acts
on it. That fails when the target has no usable DOM text — an icon-only button
with an empty accessible name, a canvas app, a visual-only SPA. 15.3 adds a
DOM-first VISION FALLBACK: only when the loop is stuck, a downscaled screenshot
is sent to a SECOND, image-capable model that identifies the next target, which
code then maps back to a real DOM element (vision LOCATES, DOM ACTS).

Why a separate seam and not the LLMProvider ABC
-----------------------------------------------
providers/base.py declares `LLMMessage.content: str` — a bare string, so a
multimodal content list fails validation before it reaches a provider (the
dom_observe.py docstring names this exact constraint), and the primary
deepseek-chat has no image input at all. Widening the whole ABC to carry images
for one narrow, opt-in feature is the wrong trade. Instead this is a small,
dedicated interface: one method that takes a prompt + one JPEG and returns the
model's raw text — the loop parses it exactly as it parses a `_decide` reply.

The seam mirrors providers/factory.py + browser_session.BROWSER_FACTORY:
- VISION_PROVIDER_FACTORY is the injectable hook (tests swap a fake / a refuser).
- build_vision_provider(config) returns None when disabled OR unconfigured, so
  the loop degrades cleanly to DOM-only — vision is never required.
- The provider is BUILT INSIDE the browser coroutine (like factory.build_provider)
  so any client it holds binds to the dedicated browser loop, not the main one.

Credential lives in .env (VISION_API_KEY, falling back to GEMINI_API_KEY — the
OAuth/API-key convention); only the on/off toggle is a runtime app setting
(app_settings.BrowserVisionConfig). Default backend is Gemini, reusing the
google-generativeai SDK already in the tree — no new dependency.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from loguru import logger

from app.core.config import settings

# A working floor for the vision model's output cap — the reading_enumerator /
# task_router landmine: on thinking models reasoning tokens count against the
# cap, so a tiny cap returns ZERO text and the loop would read that as "no usable
# action" and never benefit from vision.
_VISION_MAX_TOKENS = 512


class VisionProvider(ABC):
    """A minimal image-in / text-out model, used ONLY by the stuck browser loop.

    `describe` takes the same kind of decision prompt `_decide` builds plus one
    JPEG of the current viewport, and returns the model's raw text reply (the
    loop parses it into an action). It must be BEST-EFFORT — never raise; a
    failed/blocked/empty call returns "" and the loop falls back to its honest
    DOM-only stop."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @abstractmethod
    async def describe(self, *, prompt: str, image_jpeg: bytes) -> str:
        """One vision call. Returns the model's raw text, or "" on any failure."""
        ...

    async def aclose(self) -> None:
        """Release any client the provider holds. Default no-op (Gemini's SDK
        holds no per-instance client to close); an httpx-backed provider would
        override this. The caller closes the provider in a `finally`, mirroring
        the LLM provider's __aexit__."""
        return None


class GeminiVisionProvider(VisionProvider):
    """Vision via Google's Gemini (google-generativeai — already a dependency).

    Reuses the GeminiProvider.chat shape: the blocking SDK call runs off the
    event loop, and text is extracted defensively (the `.text` accessor RAISES
    on an empty/blocked response). The image is a `{mime_type, data}` part — no
    Pillow needed for the call itself."""

    def __init__(self, api_key: str, model: str) -> None:
        import google.generativeai as genai  # lazy: only when vision is built

        if not api_key:
            raise ValueError("vision provider needs an API key")
        self._model_name = model
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

    async def describe(self, *, prompt: str, image_jpeg: bytes) -> str:
        if not image_jpeg:
            return ""
        image_part = {"mime_type": "image/jpeg", "data": image_jpeg}
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._model.generate_content([prompt, image_part]),
            )
        except Exception as exc:  # a vision miss is never fatal to the loop
            logger.warning(f"vision describe call failed (non-critical): {type(exc).__name__}: {exc}")
            return ""
        return self._response_text(response)

    @staticmethod
    def _response_text(response: Any) -> str:
        """The `.text` accessor raises on an empty-parts response (reasoning ate
        the cap, or a safety block). Extract defensively and return "" — the loop
        treats "" as "no usable action" and stops honestly, never crashes."""
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


# BROWSER_FACTORY / google_services pattern: None = the default resolution below.
# A caller (or a test) may set this to build any VisionProvider (or return None);
# it is only ever consulted when vision is ENABLED, so the hermetic refuser fires
# exactly on the case that would otherwise build a real client.
VISION_PROVIDER_FACTORY: Optional[Callable[[Any], Optional[VisionProvider]]] = None


def build_vision_provider(config: Any) -> Optional[VisionProvider]:
    """A VisionProvider when vision is opted in AND a key is configured, else
    None (the loop stays DOM-only). `config` is a BrowserVisionConfig; when its
    `enabled` is false this returns None WITHOUT touching the factory, so the
    disabled state is free and safe.

    Best-effort: any construction failure logs and returns None — a broken vision
    setup must never break a browse that would otherwise run DOM-only."""
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
    if provider == "gemini":
        api_key = settings.VISION_API_KEY or settings.GEMINI_API_KEY
        if not api_key:
            logger.info("browser vision enabled but no VISION_API_KEY/GEMINI_API_KEY — staying DOM-only")
            return None
        try:
            return GeminiVisionProvider(api_key=api_key, model=settings.VISION_MODEL)
        except Exception as exc:
            logger.warning(f"could not build Gemini vision provider: {type(exc).__name__}: {exc}")
            return None

    logger.warning(f"unknown VISION_PROVIDER '{provider}' — browser vision disabled")
    return None
