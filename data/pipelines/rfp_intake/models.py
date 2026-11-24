"""The RFP intake tables (briefing §2.3: Ticket, RFP metadata, DepartmentSection).

PostgreSQL (Supabase) is the source of truth, through the same SQLModel stack
as inventory: TinyDB and JSON files are not acceptable for these entities.
`store.initialize_schema` creates exactly these ten tables: the five of
Part 1, `rfp_section_drafts` and `rfp_proposal_handoffs` for Part 2, and
`rfp_approval_runs`, `rfp_approval_decisions` and `rfp_final_documents` for
Part 3. (The approval graph's checkpoints are LangGraph's own tables, in their
own schema: see `approval/checkpoint.py`.)

Portability: JSON columns are JSONB on PostgreSQL and JSON elsewhere, and every
check is a plain `IN (...)` list, so the offline tests run the same models on
SQLite. The briefing's Part 2/3 columns of `rfp_department_sections` existed
from the start (nullable). The columns Parts 2 and 3 added later
(`ADDED_SECTION_COLUMNS`) are added to an existing table by
`store.initialize_schema`, because `create_all` never alters a table that
already exists.

The tables have their own registry (`RfpModel.metadata`), not the global
`SQLModel.metadata` the inventory tables use: a bare `SQLModel.metadata.create_all()`
elsewhere never creates them by accident, and each domain initializes only its own.
"""

from datetime import date, datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
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
# A department section through Part 2 (our own vocabulary; the ticket keeps the
# briefing's statuses). `pending` = not drafted in the current run yet.
SECTION_DRAFT_STATUSES: Final = (
    "pending",
    "drafting",
    "under_evaluation",
    "passed",
    "needs_human_review",
    "failed",
)
# The section columns Part 2 added after Part 1's tables existed.
PART_2_SECTION_COLUMNS: Final = (
    "draft_status",
    "draft_iterations",
    "draft_data",
    "draft_run_id",
    "ready_at",
)
# The section columns Part 3 added (the briefing's `approver` / `approved_at` existed).
PART_3_SECTION_COLUMNS: Final = ("approved_by", "review_rounds")
ADDED_SECTION_COLUMNS: Final = PART_2_SECTION_COLUMNS + PART_3_SECTION_COLUMNS

# A department section through Part 3 (`approval_status`):
# - `pending`: waiting for its owner's decision;
# - `changes_requested`: being revised (asked by the owner or by an arbitration ruling);
# - `approved` / `rejected`: the owner's decision (or the arbiter's, for a rejection);
# - `withdrawn`: another section was rejected, so no decision is needed any more.
APPROVAL_STATUSES: Final = ("pending", "changes_requested", "approved", "rejected", "withdrawn")
APPROVAL_RUN_STATUSES: Final = ("running", "paused", "done", "rejected", "failed")
# What a decision does. `withdrawn` is only ever written by the system.
DECISION_ACTIONS: Final = ("approve", "request_changes", "reject", "withdrawn")
# Who decided: the department owner, an arbiter applying a §7 rule, or the system.
DECISION_SOURCES: Final = ("owner", "arbiter", "system")
# `recorded` by the API, `applied` once the graph has taken it, `stale` when the
# request it answered had moved on, `refused` when the graph's validation refused it.
DECISION_STATUSES: Final = ("recorded", "applied", "stale", "refused")


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


DRAFT_STATUS_CHECK: Final = (
    f"draft_status IS NULL OR {_one_of('draft_status', SECTION_DRAFT_STATUSES)}"
)
DRAFT_STATUS_CHECK_NAME: Final = "ck_rfp_section_draft_status"
APPROVAL_STATUS_CHECK: Final = (
    f"approval_status IS NULL OR {_one_of('approval_status', APPROVAL_STATUSES)}"
)
APPROVAL_STATUS_CHECK_NAME: Final = "ck_rfp_section_approval_status"
TICKET_STATUS_CHECK: Final = _one_of("status", [s.value for s in TicketStatus])
TICKET_STATUS_CHECK_NAME: Final = "ck_rfp_status"


