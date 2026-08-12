"""
Furi OS — Ollama Provider
Implements the LLMProvider interface using the local Ollama HTTP API.
No API key required — fully offline and local.
"""
import json
from typing import AsyncIterator, List, Optional

import httpx
from loguru import logger

from app.core.config import settings
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse


class OllamaProvider(LLMProvider):
    """
    Ollama local LLM provider.
    Communicates with the Ollama server via its REST API.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self._model_name = model or settings.OLLAMA_MODEL
        # A generous read timeout so a cold model load or a long first-token on
        # streaming doesn't ReadTimeout; a short connect timeout still fails fast
        # if the Ollama server isn't running.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=10.0)
        )
        logger.info(f"Ollama provider initialized: {self._model_name} @ {self._base_url}")

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model_name

    def _to_ollama_messages(self, messages: List[LLMMessage]) -> list:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming chat via Ollama /api/chat."""
        options = {"temperature": temperature}
        # Ollama's output cap is `num_predict`; without it a caller's max_tokens
        # is silently ignored and the model runs to EOS.
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {
            "model": self._model_name,
            "messages": self._to_ollama_messages(messages),
            "stream": False,
            "options": options,
        }

        response = await self._client.post(
            f"{self._base_url}/api/chat",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return LLMResponse(
            content=data["message"]["content"],
            model=self._model_name,
            provider=self.provider_name,
            tokens_used=data.get("eval_count"),
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Streaming chat via Ollama — parses NDJSON stream."""
        options = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {
            "model": self._model_name,
            "messages": self._to_ollama_messages(messages),
            "stream": True,
            "options": options,
        }

        async with self._client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    delta = data.get("message", {}).get("content", "")
                    if delta:
                        yield delta
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate embedding via Ollama /api/embeddings."""
        payload = {"model": self._model_name, "prompt": text}

        try:
            response = await self._client.post(
                f"{self._base_url}/api/embeddings",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return EmbeddingResponse(
                embedding=data["embedding"],
                model=self._model_name,
                provider=self.provider_name,
            )
        except Exception as e:
            logger.warning(f"Ollama embedding failed: {e}. Using zero vector.")
            return EmbeddingResponse(
                embedding=[0.0] * 768,
                model=self._model_name,
                provider=self.provider_name,
            )

    async def __aenter__(self) -> "OllamaProvider":
        return self

    async def __aexit__(self, *args) -> None:
        await self._client.aclose()
