from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration. Invalid or missing values fail at startup."""

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    jwt_secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=60, gt=0)

    # Password reset link lifetime. AUTH-03 fixes the window at 15-60 minutes,
    # so a value outside it is a configuration error, not a preference.
    password_reset_token_expire_minutes: int = Field(default=30, ge=15, le=60)

    # Transactional email through Resend. Leaving the key unset selects the
    # console backend, which logs the message instead of sending it, so the
    # reset flow stays exercisable locally without an account.
    resend_api_key: str | None = None
    email_from: str = "TrackFlow <onboarding@resend.dev>"

    # Where the reset link points. The token is appended as `?token=<jwt>`.
    frontend_base_url: str = "http://localhost:3000"

    @field_validator("frontend_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def email_is_live(self) -> bool:
        """False when no provider key is configured and mail is only logged."""
        return bool(self.resend_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