class RfpTicket(RfpModel, table=True):
    """One upload. `id` is the briefing's `ticket_id`."""

    __tablename__ = "rfp_tickets"
    __table_args__ = (
        CheckConstraint(TICKET_STATUS_CHECK, name=TICKET_STATUS_CHECK_NAME),
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
        CheckConstraint(DRAFT_STATUS_CHECK, name=DRAFT_STATUS_CHECK_NAME),
        CheckConstraint(APPROVAL_STATUS_CHECK, name=APPROVAL_STATUS_CHECK_NAME),
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
    # `draft_content`: the latest draft as Markdown; `evaluation_results`: its
    # EvaluationResult. Every attempt is kept in `rfp_section_drafts`.
    draft_content: str | None = _text()
    evaluation_results: dict[str, Any] | None = _json()
    approval_status: str | None = Field(default=None, max_length=32)
    approver: str | None = Field(default=None, max_length=100)
    approved_at: datetime | None = _timestamp(nullable=True)
    created_at: datetime = _timestamp()
    # Added by Part 2 (`PART_2_SECTION_COLUMNS`).
    draft_status: str | None = Field(default=None, max_length=32)
    draft_iterations: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    # The structured draft the Markdown was rendered from, plus how it was made
    # (knowledge sources, guard notes, SLA designation, rate-card version).
    draft_data: dict[str, Any] | None = _json()
    draft_run_id: UUID | None = Field(default=None)
    # When the section left Part 2 for approval (starts the §3 approval-time KPI).
    # Part 3 resets it each time a revised section goes back to its owner.
    ready_at: datetime | None = _timestamp(nullable=True)
    # Added by Part 3 (`PART_3_SECTION_COLUMNS`). `approver` is the owner's name
    # (briefing §2.3); `approved_by` the account that signed in to decide.
    approved_by: str | None = Field(default=None, max_length=254)
    # Revisions in Part 3 so far (by the owner's request or an arbitration ruling).
    review_rounds: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )


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


