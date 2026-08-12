"""
Furi OS — LLM Provider Factory
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
    return build_provider()


def build_provider() -> LLMProvider:
    """Construct a FRESH provider instance, bypassing the cache.

    The cached create_provider() holds a persistent httpx client that binds to
    the event loop it is first used on. Code that must run on a DIFFERENT loop
    (the Phase 14 browser runtime drives its LLM decisions on a dedicated
    Proactor loop — see app/core/browser_runtime.py) needs its own provider whose
    client binds to that loop; sharing the cached one would use an httpx pool
    across two loops. Such a caller builds one here and closes it (__aexit__)
    when done.
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
        # OpenRouter is OpenAI-compatible — the httpx client that targets the
        # standard /chat/completions path (NOT the Groq SDK, which bakes in
        # /openai/v1 and would 404 here).
        from app.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(
            api_key=settings.OPENROUTER_API_KEY,
            model=settings.OPENROUTER_MODEL,
            base_url="https://openrouter.ai/api/v1",
            provider_name="openrouter",
        )

    elif provider_name == "deepseek":
        # DeepSeek is OpenAI-compatible. Use the httpx client that POSTs to
        # {base_url}/chat/completions — the Groq SDK bakes in /openai/v1 and
        # would 404 against DeepSeek. Use deepseek-chat (V3); deepseek-reasoner
        # (R1) is a thinking model that would starve the classifier's 512-token cap.
        from app.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(
            api_key=settings.DEEPSEEK_API_KEY,
            model=settings.DEEPSEEK_MODEL,
            base_url=settings.DEEPSEEK_BASE_URL,
            provider_name="deepseek",
        )

    else:
        raise ValueError(
            f"Unknown LLM provider: '{provider_name}'. "
            f"Valid options: gemini, groq, ollama, openrouter, deepseek. "
            f"Check LLM_PROVIDER in your .env file."
        )


def reset_provider_cache() -> None:
    """Clear the provider cache — used in tests to swap providers."""
    create_provider.cache_clear()
