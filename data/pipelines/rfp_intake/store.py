"""Every read and write of the RFP intake tables. Each function takes the engine.

The API and the CLI (`scripts/rfp_intake.py`, its own process) both pass
`services/inventory/database.get_engine`; tests pass SQLite. Status changes are
conditional updates: a step that finds its ticket
no longer in the status its graph works on (`analyzing` for Part 1, `drafting`
or `under_evaluation` for Part 2; swept as abandoned, or reset) raises
`TicketStateError` instead of overwriting the newer state.

Part 2's own writes are in `proposal/store.py`; this module keeps what both
parts share (schema, stage, events, failure, sweep, reads).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.notifications import outbox
from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID, DEPARTMENT_IDS, TicketStatus
from data.pipelines.rfp_intake.models import (
    ADDED_SECTION_COLUMNS,
    APPROVAL_STATUS_CHECK,
    APPROVAL_STATUS_CHECK_NAME,
    APPROVAL_STATUSES,
    DRAFT_STATUS_CHECK,
    DRAFT_STATUS_CHECK_NAME,
    SECTION_DRAFT_STATUSES,
    TABLES,
    TICKET_STATUS_CHECK,
    TICKET_STATUS_CHECK_NAME,
    RfpDepartmentSection,
    RfpHandoff,
    RfpMetadata,
    RfpModel,
    RfpProposalHandoff,
    RfpSectionDraft,
    RfpTicket,
    RfpTicketEvent,
    utcnow,
)

ANALYZING: Final = TicketStatus.ANALYZING.value
ANALYZING_ONLY: Final = frozenset({ANALYZING})
# The statuses the Part 2 graphs work on.
PART_2_ACTIVE: Final = frozenset({TicketStatus.DRAFTING.value, TicketStatus.UNDER_EVALUATION.value})
# A ticket left in one of these by a dead process is closed by the sweep.
IN_PROGRESS: Final = ANALYZING_ONLY | PART_2_ACTIVE
REPROCESSABLE: Final = frozenset({TicketStatus.FAILED.value, TicketStatus.DISCARDED.value})
LIST_LIMIT: Final = 50
ABANDONED_STAGE: Final = "abandoned"
# The live stream's event for a new ticket (`live_notifications.event`).
TICKET_CREATED_EVENT: Final = "rfp_ticket_created"


class TicketNotFound(LookupError):
    """No ticket has this id."""


class TicketStateError(RuntimeError):
    """The ticket is not in the status this step needs. The message is safe to show."""


def as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; they were written in UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


# --- Schema -------------------------------------------------------------------------


_SECTIONS_TABLE: Final = str(RfpDepartmentSection.__tablename__)
_TICKETS_TABLE: Final = str(RfpTicket.__tablename__)

# (table, constraint, expression, values it must allow). On PostgreSQL each is
# added when missing and replaced when it lacks a value added later, such as
# the `rejected` ticket status of Part 3. A table `create_all` just built
# already has them, from the models.
_CHECKS: Final = (
    (_SECTIONS_TABLE, DRAFT_STATUS_CHECK_NAME, DRAFT_STATUS_CHECK, SECTION_DRAFT_STATUSES),
    (_SECTIONS_TABLE, APPROVAL_STATUS_CHECK_NAME, APPROVAL_STATUS_CHECK, APPROVAL_STATUSES),
    (
        _TICKETS_TABLE,
        TICKET_STATUS_CHECK_NAME,
        TICKET_STATUS_CHECK,
        tuple(status.value for status in TicketStatus),
    ),
)


def _add_section_columns(connection: Any) -> None:
    """Add the Part 2 and Part 3 section columns to a table created before they existed.

    `create_all` never alters a table, so a database initialized by an earlier
    part gets them here (as inventory's `products.client_id`). Each type is
    compiled by the connection's dialect (JSONB and UUID on PostgreSQL).
    """
    table = RfpModel.metadata.tables[_SECTIONS_TABLE]
    present = {column["name"] for column in inspect(connection).get_columns(_SECTIONS_TABLE)}
    postgres = connection.dialect.name == "postgresql"
    for name in ADDED_SECTION_COLUMNS:
        if name in present:
            continue
        column = table.c[name]
        ddl = f"{name} {column.type.compile(dialect=connection.dialect)}"
        if column.server_default is not None:
            ddl += f" NOT NULL DEFAULT {column.server_default.arg}"  # type: ignore[attr-defined]
        exists = "IF NOT EXISTS " if postgres else ""
        connection.execute(text(f"ALTER TABLE {_SECTIONS_TABLE} ADD COLUMN {exists}{ddl}"))


def _ensure_checks(connection: Any) -> None:
    """PostgreSQL only: every `_CHECKS` constraint present and allowing all its values."""
    for table, name, expression, values in _CHECKS:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST(:table AS regclass) AND conname = :name"
            ),
            {"table": table, "name": name},
        ).scalar_one_or_none()
        if definition is not None and all(f"'{value}'" in definition for value in values):
            continue
        if definition is not None:
            connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
        connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"))


def initialize_schema(engine: Engine) -> None:
    """Create the ten tables when missing, with RLS on and no policies (as inventory).

    Initialization, not migration: `create_all` never alters an existing table,
    so the later parts' section columns and check values are added separately.
    The shared live notification outbox, where new tickets are announced, is
    created too.
    """
    tables = [RfpModel.metadata.tables[str(model.__tablename__)] for model in TABLES]
    with engine.begin() as connection:
        RfpModel.metadata.create_all(connection, tables=tables)
        _add_section_columns(connection)
        if connection.dialect.name == "postgresql":
            _ensure_checks(connection)
            quote = connection.dialect.identifier_preparer.quote
            for table in tables:
                connection.execute(
                    text(f"ALTER TABLE {quote(table.name)} ENABLE ROW LEVEL SECURITY")
                )
    outbox.initialize_schema(engine)


# --- Ticket lifecycle -----------------------------------------------------------------


def create_ticket(
    engine: Engine,
    *,
    ticket_id: UUID,
    original_filename: str,
    raw_pdf_path: str,
    pdf_sha256: str,
    pdf_bytes: int,
    uploaded_by: str,
) -> RfpTicket:
    """Register a ticket (`analyzing`) and its `rfp_ticket_created` announcement, atomically.

    Both rows commit together or not at all, whichever process registers the
    ticket, so the live stream announces exactly the tickets that exist. The
    announcement goes to the shared outbox (`outbox.append`), which serializes
    its writers until their commit, so a later `seq` is never visible before an
    earlier one.
    """
    ticket = RfpTicket(
        id=ticket_id,
        status=ANALYZING,
        current_stage="queued",
        original_filename=original_filename,
        raw_pdf_path=raw_pdf_path,
        pdf_sha256=pdf_sha256,
        pdf_bytes=pdf_bytes,
        uploaded_by=uploaded_by,
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add(ticket)
        session.flush()
        outbox.append(
            session,
            event=TICKET_CREATED_EVENT,
            subject=str(ticket.id),
            payload=new_ticket_summary(ticket),
        )
        session.commit()
    return ticket


def get_ticket(engine: Engine, ticket_id: UUID | str) -> RfpTicket | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.get(RfpTicket, as_uuid(ticket_id))


def _no_longer(statuses: frozenset[str]) -> str:
    if statuses <= ANALYZING_ONLY:
        return "the ticket is no longer being analyzed"
    if statuses <= PART_2_ACTIVE:
        return "the ticket is no longer being drafted"
    return f"the ticket is no longer {' or '.join(sorted(statuses))}"


def update_if(session: Session, ticket_id: UUID, statuses: frozenset[str], **values: Any) -> None:
    """Update the ticket only while its status is one of `statuses`; else TicketStateError."""
    result = session.execute(
        update(RfpTicket)
        .where(col(RfpTicket.id) == ticket_id, col(RfpTicket.status).in_(sorted(statuses)))
        .values(updated_at=utcnow(), **values)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise TicketStateError(_no_longer(statuses))


def _update_analyzing(session: Session, ticket_id: UUID, **values: Any) -> None:
    update_if(session, ticket_id, ANALYZING_ONLY, **values)


def set_stage(
    engine: Engine,
    ticket_id: UUID | str,
    stage: str,
    *,
    statuses: frozenset[str] = ANALYZING_ONLY,
) -> None:
    with Session(engine) as session:
        update_if(session, as_uuid(ticket_id), statuses, current_stage=stage[:64])
        session.commit()


def record_event(
    engine: Engine,
    ticket_id: UUID | str,
    node: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
    *,
    duration_ms: int | None = None,
    tokens: int = 0,
) -> None:
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        session.add(
            RfpTicketEvent(
                ticket_id=key,
                node=node[:64],
                outcome=outcome,
                detail=detail,
                duration_ms=duration_ms,
            )
        )
        if tokens:
            session.execute(
                update(RfpTicket)
                .where(col(RfpTicket.id) == key)
                .values(llm_tokens_used=RfpTicket.llm_tokens_used + tokens, updated_at=utcnow())
            )
        session.commit()


def save_markdown(engine: Engine, ticket_id: UUID | str, markdown: str, truncated: bool) -> None:
    with Session(engine) as session:
        _update_analyzing(
            session, as_uuid(ticket_id), markdown=markdown, markdown_truncated=truncated
        )
        session.commit()


def save_classification(
    engine: Engine, ticket_id: UUID | str, classification: dict[str, Any]
) -> None:
    with Session(engine) as session:
        _update_analyzing(session, as_uuid(ticket_id), classification=classification)
        session.commit()


def save_metadata(
    engine: Engine,
    ticket_id: UUID | str,
    metadata: dict[str, Any],
    readability: dict[str, Any],
    *,
    language: str | None,
) -> None:
    """The metadata and readability metrics, stored with the ticket (one row per RFP)."""
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        ticket = session.get(RfpTicket, key)
        if ticket is None:
            raise TicketNotFound(str(key))
        session.execute(delete(RfpMetadata).where(col(RfpMetadata.ticket_id) == key))
        deadline = metadata.get("deadline")
        session.add(
            RfpMetadata(
                rfp_id=ticket.rfp_id,
                ticket_id=key,
                client_name=metadata.get("client_name"),
                client_country=metadata.get("client_country"),
                currency=metadata.get("currency"),
                services_requested=list(metadata.get("services_requested") or []),
                monthly_volume=metadata.get("monthly_volume"),
                deadline=date.fromisoformat(deadline) if deadline else None,
                deadline_text=metadata.get("deadline_text"),
                budget_range=metadata.get("budget_range"),
                departments_needed=list(metadata.get("departments_needed") or []),
                evidence={
                    "quotes": metadata.get("evidence") or {},
                    "deadline_kind": metadata.get("deadline_kind"),
                    "missing": metadata.get("missing") or [],
                    "client_questions": metadata.get("client_questions") or [],
                    "notes": metadata.get("notes") or [],
                },
                language=language,
                word_count=readability["word_count"],
                sentence_count=readability["sentence_count"],
                char_count=readability["char_count"],
                estimated_tokens=readability["estimated_tokens"],
                flesch_reading_ease=readability.get("flesch_reading_ease"),
                flesch_kincaid_grade=readability.get("flesch_kincaid_grade"),
                gunning_fog=readability.get("gunning_fog"),
                readability_note=readability.get("note"),
                processing_cost=readability["processing_cost"],
            )
        )
        _update_analyzing(session, key)
        session.commit()


def discard(engine: Engine, ticket_id: UUID | str, reason: str) -> None:
    with Session(engine) as session:
        _update_analyzing(
            session,
            as_uuid(ticket_id),
            status=TicketStatus.DISCARDED.value,
            discard_reason=reason,
            completed_at=utcnow(),
        )
        session.commit()


def fail(
    engine: Engine,
    ticket_id: UUID | str,
    *,
    stage: str,
    message: str,
    statuses: frozenset[str] = ANALYZING_ONLY,
) -> bool:
    """Mark a ticket in `statuses` `failed`. False when it had moved on already."""
    with Session(engine) as session:
        try:
            update_if(
                session,
                as_uuid(ticket_id),
                statuses,
                status=TicketStatus.FAILED.value,
                error_stage=stage[:64],
                error_message=message,
                completed_at=utcnow(),
            )
        except TicketStateError:
            return False
        session.commit()
    return True


def complete_intake(
    engine: Engine,
    ticket_id: UUID | str,
    *,
    sections: list[dict[str, Any]],
    sales_brief: dict[str, Any],
    handoff_payload: dict[str, Any],
    contract_version: str,
    next_stage: str,
) -> None:
    """Sections, brief, handoff and `intake_complete` in one transaction: all or nothing."""
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        session.execute(
            delete(RfpDepartmentSection).where(col(RfpDepartmentSection.ticket_id) == key)
        )
        session.execute(delete(RfpHandoff).where(col(RfpHandoff.ticket_id) == key))
        for section in sections:
            session.add(
                RfpDepartmentSection(
                    ticket_id=key,
                    department_id=section["department_id"],
                    owner=section["owner"],
                    objective=section.get("objective"),
                    extracts=section.get("extracts") or [],
                    key_aspects=section.get("key_aspects") or [],
                    needed_from_department=section.get("needed_from_department") or [],
                    open_questions=section.get("open_questions") or [],
                    validation_notes=section.get("validation_notes") or [],
                )
            )
        session.add(
            RfpHandoff(
                ticket_id=key,
                contract_version=contract_version,
                next_stage=next_stage,
                status="pending",
                payload=handoff_payload,
            )
        )
        _update_analyzing(
            session,
            key,
            status=TicketStatus.INTAKE_COMPLETE.value,
            sales_brief=sales_brief,
            current_stage=None,
            completed_at=utcnow(),
        )
        session.commit()


def reset_ticket(
    engine: Engine, ticket_id: UUID | str, *, allowed: frozenset[str] = REPROCESSABLE
) -> str:
    """Back to `analyzing` with no derived rows, for a reprocess. Returns the old status."""
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        ticket = session.get(RfpTicket, key)
        if ticket is None:
            raise TicketNotFound(str(key))
        previous = ticket.status
        if previous not in allowed:
            raise TicketStateError(
                f"a ticket in status {previous!r} cannot be reprocessed "
                f"(only {', '.join(sorted(allowed))})"
            )
        # Children first: drafts reference sections, and PostgreSQL checks it.
        for model in (
            RfpProposalHandoff,
            RfpSectionDraft,
            RfpHandoff,
            RfpDepartmentSection,
            RfpMetadata,
        ):
            session.execute(delete(model).where(col(model.ticket_id) == key))  # type: ignore[attr-defined]
        session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == key, col(RfpTicket.status) == previous)
            .values(
                status=ANALYZING,
                current_stage="queued",
                markdown=None,
                markdown_truncated=False,
                classification=None,
                discard_reason=None,
                error_stage=None,
                error_message=None,
                sales_brief=None,
                llm_tokens_used=0,
                completed_at=None,
                updated_at=utcnow(),
            )
        )
        session.add(
            RfpTicketEvent(
                ticket_id=key,
                node="reprocess",
                outcome="decision",
                detail={"previous_status": previous},
            )
        )
        session.commit()
    return previous


_SWEPT_WORK: Final = {
    TicketStatus.ANALYZING.value: "the analysis",
    TicketStatus.DRAFTING.value: "the proposal drafting",
    TicketStatus.UNDER_EVALUATION.value: "the proposal evaluation",
}


def sweep_abandoned(
    engine: Engine, *, older_than: timedelta, now: datetime | None = None
) -> list[UUID]:
    """Mark `failed` every ticket in progress with no progress for `older_than`.

    A run dies with its process (API restart mid-run); without this its ticket
    would show `analyzing`, `drafting` or `under_evaluation` forever. Each node
    refreshes `updated_at`. Finished states (`intake_complete`,
    `needs_human_review`, `waiting_for_approval`, ...) are never touched.
    """
    cutoff = (now or utcnow()) - older_than
    minutes = int(older_than.total_seconds() // 60)
    closed: list[UUID] = []
    with Session(engine) as session:
        for status, work in _SWEPT_WORK.items():
            stale = list(
                session.execute(
                    select(col(RfpTicket.id)).where(
                        col(RfpTicket.status) == status,
                        col(RfpTicket.updated_at) < cutoff,
                    )
                ).scalars()
            )
            if not stale:
                continue
            session.execute(
                update(RfpTicket)
                .where(col(RfpTicket.id).in_(stale), col(RfpTicket.status) == status)
                .values(
                    status=TicketStatus.FAILED.value,
                    error_stage=ABANDONED_STAGE,
                    error_message=(
                        f"{work} stopped making progress for {minutes} minutes "
                        "(the API was probably restarted mid-run)"
                    ),
                    completed_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            closed.extend(stale)
        session.commit()
    return closed


# --- Reads ------------------------------------------------------------------------------


def _ticket_fields(ticket: RfpTicket) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "rfp_id": ticket.rfp_id,
        "status": ticket.status,
        "current_stage": ticket.current_stage,
        "original_filename": ticket.original_filename,
        "created_at": as_utc(ticket.created_at),
        "updated_at": as_utc(ticket.updated_at),
    }


def new_ticket_summary(ticket: RfpTicket) -> dict[str, Any]:
    """A just-registered ticket as the list shows it (`RfpTicketSummary`), JSON-ready.

    No metadata exists yet: no client, no country, no departments.
    """
    fields = _ticket_fields(ticket)
    return {
        **fields,
        "ticket_id": str(fields["ticket_id"]),
        "rfp_id": str(fields["rfp_id"]),
        "created_at": fields["created_at"].isoformat(),
        "updated_at": fields["updated_at"].isoformat(),
        "client_name": None,
        "client_country": None,
        "departments_needed": [],
    }


def list_tickets(engine: Engine, *, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """The newest tickets first, with the client when metadata exists."""
    with Session(engine) as session:
        rows = session.execute(
            select(RfpTicket, RfpMetadata)
            .outerjoin(RfpMetadata, col(RfpMetadata.ticket_id) == col(RfpTicket.id))
            .order_by(col(RfpTicket.created_at).desc())
            .limit(limit)
        ).all()
    return [
        {
            **_ticket_fields(ticket),
            "client_name": metadata.client_name if metadata else None,
            "client_country": metadata.client_country if metadata else None,
            "departments_needed": list(metadata.departments_needed) if metadata else [],
        }
        for ticket, metadata in rows
    ]


def _metadata_fields(metadata: RfpMetadata) -> dict[str, Any]:
    evidence = metadata.evidence or {}
    return {
        "client_name": metadata.client_name,
        "client_country": metadata.client_country,
        "currency": metadata.currency,
        "services_requested": list(metadata.services_requested),
        "monthly_volume": metadata.monthly_volume,
        "deadline": metadata.deadline,
        "deadline_text": metadata.deadline_text,
        "deadline_kind": evidence.get("deadline_kind"),
        "budget_range": metadata.budget_range,
        "departments_needed": list(metadata.departments_needed),
        "evidence": evidence.get("quotes") or {},
        "missing": evidence.get("missing") or [],
        "notes": evidence.get("notes") or [],
        "readability": {
            "language": metadata.language,
            "word_count": metadata.word_count,
            "sentence_count": metadata.sentence_count,
            "char_count": metadata.char_count,
            "estimated_tokens": metadata.estimated_tokens,
            "flesch_reading_ease": metadata.flesch_reading_ease,
            "flesch_kincaid_grade": metadata.flesch_kincaid_grade,
            "gunning_fog": metadata.gunning_fog,
            "processing_cost": metadata.processing_cost,
            "note": metadata.readability_note,
        },
    }


def _draft_fields(section: RfpDepartmentSection) -> dict[str, Any]:
    """A section's Part 2 state: the current draft and its evaluation, if any."""
    data = section.draft_data or {}
    return {
        "draft_status": section.draft_status,
        "draft_iterations": section.draft_iterations or 0,
        "max_iterations": data.get("max_iterations"),
        "is_sla_section": bool(data.get("is_sla_section")),
        "needs_human_review": section.draft_status == "needs_human_review",
        "draft_content": section.draft_content,
        "draft": data.get("draft"),
        "guard_notes": list(data.get("guard_notes") or []),
        "knowledge_source": data.get("knowledge_source"),
        "rate_card_version": data.get("rate_card_version"),
        "review_note": data.get("review_note"),
        "evaluation": section.evaluation_results,
        "approval_status": section.approval_status,
        "ready_at": as_utc(section.ready_at),
    }


def _proposal_fields(proposal: RfpProposalHandoff) -> dict[str, Any]:
    payload = proposal.payload or {}
    metrics = payload.get("metrics") or {}
    return {
        "contract_version": proposal.contract_version,
        "next_stage": proposal.next_stage,
        "status": proposal.status,
        "needs_human_review": proposal.needs_human_review,
        "sla_department": payload.get("sla_department"),
        "currency": payload.get("currency"),
        "rate_card_version": payload.get("rate_card_version"),
        "max_iterations": payload.get("max_iterations"),
        "average_iterations_per_section": metrics.get("average_iterations_per_section"),
        "passed_first_attempt": metrics.get("passed_first_attempt"),
        "created_at": as_utc(proposal.created_at),
        "claimed_at": as_utc(proposal.claimed_at),
    }


def get_ticket_detail(engine: Engine, ticket_id: UUID | str) -> dict[str, Any] | None:
    """Everything Sales reads about one ticket. None for an unknown id."""
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        ticket = session.get(RfpTicket, key)
        if ticket is None:
            return None
        metadata = session.execute(
            select(RfpMetadata).where(col(RfpMetadata.ticket_id) == key)
        ).scalar_one_or_none()
        sections = list(
            session.execute(
                select(RfpDepartmentSection).where(col(RfpDepartmentSection.ticket_id) == key)
            ).scalars()
        )
        handoff = session.get(RfpHandoff, key)
        proposal = session.get(RfpProposalHandoff, key)
        events = list(
            session.execute(
                select(RfpTicketEvent)
                .where(col(RfpTicketEvent.ticket_id) == key)
                .order_by(col(RfpTicketEvent.created_at), col(RfpTicketEvent.id))
            ).scalars()
        )

    sections.sort(key=lambda section: DEPARTMENT_IDS.index(section.department_id))  # type: ignore[arg-type]
    return {
        **_ticket_fields(ticket),
        "pdf_bytes": ticket.pdf_bytes,
        "completed_at": as_utc(ticket.completed_at),
        "classification": ticket.classification,
        "discard_reason": ticket.discard_reason,
        "error_stage": ticket.error_stage,
        "error_message": ticket.error_message,
        "llm_tokens_used": ticket.llm_tokens_used,
        "markdown_chars": len(ticket.markdown) if ticket.markdown else 0,
        "markdown_truncated": ticket.markdown_truncated,
        "metadata": _metadata_fields(metadata) if metadata else None,
        "sections": [
            {
                "department_id": section.department_id,
                "department_name": DEPARTMENT_BY_ID[section.department_id].name,
                "owner": section.owner,
                "objective": section.objective,
                "extracts": list(section.extracts),
                "key_aspects": list(section.key_aspects),
                "needed_from_department": list(section.needed_from_department),
                "open_questions": list(section.open_questions),
                "validation_notes": list(section.validation_notes),
                **_draft_fields(section),
            }
            for section in sections
        ],
        "sales_brief": ticket.sales_brief,
        "handoff": (
            {
                "contract_version": handoff.contract_version,
                "next_stage": handoff.next_stage,
                "status": handoff.status,
                "created_at": as_utc(handoff.created_at),
                "claimed_at": as_utc(handoff.claimed_at),
            }
            if handoff
            else None
        ),
        "proposal": _proposal_fields(proposal) if proposal else None,
        "events": [
            {
                "node": event.node,
                "outcome": event.outcome,
                "detail": event.detail,
                "duration_ms": event.duration_ms,
                "created_at": as_utc(event.created_at),
            }
            for event in events
        ],
    }