class RfpSectionDraft(RfpModel, table=True):
    """One attempt of the generator-evaluator loop: the draft and its evaluation.

    The audit trail of Part 2 and the source of the "average iterations per
    section" KPI (briefing §3). Saving is an upsert on (section, run, iteration).
    """

    __tablename__ = "rfp_section_drafts"
    __table_args__ = (
        UniqueConstraint("section_id", "run_id", "iteration", name="uq_rfp_section_draft_attempt"),
        CheckConstraint(_one_of("department_id", DEPARTMENT_IDS), name="ck_rfp_draft_dept"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    ticket_id: UUID = Field(foreign_key="rfp_tickets.id", nullable=False, index=True)
    section_id: UUID = Field(foreign_key="rfp_department_sections.id", nullable=False, index=True)
    department_id: str = Field(max_length=16, nullable=False)
    run_id: UUID = Field(nullable=False)
    iteration: int = Field(nullable=False)
    draft_data: dict[str, Any] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    draft_content: str = Field(sa_column=Column(Text, nullable=False))
    guard_notes: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    evaluation: dict[str, Any] | None = _json()
    overall_pass: bool | None = Field(default=None)
    tokens: int = Field(default=0, nullable=False)
    created_at: datetime = _timestamp()
    evaluated_at: datetime | None = _timestamp(nullable=True)


class RfpProposalHandoff(RfpModel, table=True):
    """The Part 3 queue: every department's draft and EvaluationResult, for approval."""

    __tablename__ = "rfp_proposal_handoffs"
    __table_args__ = (
        CheckConstraint(_one_of("status", HANDOFF_STATUSES), name="ck_rfp_proposal_status"),
        Index("rfp_proposal_handoffs_pending_idx", "next_stage", "status"),
    )

    ticket_id: UUID = Field(primary_key=True, foreign_key="rfp_tickets.id")
    contract_version: str = Field(max_length=32, nullable=False)
    next_stage: str = Field(max_length=32, nullable=False)
    status: str = Field(default="pending", max_length=16, nullable=False)
    needs_human_review: bool = Field(default=False, nullable=False)
    payload: dict[str, Any] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    created_at: datetime = _timestamp()
    claimed_at: datetime | None = _timestamp(nullable=True)
    claimed_by: str | None = Field(default=None, max_length=100)


class RfpApprovalRun(RfpModel, table=True):
    """One ticket's Part 3 run: its checkpoint thread, the lease, and the arbitration log.

    The lease (`worker`, `lease_expires_at`) makes one process at a time resume
    the ticket's graph: two concurrent resumes of one thread would each start
    from the same checkpoint and lose each other's writes.
    """

    __tablename__ = "rfp_approval_runs"
    __table_args__ = (
        CheckConstraint(_one_of("status", APPROVAL_RUN_STATUSES), name="ck_rfp_approval_status"),
        UniqueConstraint("thread_id", name="uq_rfp_approval_thread"),
    )

    ticket_id: UUID = Field(primary_key=True, foreign_key="rfp_tickets.id")
    # The LangGraph checkpoint thread, `rfp-<ticket_id>`.
    thread_id: str = Field(max_length=80, nullable=False)
    graph_version: str = Field(max_length=32, nullable=False)
    status: str = Field(default="running", max_length=16, nullable=False)
    arbitration_rounds: int = Field(default=0, nullable=False)
    # Every §7 ruling, in order (trigger, arbiter, rule, departments, round, evidence).
    rulings: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    worker: str | None = Field(default=None, max_length=100)
    lease_expires_at: datetime | None = _timestamp(nullable=True)
    error_stage: str | None = Field(default=None, max_length=64)
    error_message: str | None = _text()
    started_at: datetime = _timestamp()
    updated_at: datetime = _timestamp()
    finished_at: datetime | None = _timestamp(nullable=True)


# One live owner decision per section and review round: a double click, or two
# tabs, cannot record two. Refused and stale decisions do not count.
_LIVE_OWNER_DECISION: Final = "source = 'owner' AND status IN ('recorded', 'applied')"


class RfpApprovalDecision(RfpModel, table=True):
    """Every decision on a section, append-only: the owner's, an arbiter's, or the system's."""

    __tablename__ = "rfp_approval_decisions"
    __table_args__ = (
        CheckConstraint(_one_of("department_id", DEPARTMENT_IDS), name="ck_rfp_decision_dept"),
        CheckConstraint(_one_of("action", DECISION_ACTIONS), name="ck_rfp_decision_action"),
        CheckConstraint(_one_of("source", DECISION_SOURCES), name="ck_rfp_decision_source"),
        CheckConstraint(_one_of("status", DECISION_STATUSES), name="ck_rfp_decision_status"),
        Index(
            "uq_rfp_decision_live_owner",
            "section_id",
            "review_round",
            unique=True,
            postgresql_where=sql_text(_LIVE_OWNER_DECISION),
            sqlite_where=sql_text(_LIVE_OWNER_DECISION),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    ticket_id: UUID = Field(foreign_key="rfp_tickets.id", nullable=False, index=True)
    section_id: UUID = Field(foreign_key="rfp_department_sections.id", nullable=False)
    department_id: str = Field(max_length=16, nullable=False)
    # The review round the decision answers (1 = Part 2's draft, then one per revision).
    review_round: int = Field(nullable=False)
    # The draft the decider saw: a decision never applies to a newer draft.
    draft_sha256: str | None = Field(default=None, max_length=64)
    action: str = Field(max_length=16, nullable=False)
    comment: str | None = _text()
    source: str = Field(max_length=16, nullable=False)
    # The owner's account email, or the arbiter's name, or "system".
    decided_by: str = Field(max_length=254, nullable=False)
    # The briefing's name for who decided (owner or arbiter), or "system".
    decided_by_name: str = Field(max_length=100, nullable=False)
    # The §7 trigger behind an arbiter's decision.
    trigger_id: str | None = Field(default=None, max_length=32)
    status: str = Field(default="recorded", max_length=16, nullable=False)
    status_note: str | None = _text()
    created_at: datetime = _timestamp(index=True)
    applied_at: datetime | None = _timestamp(nullable=True)


class RfpFinalDocument(RfpModel, table=True):
    """Briefing §2.3 "FinalDocument": `ticket_id`, `sections`, `currency`, `generated_at`.

    Written in the same transaction that moves the ticket to `done`, so a
    `done` ticket always has its document and the reverse.
    """

    __tablename__ = "rfp_final_documents"
    __table_args__ = (CheckConstraint("currency IN ('USD', 'EUR')", name="ck_rfp_final_currency"),)

    ticket_id: UUID = Field(primary_key=True, foreign_key="rfp_tickets.id")
    rfp_id: UUID = Field(nullable=False)
    client_name: str | None = Field(default=None, max_length=200)
    currency: str = Field(max_length=3, nullable=False)
    # One entry per approved department section, in the briefing's order.
    sections: list[dict[str, Any]] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    # The consolidated document, as Markdown.
    content: str = Field(sa_column=Column(Text, nullable=False))
    content_sha256: str = Field(max_length=64, nullable=False)
    approval_thread_id: str = Field(max_length=80, nullable=False)
    generated_at: datetime = _timestamp()


TABLES: Final = (
    RfpTicket,
    RfpMetadata,
    RfpDepartmentSection,
    RfpHandoff,
    RfpTicketEvent,
    RfpSectionDraft,
    RfpProposalHandoff,
    RfpApprovalRun,
    RfpApprovalDecision,
    RfpFinalDocument,
)
