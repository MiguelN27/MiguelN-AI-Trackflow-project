"""The RFP intake tables (briefing §2.3: Ticket, RFP metadata, DepartmentSection).

PostgreSQL (Supabase) is the source of truth, through the same SQLModel stack
as inventory: TinyDB and JSON files are not acceptable for these entities.
`store.initialize_schema` creates exactly these five tables.

Portability: JSON columns are JSONB on PostgreSQL and JSON elsewhere, and every
check is a plain `IN (...)` list, so the offline tests run the same models on
SQLite. The Part 2/3 columns of `rfp_department_sections` exist from the start
(nullable), because `create_all` never alters a table that already exists.

The tables have their own registry (`RfpModel.metadata`), not the global
`SQLModel.metadata` the inventory tables use: a bare `SQLModel.metadata.create_all()`
elsewhere never creates them by accident, and each domain initializes only its own.
"""

from datetime import date, datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

from data.pipelines.rfp_intake.briefing import (
    COUNTRIES,
    DEPARTMENT_IDS,
    TicketStatus,
)

JSON_TYPE: Final = JSON().with_variant(JSONB(), "postgresql")

HANDOFF_STATUSES: Final = ("pending", "claimed")
EVENT_OUTCOMES: Final = ("completed", "failed", "decision", "warning")
PROCESSING_COST_TIERS: Final = ("low", "medium", "high")


