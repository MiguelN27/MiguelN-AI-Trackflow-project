"""The handoff to Part 2: `ticket_id` + the synthesizer payload, in a queue table.

Routing (ticket: "queue flag, DB field, or documented handoff contract - no
second API"): when an intake completes, `complete_intake` writes one
`rfp_handoffs` row with `next_stage = "drafting"`, `status = "pending"` and
an `IntakeHandoff` payload. It holds everything the proposal generators need -
client, currency, scope, and per department the owner, key aspects, needs and
open questions - so Part 2 starts without re-parsing the PDF.

Part 2 takes work with `claim_next`, which locks one pending row with
`FOR UPDATE SKIP LOCKED` (two workers never claim the same ticket), marks it
claimed and moves the ticket to `drafting`. The contract and its JSON Schema
are documented in `docs/rfp/handoff-contract.md` (kept in sync by a test).
"""

from datetime import date, datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.pipelines.rfp_intake.briefing import (
    Country,
    Currency,
    DepartmentId,
    Service,
    TicketStatus,
)
from data.pipelines.rfp_intake.models import RfpHandoff, RfpTicket, utcnow
from data.pipelines.rfp_intake.store import as_utc, as_uuid

CONTRACT_VERSION: Final = "rfp-intake/1"
NEXT_STAGE: Final = "drafting"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HandoffClient(_Contract):
    name: str | None
    country: Country | None
    currency: Currency | None = Field(description="USD for US operations, EUR for Spain (§5)")


class HandoffKeyAspect(_Contract):
    aspect: str
    detail: str
    source_quote: str = Field(description="Verbatim passage of the RFP or its metadata")


class HandoffDepartment(_Contract):
    department_id: DepartmentId
    department_name: str
    owner: str = Field(description="The department owner who approves this section in Part 3")
    objective: str
    ask: str = Field(description="What Sales asks the owner to provide")
    key_aspects: list[HandoffKeyAspect]
    needed_from_department: list[str]
    open_questions: list[str]


class HandoffBrief(_Contract):
    summary: str
    client_questions: list[str]
    risks: list[str]
    unassigned_requirements: list[str]


class HandoffReadability(_Contract):
    word_count: int
    estimated_tokens: int
    flesch_kincaid_grade: float | None
    processing_cost: Literal["low", "medium", "high"]


class HandoffSource(_Contract):
    raw_pdf_path: str
    pdf_sha256: str
    markdown_chars: int
    markdown_truncated: bool


class IntakeHandoff(_Contract):
    """What an RFP intake hands to Part 2 (proposal drafting)."""

    contract_version: Literal["rfp-intake/1"] = CONTRACT_VERSION
    next_stage: Literal["drafting"] = NEXT_STAGE
    ticket_id: UUID
    rfp_id: UUID
    client: HandoffClient
    services_requested: list[Service]
    monthly_volume: int | None = Field(description="Orders per month; null when the RFP omits it")
    deadline: date | None
    deadline_text: str | None
    budget_range: str | None
    departments: list[HandoffDepartment] = Field(min_length=1)
    sales_brief: HandoffBrief
    readability: HandoffReadability
    source: HandoffSource
    created_at: datetime


