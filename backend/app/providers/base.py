"""
Jarvis OS — LLM Provider Base Protocol
Defines the interface all LLM providers must implement.
Business logic never imports a concrete provider — only this interface.
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional

from pydantic import BaseModel


class LLMMessage(BaseModel):
    """A single message in a conversation."""
    role: str  # "user" | "assistant" | "system"
    content: str


class LLMResponse(BaseModel):
    """Non-streaming LLM response."""
    content: str
    model: str
    provider: str
    tokens_used: Optional[int] = None


class EmbeddingResponse(BaseModel):
    """Embedding vector response."""
    embedding: List[float]
    model: str
    provider: str


class LLMProvider(ABC):
    """
    Abstract LLM provider interface.
    
    All providers must implement:
    - chat()         → full response
    - stream_chat()  → async token iterator
    - embed()        → embedding vector
    - model_name     → current model identifier
    - provider_name  → provider identifier
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g. 'gemini', 'groq', 'ollama')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Current model being used."""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """
        Non-streaming chat completion.
        Returns the complete response after generation finishes.
        """
        ...

    @abstractmethod
    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """
        Streaming chat completion.
        Yields text deltas as they are generated.
        """
        ...

    @abstractmethod
    async def embed(self, text: str) -> EmbeddingResponse:
        """
        Generate an embedding vector for the given text.
        Used by the memory engine for semantic search.
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} provider={self.provider_name} model={self.model_name}>"
