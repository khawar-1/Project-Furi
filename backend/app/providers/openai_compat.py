"""
Jarvis OS — OpenAI-compatible Provider
Drives any OpenAI-compatible chat-completions endpoint (DeepSeek, OpenRouter,
etc.) directly over httpx — NOT via the Groq SDK.

Why not reuse GroqProvider: the Groq SDK hardcodes the resource path
`/openai/v1/chat/completions`, so overriding its base_url produces
`https://api.deepseek.com/openai/v1/chat/completions` — a 404, because DeepSeek's
endpoint is `{base_url}/chat/completions`. There is no base_url that fixes a
baked-in path, so these endpoints need a client that targets the standard path.
httpx is already a dependency (the Ollama provider uses it) — no new package.
"""
import json
from typing import AsyncIterator, List, Optional

import httpx
from loguru import logger

from app.core.config import settings
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse

# DeepSeek's deepseek-chat caps output at 8192 tokens — asking for more is a
# 400 Bad Request. This ceiling is harmless for other OpenAI-compatible hosts
# (their limits are >= this), and no caller in this codebase asks for more than
# 4000, so clamping is a no-op guard today that prevents a future over-ask 400.
_MAX_OUTPUT_TOKENS = 8192


class OpenAICompatProvider(LLMProvider):
    """
    Generic OpenAI-compatible chat provider.

    POSTs to `{base_url}/chat/completions` with a Bearer token — the shape every
    OpenAI-compatible cloud API (DeepSeek, OpenRouter, Together, etc.) speaks.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        provider_name: str,
    ) -> None:
        self._api_key = api_key
        self._model_name = model
        # Trailing slash normalized so `{base}/chat/completions` is always clean.
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name

        if not self._api_key:
            raise ValueError(
                f"API key is not set for provider '{provider_name}'. "
                "Add the matching key to your .env file, or set LLM_PROVIDER=gemini."
            )

        # Generous read timeout for a long planner generation (~4000 tokens);
        # a short connect timeout still fails fast if the host is unreachable.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=10.0),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        logger.info(f"{provider_name} provider initialized: {self._model_name} @ {self._base_url}")

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _unreachable(self, exc: Exception) -> RuntimeError:
        """A transport failure carrying a message that is NEVER empty.

        2026-07-26 incident: a machine-wide DNS outage made the planner log
        `Planner LLM call failed (attempt 1): ` — nothing after the colon,
        because that httpx exception's `str()` was empty. Half the failure was
        undiagnosable from the log, and the user-facing text ended up as a raw
        `[Errno 11001] getaddrinfo failed`.

        Same class of defect as the 2026-07-24 round, which kept DeepSeek's error
        BODY on HTTP STATUS errors (`_raise_for_status`) but left TRANSPORT
        errors to whatever `str()` happened to give. Normalizing here rather than
        at each caller fixes it once for all ~13 provider call sites.

        The original exception is chained (`from exc`), so callers that classify
        by walking `__cause__` — planner._is_transport_error — still see the real
        httpx type."""
        detail = str(exc).strip()
        label = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return RuntimeError(
            f"{self._provider_name} could not be reached at {self._base_url} ({label})"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def _to_messages(self, messages: List[LLMMessage]) -> list:
        # DeepSeek 400s on a message with empty/whitespace content. The chat
        # route already filters these, but not every caller (planner, extractor)
        # does — so this is the belt to that suspenders. Never return an empty
        # list (also a 400): if filtering somehow removed everything, fall back
        # to the raw mapping so the API reports the real problem, not a mask.
        out = [
            {"role": m.role, "content": m.content}
            for m in messages
            if (m.content or "").strip()
        ]
        if not out and messages:
            out = [{"role": m.role, "content": m.content} for m in messages]
        return out

    @staticmethod
    def _resolve_max_tokens(max_tokens: Optional[int]) -> int:
        """Clamp the requested output budget to a value the API will accept."""
        return max(1, min(max_tokens or _MAX_OUTPUT_TOKENS, _MAX_OUTPUT_TOKENS))

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """`raise_for_status()` that KEEPS the body.

        Every OpenAI-compatible API — DeepSeek included — returns a JSON
        explanation with each 4xx/5xx (e.g.
        `{"error":{"message":"...","type":"invalid_request_error"}}`, or a
        moderation "Content Exists Risk"). The bare httpx `raise_for_status()`
        discards it, leaving only "Client error '400 Bad Request'" — which is
        the difference between a blind error and an actionable one (live: an
        opaque DeepSeek 400 surfaced straight into the chat, 2026-07-24).
        """
        if response.is_success:
            return
        # A streaming response hasn't been read yet — pull the (small) error body.
        detail = ""
        try:
            if not response.is_closed:
                await response.aread()
            detail = response.text.strip()
        except Exception:  # noqa: BLE001 — the status is the signal; body is a bonus
            detail = ""
        message = detail
        try:
            body = json.loads(detail)
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict) and err.get("message"):
                    message = err["message"]
                elif body.get("message"):
                    message = body["message"]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        summary = (
            f"{self._provider_name} API {response.status_code} "
            f"{response.reason_phrase}: {message or '(no response body)'}"
        )
        logger.error(summary)
        raise httpx.HTTPStatusError(
            summary, request=response.request, response=response
        )

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming chat via POST {base_url}/chat/completions."""
        payload = {
            "model": self._model_name,
            "messages": self._to_messages(messages),
            "temperature": temperature,
            "max_tokens": self._resolve_max_tokens(max_tokens),
            "stream": False,
        }

        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
            )
        except httpx.RequestError as exc:  # DNS, connect, read timeout, reset
            raise self._unreachable(exc) from exc
        await self._raise_for_status(response)
        data = response.json()

        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return LLMResponse(
            content=choice["message"].get("content") or "",
            model=data.get("model", self._model_name),
            provider=self.provider_name,
            tokens_used=usage.get("total_tokens"),
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Streaming chat — parses the OpenAI SSE `data: {...}` / `data: [DONE]` stream."""
        payload = {
            "model": self._model_name,
            "messages": self._to_messages(messages),
            "temperature": temperature,
            "max_tokens": self._resolve_max_tokens(max_tokens),
            "stream": True,
        }

        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
            ) as response:
                await self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if not data_str or data_str == "[DONE]":
                        if data_str == "[DONE]":
                            break
                        continue
                    try:
                        data = json.loads(data_str)
                        delta = data["choices"][0]["delta"].get("content")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except httpx.RequestError as exc:  # incl. a mid-stream disconnect
            raise self._unreachable(exc) from exc

    async def embed(self, text: str) -> EmbeddingResponse:
        """
        Embeddings are handled locally by fastembed (see app/memory/embedder.py),
        never through the chat provider. Return a zero vector as an inert stub.
        """
        logger.warning(
            f"{self._provider_name} provider embed() called — embeddings use local "
            "fastembed, not the chat provider. Returning zero vector."
        )
        return EmbeddingResponse(
            embedding=[0.0] * 768,
            model="none",
            provider=self.provider_name,
        )

    async def __aenter__(self) -> "OpenAICompatProvider":
        return self

    async def __aexit__(self, *args) -> None:
        await self._client.aclose()
