"""RFP intake settings, from the environment or the root `.env` (see `.env.example`).

Read here rather than through `services.core.config`, because nothing in
`data/` imports `services/`. The model endpoint itself (`GENERATION_MODEL`,
`LLM_BASE_URL`, `LLM_API_KEY`) is the RAG settings' (`knowledge_base.RagSettings`).
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

from data.pipelines.rfp_intake.briefing import DEPARTMENT_IDS

ROOT_DIR: Final = Path(__file__).resolve().parents[3]
DEFAULT_UPLOAD_DIR: Final = ROOT_DIR / "data" / "raw" / "rfp"
_SCHEMA_NAME: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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

    # --- Part 2: proposal drafting and evaluation ---
    # Drafts per section before it ships as `needs_human_review` (briefing §3
    # targets fewer than 2 on average).
    max_draft_iterations: int = Field(default=3, ge=1, le=6)
    # Draft right after a successful intake (API background task).
    auto_draft: bool = True
    # How many queued handoffs one drain claims at most.
    draft_batch_size: int = Field(default=5, ge=1, le=50)
    # Readability of a section's prose: Flesch reading ease at least this...
    readability_min_ease: float = Field(default=30.0, ge=0, le=100)
    # ...and a Flesch-Kincaid grade at most this.
    readability_max_grade: float = Field(default=14.0, ge=1, le=30)
    # A second, model-based compliance reviewer that can only add violations.
    compliance_reviewer: bool = True
    # Knowledge-base passages per department query.
    knowledge_top_k: int = Field(default=4, ge=1, le=10)

    # --- Part 3: human approval per department and the final document ---
    # The account of each department's owner (briefing §2.1), as JSON:
    # {"warehouse": "ana.whitfield@trackflow.com", ...}. Only that account can
    # approve, reject or request changes on its department's section; nobody
    # decides on another's behalf (§6: peer owners, no approval ladder).
    approvers: dict[str, str] = Field(default_factory=dict)
    # Revisions one section may go through in Part 3 (requested by its owner or
    # forced by an arbitration ruling). At the limit the owner approves or rejects.
    max_revision_rounds: int = Field(default=3, ge=1, le=6)
    # Times the arbitration node (§7) may send sections back before an unresolved
    # conflict rejects the offending section.
    max_arbitration_rounds: int = Field(default=2, ge=1, le=5)
    # Open the approvals right after drafting (API background task).
    auto_request_approval: bool = True
    # PostgreSQL schema of the approval graph's checkpoint tables.
    checkpoint_schema: str = "rfp_graph"

    # --- Milestone 10: the live notification stream (`GET /rfp/notifications/stream`) ---
    # A `: keep-alive` comment this often, so proxies and the browser's watchdog
    # know the stream is alive while no ticket arrives.
    stream_heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    # How often the API's single watcher reads the feed while someone is
    # connected. An upload through this API wakes it at once; the poll catches
    # tickets registered by another process (the CLI, another worker).
    stream_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    # Replay after a reconnect (`Last-Event-ID`): at most this many events, none
    # older than this. Beyond either, the client is told to resync (refetch the list once).
    stream_replay_hours: int = Field(default=24, ge=1, le=24 * 7)
    stream_replay_limit: int = Field(default=200, ge=1, le=1000)
    # Events waiting for one slow client before it is disconnected (it then resumes by replay).
    stream_queue_size: int = Field(default=100, ge=1, le=10_000)
    # A stream ends normally after this long (or when its token expires, if sooner),
    # and the client resumes it at once: periodic re-authentication, bounded connections.
    stream_max_seconds: float = Field(default=900.0, gt=0, le=24 * 60 * 60)

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

    @field_validator("approvers", mode="after")
    @classmethod
    def known_departments_and_emails(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(DEPARTMENT_IDS))
        if unknown:
            raise ValueError(f"unknown department(s): {', '.join(unknown)}")
        cleaned = {dept: email.strip().lower() for dept, email in value.items()}
        invalid = sorted(dept for dept, email in cleaned.items() if not _EMAIL.match(email))
        if invalid:
            raise ValueError(f"not an email address for: {', '.join(invalid)}")
        return cleaned

    @field_validator("checkpoint_schema", mode="after")
    @classmethod
    def plain_schema_name(cls, value: str) -> str:
        if not _SCHEMA_NAME.match(value):
            raise ValueError("a lowercase SQL identifier, such as rfp_graph")
        return value

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_rfp_settings() -> RfpIntakeSettings:
    return RfpIntakeSettings()
