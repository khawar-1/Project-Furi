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

    @property
    def model_name(self) -> str:
        return self._model_name

    def _to_messages(self, messages: List[LLMMessage]) -> list:
        return [{"role": m.role, "content": m.content} for m in messages]

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
            "max_tokens": max_tokens or 8192,
            "stream": False,
        }

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
        )
        response.raise_for_status()
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
            "max_tokens": max_tokens or 8192,
            "stream": True,
        }

        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            json=payload,
        ) as response:
            response.raise_for_status()
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