def build_handoff(
    *,
    ticket: RfpTicket,
    metadata: dict[str, Any],
    readability: dict[str, Any],
    sections: list[dict[str, Any]],
    brief: dict[str, Any],
) -> IntakeHandoff:
    asks = {item["department_id"]: item["ask"] for item in brief.get("departments", [])}
    deadline = metadata.get("deadline")
    return IntakeHandoff(
        ticket_id=ticket.id,
        rfp_id=ticket.rfp_id,
        client=HandoffClient(
            name=metadata.get("client_name"),
            country=metadata.get("client_country"),
            currency=metadata.get("currency"),
        ),
        services_requested=list(metadata.get("services_requested") or []),
        monthly_volume=metadata.get("monthly_volume"),
        deadline=date.fromisoformat(deadline) if isinstance(deadline, str) else deadline,
        deadline_text=metadata.get("deadline_text"),
        budget_range=metadata.get("budget_range"),
        departments=[
            HandoffDepartment(
                department_id=section["department_id"],
                department_name=section["department_name"],
                owner=section["owner"],
                objective=section.get("objective") or "",
                ask=asks.get(section["department_id"], ""),
                key_aspects=[HandoffKeyAspect(**aspect) for aspect in section["key_aspects"]],
                needed_from_department=section.get("needed_from_department") or [],
                open_questions=section.get("open_questions") or [],
            )
            for section in sections
        ],
        sales_brief=HandoffBrief(
            summary=brief["summary"],
            client_questions=brief.get("client_questions") or [],
            risks=brief.get("risks") or [],
            unassigned_requirements=brief.get("unassigned_requirements") or [],
        ),
        readability=HandoffReadability(
            word_count=readability["word_count"],
            estimated_tokens=readability["estimated_tokens"],
            flesch_kincaid_grade=readability.get("flesch_kincaid_grade"),
            processing_cost=readability["processing_cost"],
        ),
        source=HandoffSource(
            raw_pdf_path=ticket.raw_pdf_path,
            pdf_sha256=ticket.pdf_sha256,
            markdown_chars=len(ticket.markdown or ""),
            markdown_truncated=ticket.markdown_truncated,
        ),
        created_at=utcnow(),
    )


def json_schema() -> dict[str, Any]:
    return IntakeHandoff.model_json_schema()


def load_handoff(engine: Engine, ticket_id: UUID | str) -> IntakeHandoff | None:
    with Session(engine) as session:
        row = session.get(RfpHandoff, as_uuid(ticket_id))
        return IntakeHandoff.model_validate(row.payload) if row else None


class ClaimedHandoff(BaseModel):
    handoff: IntakeHandoff
    claimed_at: datetime
    claimed_by: str


def _claim_row(session: Session, row: RfpHandoff, claimed_by: str) -> ClaimedHandoff:
    """Mark `row` claimed and move its ticket from `intake_complete` to `drafting`."""
    now = utcnow()
    row.status = "claimed"
    row.claimed_at = now
    row.claimed_by = claimed_by[:100]
    session.execute(
        update(RfpTicket)
        .where(
            col(RfpTicket.id) == row.ticket_id,
            col(RfpTicket.status) == TicketStatus.INTAKE_COMPLETE.value,
        )
        .values(status=TicketStatus.DRAFTING.value, current_stage="queued", updated_at=now)
    )
    handoff = IntakeHandoff.model_validate(row.payload)
    session.commit()
    return ClaimedHandoff(handoff=handoff, claimed_at=as_utc(now) or now, claimed_by=claimed_by)


def claim_next(
    engine: Engine, *, claimed_by: str, next_stage: str = NEXT_STAGE
) -> ClaimedHandoff | None:
    """The oldest pending handoff for `next_stage`, claimed; None when the queue is empty.

    The ticket moves from `intake_complete` to `drafting` in the same
    transaction. On PostgreSQL the row is locked with SKIP LOCKED, so
    concurrent claimers get different tickets.
    """
    with Session(engine) as session:
        row = session.execute(
            select(RfpHandoff)
            .where(col(RfpHandoff.status) == "pending", col(RfpHandoff.next_stage) == next_stage)
            .order_by(col(RfpHandoff.created_at))
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _claim_row(session, row, claimed_by)


def claim(engine: Engine, ticket_id: UUID | str, *, claimed_by: str) -> ClaimedHandoff | None:
    """Claim one ticket's pending handoff; None when it has none pending (or it is locked)."""
    with Session(engine) as session:
        row = session.execute(
            select(RfpHandoff)
            .where(
                col(RfpHandoff.ticket_id) == as_uuid(ticket_id),
                col(RfpHandoff.status) == "pending",
                col(RfpHandoff.next_stage) == NEXT_STAGE,
            )
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _claim_row(session, row, claimed_by)
