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

    # DeepSeek — OpenAI-compatible cloud API. DeepSeek retired the deepseek-chat
    # (V3) name on 2026-07-24; the V4 line is deepseek-v4-flash (fast/cheap,
    # non-reasoning — the drop-in) and deepseek-v4-pro (higher quality). Use a
    # NON-reasoning model: deepseek-reasoner (R1) / any thinking tier re-triggers
    # the classifier's empty-output problem (reasoning tokens eat the token
    # budget). Driven through the OpenAICompatProvider ({base_url}/chat/completions).
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"
    # HTTP timeout for Ollama calls — generous for a cold model load / long
    # planner generations on a local GPU (the 120s hardcoded default was tight).
    OLLAMA_TIMEOUT_SECONDS: int = 300

    # ------------------------------------------------------------------ Vision
    # The browser loop's vision channel (Phase 15.3, vision-first since 2026-07-21):
    # a SECOND, image-capable model that sees the page as a set-of-marks screenshot.
    # The primary provider (deepseek-chat) has no image input, so this is a
    # separate model behind app/providers/vision.py's seam. Off unless the user
    # opts in (browser_vision.config) AND a key is configured — otherwise the loop
    # stays DOM-only. VISION_API_KEY falls back to GEMINI_API_KEY so a user who
    # already has Gemini configured needs only to flip the toggle. The model must
    # accept image input (gemini-2.0-flash / Groq's Llama-4 do; a text-only model
    # fails the vision call and the loop degrades to DOM-only).
    VISION_PROVIDER: str = "gemini"
    VISION_API_KEY: str = ""
    VISION_MODEL: str = "gemini-2.0-flash"

    # MULTI-KEY ROTATION (2026-07-23). The vision channel rotates through EVERY
    # configured key: a key that hits its quota / rate limit is set cooling-down
    # and the next key is tried, and only when ALL keys are cooling does the loop
    # fall to DOM-text — so quota exhaustion NEVER fails a browse. Groq is tried
    # before Gemini (its free tier is far more generous and its Llama-4 models
    # accept images). Keys are comma-separated; the singular VISION_API_KEY /
    # GEMINI_API_KEY above still work (they extend the Gemini pool). Leaving all of
    # these blank keeps today's single-key behaviour.
    VISION_GROQ_API_KEYS: str = ""      # comma-separated Groq keys (tried first)
    VISION_GEMINI_API_KEYS: str = ""    # comma-separated Gemini keys (tried after Groq)
    VISION_GROQ_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    VISION_GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    # How long a key stays cooling after a quota/rate-limit/auth error before it is
    # re-probed. Long enough to stop hammering a daily-exhausted key; short enough
    # that a per-minute rate limit recovers within a session. Process-global, reset
    # on restart (quotas may have reset by then).
    VISION_KEY_COOLDOWN_SECONDS: int = 900

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

    # Optional. Google Programmable Search (Custom Search JSON API). When BOTH are
    # set, web_search PREFERS Google — its index is fresher than the aggregator
    # snippets Tavily/DDG return, which matters for "latest episode / newest / today"
    # facts (2026-07-25: Tavily reported Black Clover's latest as a stale 131 vs the
    # true 170). Falls through to Tavily → DuckDuckGo when unset or when Google
    # returns nothing. Free tier: 100 queries/day. Create a key at
    # https://developers.google.com/custom-search/v1/introduction and an engine
    # (set to "search the entire web") at https://programmablesearchengine.google.com
    # — CX is the engine id. Direct google.com/search scraping is deliberately NOT
    # used: it violates Google's ToS and is CAPTCHA-blocked for automated clients.
    GOOGLE_SEARCH_API_KEY: str = ""
    GOOGLE_SEARCH_CX: str = ""

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
