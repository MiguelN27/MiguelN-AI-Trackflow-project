"""Settings of the live notification stream (`LIVE_STREAM_*`, see `.env.example`)."""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.core.db import ROOT_DIR


class LiveStreamSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIVE_STREAM_",
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # A `: keep-alive` comment this often, so proxies and the browser's watchdog
    # know the stream is alive while nothing happens.
    heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    # How often the API's single watcher reads the outbox while someone is
    # connected. A write through this API wakes it at once; the poll catches
    # events written by another process (the CLI, another worker).
    poll_seconds: float = Field(default=2.0, gt=0, le=60)
    # Replay after a reconnect (`Last-Event-ID`): at most this many events, none
    # older than this. Beyond either, the client is told to resync.
    replay_hours: int = Field(default=24, ge=1, le=24 * 7)
    replay_limit: int = Field(default=200, ge=1, le=1000)
    # Events waiting for one slow client before it is disconnected (it then resumes by replay).
    queue_size: int = Field(default=100, ge=1, le=10_000)
    # A stream ends normally after this long (or when its token expires, if sooner),
    # and the client resumes it at once: periodic re-authentication, bounded connections.
    max_seconds: float = Field(default=900.0, gt=0, le=24 * 60 * 60)

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_default(cls, value: Any) -> Any:
        # Compose passes `${VAR:-}` through as an empty string: use the default.
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault()
        return value


@lru_cache(maxsize=1)
def get_live_stream_settings() -> LiveStreamSettings:
    return LiveStreamSettings()
