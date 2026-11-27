"""Settings of the operational inactivity monitor (`OPS_*`, see `.env.example`)."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.core.db import ROOT_DIR

DEFAULT_RULES_PATH = ROOT_DIR / "config" / "operational-activity-rules.json"


class OperationsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPS_",
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The versioned rules file (relative paths are from the repository root).
    activity_rules_path: Path = DEFAULT_RULES_PATH
    # Run the monitor inside the API (a background task). Off in the unit tests.
    monitor_enabled: bool = True
    # How often the rules are evaluated. An alert opens at most this long after
    # its expected period has passed.
    monitor_interval_seconds: float = Field(default=60.0, ge=1, le=3600)

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_default(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault()
        return value

    @field_validator("activity_rules_path", mode="after")
    @classmethod
    def relative_to_repository(cls, value: Path) -> Path:
        return value if value.is_absolute() else ROOT_DIR / value


@lru_cache(maxsize=1)
def get_operations_settings() -> OperationsSettings:
    return OperationsSettings()
