"""Part 3 persistence: approval runs, decisions, section approvals and the final document.

PostgreSQL through the same SQLModel layer as Parts 1 and 2 (each function takes
the engine). Every write that changes a section or the ticket checks first that
the ticket is still `waiting_for_approval` (`store.update_if`): a ticket that is
already `done` or `rejected` is never written again.

- `rfp_approval_runs`: one row per ticket. Its checkpoint thread, its status, the
  lease that lets one process at a time resume the graph, and every arbitration
  ruling.
- `rfp_approval_decisions`: every decision, append-only. The owner's are
  `recorded` by the API and `applied` (or `stale`, `refused`) by the graph. An
  arbiter's or the system's are written `applied`, by the graph or the runner.
- `rfp_department_sections`: `approval_status`, `approver` (the owner's name),
  `approved_by` (the account), `approved_at`, `review_rounds`, `ready_at`.
- `rfp_final_documents`: written in the same transaction that moves the ticket
  to `done`.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.pipelines.rfp_intake.briefing import DEPARTMENT_IDS, TicketStatus
from data.pipelines.rfp_intake.models import (
    RfpApprovalDecision,
    RfpApprovalRun,
    RfpDepartmentSection,
    RfpFinalDocument,
    RfpTicket,
    RfpTicketEvent,
    utcnow,
)
from data.pipelines.rfp_intake.store import (
    TicketNotFound,
    TicketStateError,
    as_utc,
    as_uuid,
    update_if,
)

WAITING: Final = TicketStatus.WAITING_FOR_APPROVAL.value
# The ticket status every Part 3 write requires.
PART_3_ACTIVE: Final = frozenset({WAITING})
# Where Part 2 leaves a ticket: both are Part 3's to open (the handoff contract).
OPENABLE: Final = frozenset({WAITING, TicketStatus.NEEDS_HUMAN_REVIEW.value})
# A section still waiting for a decision, or being revised.
UNDECIDED: Final = ("pending", "changes_requested")

_SNAPSHOT_FIELDS: Final = (
    "draft_content",
    "draft_data",
    "evaluation_results",
    "draft_status",
    "draft_iterations",
)


def _section(session: Session, section_id: UUID | str) -> RfpDepartmentSection:
    section = session.get(RfpDepartmentSection, as_uuid(section_id))
    if section is None:
        raise TicketNotFound(f"section {section_id}")
    return section


def _guard(session: Session, ticket_id: UUID | str) -> None:
    update_if(session, as_uuid(ticket_id), PART_3_ACTIVE)


# --- The run and its lease ------------------------------------------------------------


def create_run(
    engine: Engine, ticket_id: UUID | str, *, thread_id: str, graph_version: str
) -> None:
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        if session.get(RfpApprovalRun, key) is not None:
            raise TicketStateError("this ticket's approval has already started")
        session.add(
            RfpApprovalRun(
                ticket_id=key, thread_id=thread_id, graph_version=graph_version, status="running"
            )
        )
        session.commit()


def get_run(engine: Engine, ticket_id: UUID | str) -> RfpApprovalRun | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.get(RfpApprovalRun, as_uuid(ticket_id))


def acquire_lease(
    engine: Engine,
    ticket_id: UUID | str,
    worker: str,
    *,
    ttl: timedelta,
    now: datetime | None = None,
) -> bool:
    """Take the right to resume this ticket's graph. False while another worker holds it.

    A conditional UPDATE, so it works the same on PostgreSQL and SQLite. A lease
    whose holder died expires after `ttl`.
    """
    moment = now or utcnow()
    with Session(engine) as session:
        result = session.execute(
            update(RfpApprovalRun)
            .where(
                col(RfpApprovalRun.ticket_id) == as_uuid(ticket_id),
                or_(
                    col(RfpApprovalRun.worker).is_(None),
                    col(RfpApprovalRun.worker) == worker,
                    col(RfpApprovalRun.lease_expires_at) < moment,
                ),
            )
            .values(worker=worker[:100], lease_expires_at=moment + ttl, updated_at=moment)
        )
        session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def release_lease(engine: Engine, ticket_id: UUID | str, worker: str) -> None:
    with Session(engine) as session:
        session.execute(
            update(RfpApprovalRun)
            .where(
                col(RfpApprovalRun.ticket_id) == as_uuid(ticket_id),
                col(RfpApprovalRun.worker) == worker[:100],
            )
            .values(worker=None, lease_expires_at=None)
        )
        session.commit()


def set_run(engine: Engine, ticket_id: UUID | str, **values: Any) -> None:
    with Session(engine) as session:
        session.execute(
            update(RfpApprovalRun)
            .where(col(RfpApprovalRun.ticket_id) == as_uuid(ticket_id))
            .values(updated_at=utcnow(), **values)
        )
        session.commit()


def add_rulings(
    engine: Engine, ticket_id: UUID | str, rulings: list[dict[str, Any]], *, rounds: int
) -> None:
    with Session(engine) as session:
        run = session.get(RfpApprovalRun, as_uuid(ticket_id))
        if run is None:
            raise TicketNotFound(f"approval run {ticket_id}")
        run.rulings = [*run.rulings, *rulings]
        run.arbitration_rounds = rounds
        run.updated_at = utcnow()
        session.commit()


def runs_to_recover(engine: Engine, *, now: datetime | None = None) -> list[UUID]:
    """Runs a dead process left behind: running with an expired lease, or failed."""
    moment = now or utcnow()
    with Session(engine) as session:
        return list(
            session.execute(
                select(col(RfpApprovalRun.ticket_id)).where(
                    or_(
                        (col(RfpApprovalRun.status) == "running")
                        & or_(
                            col(RfpApprovalRun.lease_expires_at).is_(None),
                            col(RfpApprovalRun.lease_expires_at) < moment,
                        ),
                        col(RfpApprovalRun.status) == "failed",
                    )
                )
            ).scalars()
        )


# --- The ticket and its sections --------------------------------------------------------


def open_approval(engine: Engine, ticket_id: UUID | str, departments: Sequence[str]) -> str:
    """The ticket enters Part 3 (`waiting_for_approval`), from either Part 2 end status.

    Returns the status it had. Every active section waits for its owner.
    """
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        ticket = session.get(RfpTicket, key)
        if ticket is None:
            raise TicketNotFound(str(key))
        previous = ticket.status
        update_if(
            session,
            key,
            OPENABLE,
            status=WAITING,
            current_stage="open_approval",
            error_stage=None,
            error_message=None,
        )
        for section in session.execute(
            select(RfpDepartmentSection).where(
                col(RfpDepartmentSection.ticket_id) == key,
                col(RfpDepartmentSection.department_id).in_(list(departments)),
            )
        ).scalars():
            section.approval_status = "pending"
            section.review_rounds = 0
            section.approver = None
            section.approved_by = None
            section.approved_at = None
        session.commit()
    return previous


def load_sections(engine: Engine, ticket_id: UUID | str) -> list[RfpDepartmentSection]:
    with Session(engine, expire_on_commit=False) as session:
        rows = list(
            session.execute(
                select(RfpDepartmentSection).where(
                    col(RfpDepartmentSection.ticket_id) == as_uuid(ticket_id)
                )
            ).scalars()
        )
    return sorted(rows, key=lambda row: DEPARTMENT_IDS.index(row.department_id))  # type: ignore[arg-type]


def load_section(engine: Engine, ticket_id: UUID | str, department_id: str) -> RfpDepartmentSection:
    for section in load_sections(engine, ticket_id):
        if section.department_id == department_id:
            return section
    raise TicketNotFound(f"{department_id} section of ticket {ticket_id}")


def request_review(
    engine: Engine, ticket_id: UUID | str, section_id: UUID | str, *, new_round: bool
) -> None:
    """The section waits for its owner. A revised section restarts the approval clock."""
    with Session(engine) as session:
        _guard(session, ticket_id)
        section = _section(session, section_id)
        section.approval_status = "pending"
        if new_round or section.ready_at is None:
            section.ready_at = utcnow()
        session.commit()


def start_revision(engine: Engine, ticket_id: UUID | str, section_id: UUID | str) -> int:
    """The section goes back for changes: its approval is cleared. Returns its revision count."""
    with Session(engine) as session:
        _guard(session, ticket_id)
        section = _section(session, section_id)
        section.approval_status = "changes_requested"
        section.review_rounds = (section.review_rounds or 0) + 1
        section.approver = None
        section.approved_by = None
        section.approved_at = None
        rounds = section.review_rounds
        session.commit()
    return rounds


def snapshot(section: RfpDepartmentSection) -> dict[str, Any]:
    """The section's current draft, to put back if its revision cannot be completed."""
    return {field: getattr(section, field) for field in _SNAPSHOT_FIELDS}


