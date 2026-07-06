"""
Jarvis OS — Groq Provider
Implements the LLMProvider interface using the Groq API.
Groq provides ultra-fast inference on open models (Llama, Mixtral, etc.).
"""
import asyncio
from typing import AsyncIterator, List, Optional

from loguru import logger

from app.core.config import settings
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse


class GroqProvider(LLMProvider):
    """
    Groq LLM provider using the official Groq Python SDK.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or settings.GROQ_API_KEY
        self._model_name = model or settings.GROQ_MODEL

        if not self._api_key:
            raise ValueError(
                "GROQ_API_KEY is not set. "
                "Add it to your .env file or set LLM_PROVIDER=gemini."
            )

        try:
            from groq import AsyncGroq
            self._client = AsyncGroq(api_key=self._api_key)
        except ImportError:
            raise ImportError("groq package not installed. Run: pip install groq")

        logger.info(f"Groq provider initialized: {self._model_name}")

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model_name

    def _to_groq_messages(self, messages: List[LLMMessage]) -> list:
        """Convert LLMMessage list to Groq API format."""
        return [{"role": m.role, "content": m.content} for m in messages]

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming chat via Groq."""
        response = await self._client.chat.completions.create(
            model=self._model_name,
            messages=self._to_groq_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens or 8192,
            stream=False,
        )

        return LLMResponse(
            content=response.choices[0].message.content or "",
            model=self._model_name,
            provider=self.provider_name,
            tokens_used=response.usage.total_tokens if response.usage else None,
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Streaming chat via Groq — native async streaming support."""
        stream = await self._client.chat.completions.create(
            model=self._model_name,
            messages=self._to_groq_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens or 8192,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def embed(self, text: str) -> EmbeddingResponse:
        """
        Groq does not currently offer an embedding API.
        Falls back to a simple zero vector — replace with a local model in Phase 2.
        """
        logger.warning("Groq does not support embeddings. Using zero vector fallback.")
        return EmbeddingResponse(
            embedding=[0.0] * 768,
            model="none",
            provider=self.provider_name,
        )
