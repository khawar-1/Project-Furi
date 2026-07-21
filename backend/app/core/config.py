"""
Jarvis OS — Application Configuration
Single source of truth for all environment-driven settings.
Uses Pydantic Settings for automatic env-var loading and validation.
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ App
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------ LLM
    LLM_PROVIDER: str = "gemini"

    # Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # Groq
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "anthropic/claude-3.5-sonnet"

    # DeepSeek — OpenAI-compatible cloud API. Use MODEL=deepseek-chat (V3, a
    # non-reasoning model); deepseek-reasoner (R1) would re-trigger the
    # classifier's empty-output problem on thinking models. Driven through the
    # shared GroqProvider base_url path (the Groq SDK is OpenAI-compatible).
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"
    # HTTP timeout for Ollama calls — generous for a cold model load / long
    # planner generations on a local GPU (the 120s hardcoded default was tight).
    OLLAMA_TIMEOUT_SECONDS: int = 300

    # ------------------------------------------------------------------ Vision
    # The browser loop's DOM-first vision fallback (Phase 15.3): a SECOND,
    # image-capable model used ONLY when the observe→decide→act loop is stuck on
    # a page whose target has no usable DOM text (icon-only buttons, canvas apps).
    # The primary provider (deepseek-chat) has no image input, so this is a
    # separate model behind app/providers/vision.py's seam. Off unless the user
    # opts in (browser_vision.config) AND a key is configured — otherwise the loop
    # stays DOM-only. VISION_API_KEY falls back to GEMINI_API_KEY so a user who
    # already has Gemini configured needs only to flip the toggle. The model must
    # accept image input (gemini-2.0-flash does; a text-only model would fail the
    # vision call and the loop degrades to DOM-only).
    VISION_PROVIDER: str = "gemini"
    VISION_API_KEY: str = ""
    VISION_MODEL: str = "gemini-2.0-flash"

    # ------------------------------------------------------------------ Identity resolution / memory
    # Minimum fuzzy score for a contact-name match to count as a candidate
    IDENTITY_MIN_SCORE: int = 81
    # Minimum score gap between top two candidates before asking the user
    IDENTITY_MIN_GAP: int = 8
    # Cosine similarity above which two semantic facts are considered duplicates
    SEMANTIC_DEDUP_THRESHOLD: float = 0.92

    # ------------------------------------------------------------------ Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./jarvis.db"

    # ------------------------------------------------------------------ Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "jarvis_memory"

    # ------------------------------------------------------------------ Google integration (Phase 5)
    # OAuth client for the installed-app loopback flow. Create a "Desktop app"
    # OAuth client in Google Cloud Console and paste its id/secret here.
    # (For installed apps Google documents the client secret as
    # non-confidential, but it still only ever lives in .env / Settings.)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Where the OAuth token lands. Empty = ~/.jarvis/google_token.json —
    # outside the repo, never committed, never logged.
    GOOGLE_TOKEN_PATH: str = ""

    # ------------------------------------------------------------------ Backend
    # Loopback only — 0.0.0.0 would expose the tool system (file deletion,
    # shell commands) to the whole network. Requests are additionally gated by
    # a static auth token (app/core/auth.py): loopback is not authorization —
    # any local process or a webpage firing POSTs at localhost could otherwise
    # drive the API.
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    # Empty = the backend generates/persists one at ~/.jarvis/auth_token.
    # Set explicitly only for scripted/dev use; never commit a real value.
    API_AUTH_TOKEN: str = ""

    # ------------------------------------------------------------------ Web search (Phase 6)
    # Optional. When set, web_search uses the Tavily API (purpose-built for LLM
    # agents — returns clean extracted page content, not just links) as the
    # primary provider, falling back to the keyless DuckDuckGo scrapers if
    # Tavily errors or returns nothing. Blank = DuckDuckGo only (the default —
    # a fresh clone works with no signup). Get a free key at https://tavily.com.
    TAVILY_API_KEY: str = ""

    # ------------------------------------------------------------------ Voice
    WHISPER_MODEL: str = "base"

    # ------------------------------------------------------------------ Supabase (optional)
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def is_qdrant_enabled(self) -> bool:
        return bool(self.QDRANT_HOST)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Singleton instance used across the application
settings: Settings = get_settings()
