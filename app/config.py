from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str
    telegram_bot_username: str | None = None
    telegram_update_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: str | None = Field(default=None, min_length=16)
    telegram_proxy_url: str | None = None

    public_base_url: str
    database_path: str = "data/notifier.sqlite3"
    admin_telegram_user_ids: str = ""
    link_code_ttl_minutes: int = Field(default=60, ge=5, le=10080)
    max_webhook_bytes: int = Field(default=2_097_152, ge=1024, le=20_971_520)
    log_level: str = "INFO"

    @field_validator("telegram_proxy_url")
    @classmethod
    def validate_proxy(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith("socks5://"):
            raise ValueError("TELEGRAM_PROXY_URL must start with socks5://")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must start with https://")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_webhook_mode(self):
        if self.telegram_update_mode == "webhook" and not self.telegram_webhook_secret:
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET is required in Telegram webhook mode"
            )
        return self

    @property
    def admin_ids(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.admin_telegram_user_ids.split(",")
            if value.strip()
        }

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/telegram"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
