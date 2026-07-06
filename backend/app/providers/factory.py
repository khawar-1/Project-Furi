"""
Jarvis OS — LLM Provider Factory
Reads LLM_PROVIDER from config and instantiates the correct provider.
New providers are added here — no other code changes needed.
"""
from functools import lru_cache

from loguru import logger

from app.core.config import settings
from app.providers.base import LLMProvider


@lru_cache(maxsize=1)
def create_provider() -> LLMProvider:
    """
    Factory function — returns a singleton provider instance.
    Cached so the same instance is reused across all requests.
    
    To switch providers: change LLM_PROVIDER in .env and restart the backend.
    """
    provider_name = settings.LLM_PROVIDER.lower().strip()
    logger.info(f"Creating LLM provider: {provider_name}")

    if provider_name == "gemini":
        from app.providers.gemini import GeminiProvider
        return GeminiProvider()

    elif provider_name == "groq":
        from app.providers.groq_provider import GroqProvider
        return GroqProvider()

    elif provider_name == "ollama":
        from app.providers.ollama import OllamaProvider
        return OllamaProvider()

    elif provider_name == "openrouter":
        # OpenRouter uses the OpenAI-compatible API — implemented as Groq-style
        # with a different base URL and API key
        try:
            from groq import AsyncGroq
            import app.providers.groq_provider as groq_mod
            # Monkey-patch for OpenRouter compatibility
            provider = groq_mod.GroqProvider.__new__(groq_mod.GroqProvider)
            provider._api_key = settings.OPENROUTER_API_KEY
            provider._model_name = settings.OPENROUTER_MODEL
            from groq import AsyncGroq
            provider._client = AsyncGroq(
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
            )
            return provider
        except Exception as e:
            raise ValueError(f"Failed to initialize OpenRouter provider: {e}")

    else:
        raise ValueError(
            f"Unknown LLM provider: '{provider_name}'. "
            f"Valid options: gemini, groq, ollama, openrouter. "
            f"Check LLM_PROVIDER in your .env file."
        )


def reset_provider_cache() -> None:
    """Clear the provider cache — used in tests to swap providers."""
    create_provider.cache_clear()