def restore_section(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    saved: dict[str, Any],
    *,
    note: str,
) -> None:
    with Session(engine) as session:
        _guard(session, ticket_id)
        section = _section(session, section_id)
        for field in _SNAPSHOT_FIELDS:
            setattr(section, field, saved[field])
        section.draft_data = {**(saved.get("draft_data") or {}), "review_note": note}
        session.commit()


def sign_off(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    *,
    approver: str,
    approved_by: str,
    approved_at: datetime,
) -> None:
    with Session(engine) as session:
        _guard(session, ticket_id)
        section = _section(session, section_id)
        section.approval_status = "approved"
        section.approver = approver[:100]
        section.approved_by = approved_by[:254]
        section.approved_at = approved_at
        session.commit()


def set_section_status(
    engine: Engine, ticket_id: UUID | str, section_id: UUID | str, status: str
) -> None:
    """`rejected` or `withdrawn`: the section's branch ends without an approval."""
    with Session(engine) as session:
        _guard(session, ticket_id)
        _section(session, section_id).approval_status = status
        session.commit()


def rejected_departments(engine: Engine, ticket_id: UUID | str) -> list[str]:
    return [
        s.department_id for s in load_sections(engine, ticket_id) if s.approval_status == "rejected"
    ]


def set_waiting_stage(engine: Engine, ticket_id: UUID | str, departments: list[str]) -> None:
    """Live progress while paused: which departments the ticket waits for."""
    stage = ("awaiting_approval:" + ",".join(departments))[:64] if departments else None
    with Session(engine) as session:
        session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == as_uuid(ticket_id), col(RfpTicket.status) == WAITING)
            .values(current_stage=stage, updated_at=utcnow())
        )
        session.commit()