class RfpModel(SQLModel, registry=registry()):
    """Base of the RFP intake tables: their own metadata, `RfpModel.metadata`."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _one_of(column: str, values: tuple[str, ...] | list[str]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


def _timestamp(*, nullable: bool = False, index: bool = False) -> Any:
    column = Column(DateTime(timezone=True), nullable=nullable, index=index)
    if nullable:
        return Field(default=None, sa_column=column)
    return Field(default_factory=utcnow, sa_column=column)


def _json(*, nullable: bool = True) -> Any:
    return Field(default=None, sa_column=Column(JSON_TYPE, nullable=nullable))


def _text() -> Any:
    return Field(default=None, sa_column=Column(Text, nullable=True))


class RfpTicket(RfpModel, table=True):
    """One upload. `id` is the briefing's `ticket_id`."""

    __tablename__ = "rfp_tickets"
    __table_args__ = (
        CheckConstraint(_one_of("status", [s.value for s in TicketStatus]), name="ck_rfp_status"),
        UniqueConstraint("rfp_id", name="uq_rfp_tickets_rfp_id"),
        Index("rfp_tickets_status_idx", "status"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    rfp_id: UUID = Field(default_factory=uuid4, nullable=False)
    status: str = Field(max_length=32, nullable=False)
    # The graph node running now (or the last one that ran): live progress.
    current_stage: str | None = Field(default=None, max_length=64)
    original_filename: str = Field(max_length=255, nullable=False)
    raw_pdf_path: str = Field(max_length=500, nullable=False)
    pdf_sha256: str = Field(max_length=64, nullable=False)
    pdf_bytes: int = Field(nullable=False)
    # The TinyDB account UUID of the uploader (no SQL foreign key, like inventory).
    uploaded_by: str = Field(max_length=64, nullable=False)
    # The converted document, so later parts never re-parse the PDF.
    markdown: str | None = _text()
    markdown_truncated: bool = Field(default=False, nullable=False)
    classification: dict[str, Any] | None = _json()
    discard_reason: str | None = _text()
    error_stage: str | None = Field(default=None, max_length=64)
    error_message: str | None = _text()
    sales_brief: dict[str, Any] | None = _json()
    llm_tokens_used: int = Field(default=0, nullable=False)
    created_at: datetime = _timestamp(index=True)
    updated_at: datetime = _timestamp()
    completed_at: datetime | None = _timestamp(nullable=True)


class RfpMetadata(RfpModel, table=True):
    """Briefing §2.3 "RFP metadata", plus the evidence and the readability metrics."""

    __tablename__ = "rfp_metadata"
    __table_args__ = (
        UniqueConstraint("ticket_id", name="uq_rfp_metadata_ticket_id"),
        CheckConstraint(
            f"client_country IS NULL OR {_one_of('client_country', COUNTRIES)}",
            name="ck_rfp_metadata_country",
        ),
        CheckConstraint(
            "currency IS NULL OR currency IN ('USD', 'EUR')", name="ck_rfp_metadata_currency"
        ),
        CheckConstraint(
            _one_of("processing_cost", PROCESSING_COST_TIERS), name="ck_rfp_metadata_cost"
        ),
    )

    rfp_id: UUID = Field(primary_key=True, foreign_key="rfp_tickets.rfp_id")
    ticket_id: UUID = Field(foreign_key="rfp_tickets.id", nullable=False)
    client_name: str | None = Field(default=None, max_length=200)
    client_country: str | None = Field(default=None, max_length=2)
    currency: str | None = Field(default=None, max_length=3)
    services_requested: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    monthly_volume: int | None = Field(default=None)
    deadline: date | None = Field(default=None)
    # The deadline as the document words it ("around August 24th"), kept even
    # when no full date can be read from it.
    deadline_text: str | None = _text()
    budget_range: str | None = _text()
    departments_needed: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    # {field: verbatim quote} for every value above that came from the document.
    evidence: dict[str, Any] | None = _json()
    # Readability metrics (briefing §2.3), to anticipate processing cost.
    language: str | None = Field(default=None, max_length=16)
    word_count: int = Field(default=0, nullable=False)
    sentence_count: int = Field(default=0, nullable=False)
    char_count: int = Field(default=0, nullable=False)
    estimated_tokens: int = Field(default=0, nullable=False)
    flesch_reading_ease: float | None = Field(default=None)
    flesch_kincaid_grade: float | None = Field(default=None)
    gunning_fog: float | None = Field(default=None)
    readability_note: str | None = _text()
    processing_cost: str = Field(default="low", max_length=8, nullable=False)
    created_at: datetime = _timestamp()


class RfpDepartmentSection(RfpModel, table=True):
    """Briefing §2.3 "DepartmentSection": `key_aspects` in Part 1, the rest later."""

    __tablename__ = "rfp_department_sections"
    __table_args__ = (
        UniqueConstraint("ticket_id", "department_id", name="uq_rfp_section_department"),
        CheckConstraint(_one_of("department_id", DEPARTMENT_IDS), name="ck_rfp_section_dept"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    ticket_id: UUID = Field(foreign_key="rfp_tickets.id", nullable=False, index=True)
    department_id: str = Field(max_length=16, nullable=False)
    owner: str = Field(max_length=100, nullable=False)
    objective: str | None = _text()
    # The department-relevant extracts the worker was given (verbatim quotes).
    extracts: list[str] = Field(default_factory=list, sa_column=Column(JSON_TYPE, nullable=False))
    key_aspects: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    needed_from_department: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    open_questions: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    # What the deterministic guards dropped or corrected, and why.
    validation_notes: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    # Part 2 (drafting, evaluation) and Part 3 (approval). Empty in Part 1.
    draft_content: str | None = _text()
    evaluation_results: dict[str, Any] | None = _json()
    approval_status: str | None = Field(default=None, max_length=32)
    approver: str | None = Field(default=None, max_length=100)
    approved_at: datetime | None = _timestamp(nullable=True)
    created_at: datetime = _timestamp()


class RfpHandoff(RfpModel, table=True):
    """The routing record Part 2 picks up: `ticket_id` + the synthesizer payload."""

    __tablename__ = "rfp_handoffs"
    __table_args__ = (
        CheckConstraint(_one_of("status", HANDOFF_STATUSES), name="ck_rfp_handoff_status"),
        Index("rfp_handoffs_pending_idx", "next_stage", "status"),
    )

    ticket_id: UUID = Field(primary_key=True, foreign_key="rfp_tickets.id")
    contract_version: str = Field(max_length=32, nullable=False)
    next_stage: str = Field(max_length=32, nullable=False)
    status: str = Field(default="pending", max_length=16, nullable=False)
    payload: dict[str, Any] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    created_at: datetime = _timestamp()
    claimed_at: datetime | None = _timestamp(nullable=True)
    claimed_by: str | None = Field(default=None, max_length=100)


class RfpTicketEvent(RfpModel, table=True):
    """One step of a run: a node finishing or failing, or a routing decision."""

    __tablename__ = "rfp_ticket_events"
    __table_args__ = (
        CheckConstraint(_one_of("outcome", EVENT_OUTCOMES), name="ck_rfp_event_outcome"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    ticket_id: UUID = Field(foreign_key="rfp_tickets.id", nullable=False, index=True)
    node: str = Field(max_length=64, nullable=False)
    outcome: str = Field(max_length=16, nullable=False)
    detail: dict[str, Any] | None = _json()
    duration_ms: int | None = Field(default=None)
    created_at: datetime = _timestamp(index=True)


TABLES: Final = (RfpTicket, RfpMetadata, RfpDepartmentSection, RfpHandoff, RfpTicketEvent)
