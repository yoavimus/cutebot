"""Application configuration (env-driven). See ``env.example``."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config. Never hard-code secrets — they live in the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8002

    # Database (async SQLAlchemy URL)
    database_url: str = "sqlite+aiosqlite:///./cutebot.db"

    # AI — single agent via LiteLLM (Claude by default)
    default_llm_model: str = "anthropic/claude-sonnet-4-6"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # LLM reliability (passed to LiteLLM; see app/llm.py)
    llm_timeout_s: int = 60
    llm_num_retries: int = 2
    llm_max_tokens: int = 2000

    # Telegram review channel
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_base: str = ""
    # Secret token echoed by Telegram on webhook calls — validated server-side.
    telegram_webhook_secret: str = "cutebot-webhook-secret"

    # Pipeline tuning
    batch_size: int = 5
    generation_cron: str = "0 9 * * *"
    posting_slots: str = "12:00,18:00"
    brand_file: str = "brand.yaml"

    # Image-first generation (stock library + bilingual captioning)
    stock_images_dir: str = "stock"
    primary_language: str = "he"
    secondary_languages: str = "en"
    post_disclaimer: str = "🤖 מאת CuteBot · by CuteBot"

    # Publishers (stubs in v1)
    instagram_access_token: str = ""
    tiktok_access_token: str = ""
    x_api_key: str = ""
    x_api_secret: str = ""

    @property
    def is_dev(self) -> bool:
        return self.app_env in {"development", "test"}

    @property
    def posting_slots_list(self) -> list[tuple[int, int]]:
        """Parse ``POSTING_SLOTS`` ("12:00,18:00") into [(12, 0), (18, 0)]."""
        slots: list[tuple[int, int]] = []
        for raw in self.posting_slots.split(","):
            raw = raw.strip()
            if not raw:
                continue
            hh, _, mm = raw.partition(":")
            slots.append((int(hh), int(mm or 0)))
        return slots

    @property
    def secondary_languages_list(self) -> list[str]:
        """Parse ``SECONDARY_LANGUAGES`` ("en" or "en,fr") into ["en"] / ["en", "fr"]."""
        return [lang.strip() for lang in self.secondary_languages.split(",") if lang.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