# --- Decisions ---------------------------------------------------------------------------


def record_decision(
    engine: Engine,
    *,
    ticket_id: UUID | str,
    section_id: UUID | str,
    department_id: str,
    review_round: int,
    draft_sha256: str | None,
    action: str,
    comment: str | None,
    source: str,
    decided_by: str,
    decided_by_name: str,
    trigger_id: str | None = None,
    status: str = "recorded",
) -> RfpApprovalDecision:
    """Append one decision. An owner's second live decision for a round is an IntegrityError."""
    decision = RfpApprovalDecision(
        ticket_id=as_uuid(ticket_id),
        section_id=as_uuid(section_id),
        department_id=department_id,
        review_round=review_round,
        draft_sha256=draft_sha256,
        action=action,
        comment=comment,
        source=source,
        decided_by=decided_by[:254],
        decided_by_name=decided_by_name[:100],
        trigger_id=trigger_id,
        status=status,
        applied_at=utcnow() if status == "applied" else None,
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add(decision)
        session.commit()
    return decision


def recorded_decisions(engine: Engine, ticket_id: UUID | str) -> list[RfpApprovalDecision]:
    """Decisions the graph has not taken yet, oldest first."""
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.execute(
                select(RfpApprovalDecision)
                .where(
                    col(RfpApprovalDecision.ticket_id) == as_uuid(ticket_id),
                    col(RfpApprovalDecision.status) == "recorded",
                )
                .order_by(col(RfpApprovalDecision.created_at), col(RfpApprovalDecision.id))
            ).scalars()
        )


def tickets_with_recorded_decisions(engine: Engine) -> list[UUID]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(col(RfpApprovalDecision.ticket_id))
                .where(col(RfpApprovalDecision.status) == "recorded")
                .distinct()
            ).scalars()
        )


def mark_decision(
    engine: Engine, decision_id: UUID | str, status: str, note: str | None = None
) -> bool:
    """`recorded` -> `applied`, `stale` or `refused`. False if it had left `recorded` already."""
    with Session(engine) as session:
        result = session.execute(
            update(RfpApprovalDecision)
            .where(
                col(RfpApprovalDecision.id) == as_uuid(decision_id),
                col(RfpApprovalDecision.status) == "recorded",
            )
            .values(
                status=status,
                status_note=note,
                applied_at=utcnow() if status == "applied" else None,
            )
        )
        session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def get_decision(engine: Engine, decision_id: UUID | str) -> RfpApprovalDecision | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.get(RfpApprovalDecision, as_uuid(decision_id))


def decisions_for(engine: Engine, ticket_id: UUID | str) -> list[RfpApprovalDecision]:
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.execute(
                select(RfpApprovalDecision)
                .where(col(RfpApprovalDecision.ticket_id) == as_uuid(ticket_id))
                .order_by(col(RfpApprovalDecision.created_at), col(RfpApprovalDecision.id))
            ).scalars()
        )


# --- Endings ------------------------------------------------------------------------------


