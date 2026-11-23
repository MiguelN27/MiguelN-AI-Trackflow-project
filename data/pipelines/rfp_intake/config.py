"""RFP intake settings, from the environment or the root `.env` (see `.env.example`).

Read here rather than through `services.core.config`, because nothing in
`data/` imports `services/`. The model endpoint itself (`GENERATION_MODEL`,
`LLM_BASE_URL`, `LLM_API_KEY`) is the RAG settings' (`knowledge_base.RagSettings`).
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR: Final = Path(__file__).resolve().parents[3]
DEFAULT_UPLOAD_DIR: Final = ROOT_DIR / "data" / "raw" / "rfp"


class RfpIntakeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RFP_",
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Uploaded PDFs: a runtime artifact of intake (briefing §2.4), git-ignored.
    upload_dir: Path = DEFAULT_UPLOAD_DIR
    max_upload_mb: int = Field(default=20, ge=1, le=100)
    # The intake runs in the background, so it can wait out the gateway's stalls.
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    stale_after_minutes: int = Field(default=30, ge=1, le=24 * 60)
    max_document_chars: int = Field(default=60_000, ge=1_000, le=1_000_000)

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_default(cls, value: Any) -> Any:
        # Compose passes `${VAR:-}` through as an empty string: use the default.
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault()
        return value

    @field_validator("upload_dir", mode="after")
    @classmethod
    def relative_to_repository(cls, value: Path) -> Path:
        return value if value.is_absolute() else ROOT_DIR / value

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_rfp_settings() -> RfpIntakeSettings:
    return RfpIntakeSettings()
