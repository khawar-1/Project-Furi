"""
Jarvis OS — Gemini Provider
Implements the LLMProvider interface using Google's Gemini API.
Supports full streaming and embedding generation.
"""
import asyncio
from typing import AsyncIterator, List, Optional

import google.generativeai as genai
from loguru import logger

from app.core.config import settings
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse


class GeminiProvider(LLMProvider):
    """
    Google Gemini LLM provider.
    Uses the google-generativeai SDK with streaming support.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or settings.GEMINI_API_KEY
        self._model_name = model or settings.GEMINI_MODEL

        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. "
                "Add it to your .env file or set LLM_PROVIDER to a different provider."
            )

        genai.configure(api_key=self._api_key)

        # Generation config defaults
        self._generation_config = genai.GenerationConfig(
            temperature=0.7,
            max_output_tokens=8192,
        )

        # Safety settings — permissive for personal AI assistant use
        self._safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]

        self._model = genai.GenerativeModel(
            model_name=self._model_name,
            generation_config=self._generation_config,
            safety_settings=self._safety_settings,
        )

        logger.info(f"Gemini provider initialized: {self._model_name}")

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model_name

    def _to_gemini_history(self, messages: List[LLMMessage]) -> tuple[list, str]:
        """
        Convert LLMMessage list to Gemini's history format + latest user message.
        Returns (history, current_user_message).
        """
        history = []
        system_prompt_parts = []

        for msg in messages[:-1]:  # All but the last message go into history
            if msg.role == "system":
                system_prompt_parts.append(msg.content)
                continue

            gemini_role = "user" if msg.role == "user" else "model"
            history.append({
                "role": gemini_role,
                "parts": [msg.content],
            })

        # Build the final user message, prepending system context if present
        last_msg = messages[-1]
        current_message = last_msg.content

        if system_prompt_parts:
            system_text = "\n\n".join(system_prompt_parts)
            # Inject system prompt as the beginning of the user's message
            current_message = f"[System Instructions]\n{system_text}\n\n[User Message]\n{current_message}"

        return history, current_message

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Full (non-streaming) chat completion via Gemini."""
        history, current_message = self._to_gemini_history(messages)

        # Thinking models (gemini-2.5-*) spend reasoning tokens against
        # max_output_tokens BEFORE emitting any text — a small caller cap
        # (e.g. the router's one-word classification) yields an empty
        # response with finish_reason=MAX_TOKENS. Clamp to a working floor;
        # callers that want brevity get it from their prompt, not the cap.
        effective_max = max(max_tokens, 512) if max_tokens else 8192
        generation_config = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=effective_max,
        )

        model = genai.GenerativeModel(
            model_name=self._model_name,
            generation_config=generation_config,
            safety_settings=self._safety_settings,
        )

        # Run sync SDK in thread pool to avoid blocking the event loop
        chat_session = model.start_chat(history=history)
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: chat_session.send_message(current_message),
        )

        return LLMResponse(
            content=self._response_text(response),
            model=self._model_name,
            provider=self.provider_name,
            tokens_used=response.usage_metadata.total_token_count
            if hasattr(response, "usage_metadata")
            else None,
        )

    @staticmethod
    def _response_text(response) -> str:
        """The `.text` quick accessor RAISES when the response has no parts
        (thinking exhausted the token budget, or a safety block). Extract
        defensively and raise a CLEAN error naming the finish reason —
        callers fail open on exceptions, they must never crash on Gemini's
        accessor semantics."""
        try:
            return response.text
        except Exception:
            parts: list[str] = []
            for candidate in getattr(response, "candidates", None) or []:
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    text = getattr(part, "text", "")
                    if text:
                        parts.append(text)
            if parts:
                return "".join(parts)
            candidates = getattr(response, "candidates", None) or []
            finish = getattr(candidates[0], "finish_reason", "?") if candidates else "?"
            raise ValueError(
                f"Gemini returned no text (finish_reason={finish}) — "
                f"likely the output-token cap was consumed by reasoning, "
                f"or the reply was safety-blocked"
            )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """
        Streaming chat completion — yields text deltas as Gemini generates them.
        Runs the blocking SDK call in a thread pool and bridges to async via queue.
        """
        history, current_message = self._to_gemini_history(messages)

        # Same thinking-model floor as chat() — see the comment there.
        effective_max = max(max_tokens, 512) if max_tokens else 8192
        generation_config = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=effective_max,
        )

        model = genai.GenerativeModel(
            model_name=self._model_name,
            generation_config=generation_config,
            safety_settings=self._safety_settings,
        )

        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _stream_in_thread() -> None:
            """Run the blocking Gemini stream in a thread."""
            try:
                chat_session = model.start_chat(history=history)
                for chunk in chat_session.send_message(current_message, stream=True):
                    if chunk.text:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk.text)
            except Exception as e:
                logger.error(f"Gemini streaming error: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, f"\n[Error: {e}]")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # Sentinel

        # Launch the blocking stream in a thread pool
        loop.run_in_executor(None, _stream_in_thread)

        # Yield from queue until sentinel received
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    async def embed(self, text: str) -> EmbeddingResponse:
        """Generate an embedding vector using Gemini's embedding model."""
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: genai.embed_content(
                model="models/text-embedding-004",
                content=text,
                task_type="retrieval_document",
            ),
        )

        return EmbeddingResponse(
            embedding=result["embedding"],
            model="text-embedding-004",
            provider=self.provider_name,
        )