def complete_approval(engine: Engine, ticket_id: UUID | str, document: dict[str, Any]) -> None:
    """The final document and `done` in one transaction: a `done` ticket always has it."""
    key = as_uuid(ticket_id)
    now = utcnow()
    with Session(engine) as session:
        session.add(
            RfpFinalDocument(
                ticket_id=key,
                rfp_id=as_uuid(document["rfp_id"]),
                client_name=(document.get("client") or {}).get("name"),
                currency=document["currency"],
                sections=document["sections"],
                content=document["content"],
                content_sha256=document["content_sha256"],
                approval_thread_id=document["approval_thread_id"],
                generated_at=datetime.fromisoformat(document["generated_at"]),
            )
        )
        update_if(
            session,
            key,
            PART_3_ACTIVE,
            status=TicketStatus.DONE.value,
            current_stage=None,
            error_stage=None,
            error_message=None,
            completed_at=now,
        )
        session.execute(
            update(RfpApprovalRun)
            .where(col(RfpApprovalRun.ticket_id) == key)
            .values(status="done", finished_at=now, updated_at=now, error_stage=None)
        )
        session.commit()


def close_rejected(engine: Engine, ticket_id: UUID | str) -> list[str]:
    """The proposal does not go out: undecided sections are withdrawn, the ticket `rejected`.

    Returns the departments withdrawn. Who rejected and why is in the decisions.
    """
    key = as_uuid(ticket_id)
    now = utcnow()
    with Session(engine) as session:
        withdrawn: list[str] = []
        for section in session.execute(
            select(RfpDepartmentSection).where(
                col(RfpDepartmentSection.ticket_id) == key,
                col(RfpDepartmentSection.approval_status).in_(UNDECIDED),
            )
        ).scalars():
            section.approval_status = "withdrawn"
            withdrawn.append(section.department_id)
        update_if(
            session,
            key,
            PART_3_ACTIVE,
            status=TicketStatus.REJECTED.value,
            current_stage=None,
            error_stage=None,
            error_message=None,
            completed_at=now,
        )
        session.execute(
            update(RfpApprovalRun)
            .where(col(RfpApprovalRun.ticket_id) == key)
            .values(status="rejected", finished_at=now, updated_at=now)
        )
        session.commit()
    return withdrawn


def fail_run(engine: Engine, ticket_id: UUID | str, *, stage: str, message: str) -> None:
    """A technical failure: the ticket keeps `waiting_for_approval` and every decision.

    Nothing is thrown away. `runner.resume` continues from the last checkpoint.
    """
    key = as_uuid(ticket_id)
    now = utcnow()
    with Session(engine) as session:
        session.execute(
            update(RfpApprovalRun)
            .where(col(RfpApprovalRun.ticket_id) == key)
            .values(status="failed", error_stage=stage[:64], error_message=message, updated_at=now)
        )
        session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == key, col(RfpTicket.status) == WAITING)
            .values(error_stage=stage[:64], error_message=message, updated_at=now)
        )
        session.add(
            RfpTicketEvent(
                ticket_id=key,
                node=stage[:64],
                outcome="failed",
                detail={"part": 3, "message": message},
            )
        )
        session.commit()


def clear_failure(engine: Engine, ticket_id: UUID | str) -> None:
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        session.execute(
            update(RfpApprovalRun)
            .where(col(RfpApprovalRun.ticket_id) == key)
            .values(status="running", error_stage=None, error_message=None, updated_at=utcnow())
        )
        session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == key, col(RfpTicket.status) == WAITING)
            .values(error_stage=None, error_message=None, updated_at=utcnow())
        )
        session.commit()


def get_document(engine: Engine, ticket_id: UUID | str) -> RfpFinalDocument | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.get(RfpFinalDocument, as_uuid(ticket_id))


# --- Reads for the KPI --------------------------------------------------------------------


def approval_kpi(engine: Engine) -> dict[str, Any]:
    """Briefing §3: approval time per department, and proposal build time (upload -> done)."""
    with Session(engine) as session:
        sections = list(
            session.execute(
                select(RfpDepartmentSection).where(
                    col(RfpDepartmentSection.approval_status) == "approved"
                )
            ).scalars()
        )
        tickets = list(
            session.execute(
                select(RfpTicket).where(col(RfpTicket.status) == TicketStatus.DONE.value)
            ).scalars()
        )
    per_department: dict[str, list[float]] = {}
    for section in sections:
        ready, approved = as_utc(section.ready_at), as_utc(section.approved_at)
        if ready is not None and approved is not None:
            hours = max((approved - ready).total_seconds(), 0) / 3600
            per_department.setdefault(section.department_id, []).append(hours)
    builds = [
        (completed - created).total_seconds() / 3600
        for ticket in tickets
        if (created := as_utc(ticket.created_at)) and (completed := as_utc(ticket.completed_at))
    ]
    return {
        "approval_hours_by_department": {
            dept: {"sections": len(values), "average_hours": round(sum(values) / len(values), 2)}
            for dept, values in sorted(per_department.items())
        },
        "proposals_done": len(builds),
        "average_build_hours": round(sum(builds) / len(builds), 2) if builds else None,
        "build_target": "under 48 hours from upload to final document",
    }
