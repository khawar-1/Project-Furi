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

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2"

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

    # ------------------------------------------------------------------ Backend
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # ------------------------------------------------------------------ Voice
    WHISPER_MODEL: str = "base"
    PIPER_VOICE_PATH: str = "./voices/en_US-amy-medium.onnx"

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
