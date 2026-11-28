"""Part 2 persistence: section drafts, their evaluations, and the hand-off to approval.

PostgreSQL through the same SQLModel layer as Part 1 (each function takes the
engine). Every write first checks that the ticket is still `drafting` or
`under_evaluation` (`store.update_if`). A run that finds it swept or reset
raises `TicketStateError` instead of overwriting the newer state. Part 3
runs the same section loop to revise an approved-for-review section while the
ticket is `waiting_for_approval`: the loop's writes take that status set
(`statuses=`), and the ticket's status is never moved back to a Part 2 one.

- `rfp_department_sections` keeps the *current* state of each section:
  `draft_content` (Markdown), `evaluation_results` (the latest
  EvaluationResult), `draft_status`, `draft_iterations` and `draft_data`.
- `rfp_section_drafts` keeps *every* attempt and its evaluation (audit, and
  the "average iterations per section" KPI of briefing §3).
- `rfp_proposal_handoffs` is the Part 3 queue, written with the ticket's
  final Part 2 status in one transaction.
"""

from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.pipelines.rfp_intake.briefing import DEPARTMENT_IDS, TicketStatus
from data.pipelines.rfp_intake.handoff import IntakeHandoff
from data.pipelines.rfp_intake.models import (
    RfpDepartmentSection,
    RfpHandoff,
    RfpProposalHandoff,
    RfpSectionDraft,
    RfpTicket,
    RfpTicketEvent,
    utcnow,
)
from data.pipelines.rfp_intake.store import (
    PART_2_ACTIVE,
    TicketNotFound,
    TicketStateError,
    as_uuid,
    update_if,
)

DRAFTING: Final = TicketStatus.DRAFTING.value
UNDER_EVALUATION: Final = TicketStatus.UNDER_EVALUATION.value
# Where a Part 2 run ends (besides `failed`).
FINAL_STATUSES: Final = frozenset(
    {TicketStatus.WAITING_FOR_APPROVAL.value, TicketStatus.NEEDS_HUMAN_REVIEW.value}
)
REDRAFTABLE: Final = FINAL_STATUSES | {TicketStatus.FAILED.value}
FINISHED_SECTION: Final = frozenset({"passed", "needs_human_review"})

_PART_2_RESET: Final[dict[str, Any]] = {
    "draft_status": None,
    "draft_iterations": 0,
    "draft_content": None,
    "draft_data": None,
    "draft_run_id": None,
    "evaluation_results": None,
    "approval_status": None,
    "ready_at": None,
}


class MissingSections(LookupError):
    """The handoff names a department whose Part 1 section row is missing."""


def _guard(session: Session, ticket_id: UUID, statuses: frozenset[str] = PART_2_ACTIVE) -> None:
    update_if(session, ticket_id, statuses)


def _section(session: Session, section_id: UUID | str) -> RfpDepartmentSection:
    section = session.get(RfpDepartmentSection, as_uuid(section_id))
    if section is None:
        raise MissingSections(str(section_id))
    return section


def start_run(
    engine: Engine, ticket_id: UUID | str, *, run_id: UUID | str, departments: list[str]
) -> dict[str, UUID]:
    """Reset the Part 2 state of the handoff's sections; their ids by department."""
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        _guard(session, key)
        rows = {
            row.department_id: row
            for row in session.execute(
                select(RfpDepartmentSection).where(col(RfpDepartmentSection.ticket_id) == key)
            ).scalars()
        }
        missing = [dept for dept in departments if dept not in rows]
        if missing:
            raise MissingSections(", ".join(missing))
        ids: dict[str, UUID] = {}
        for dept in departments:
            row = rows[dept]
            for field, value in _PART_2_RESET.items():
                setattr(row, field, value)
            row.draft_status = "pending"
            row.draft_run_id = as_uuid(run_id)
            ids[dept] = row.id
        session.commit()
    return ids


def mark_drafting(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    *,
    statuses: frozenset[str] = PART_2_ACTIVE,
) -> None:
    with Session(engine) as session:
        _guard(session, as_uuid(ticket_id), statuses)
        _section(session, section_id).draft_status = "drafting"
        session.commit()


def save_draft(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    *,
    run_id: UUID | str,
    iteration: int,
    department_id: str,
    draft: dict[str, Any],
    markdown: str,
    guard_notes: list[str],
    draft_data: dict[str, Any],
    tokens: int,
    statuses: frozenset[str] = PART_2_ACTIVE,
) -> None:
    """Store one attempt (an upsert, so a retried node is harmless).

    The ticket moves from `drafting` to `under_evaluation` with the first
    draft and never back: with sections drafted in parallel, the ticket shows
    the furthest stage reached, and each section's `draft_status` the detail.
    """
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        moved = session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == key, col(RfpTicket.status) == DRAFTING)
            .values(status=UNDER_EVALUATION, updated_at=utcnow())
        )
        if moved.rowcount != 1:  # type: ignore[attr-defined]
            _guard(session, key, statuses)
        attempt = session.execute(
            select(RfpSectionDraft).where(
                col(RfpSectionDraft.section_id) == as_uuid(section_id),
                col(RfpSectionDraft.run_id) == as_uuid(run_id),
                col(RfpSectionDraft.iteration) == iteration,
            )
        ).scalar_one_or_none()
        if attempt is None:
            attempt = RfpSectionDraft(
                ticket_id=key,
                section_id=as_uuid(section_id),
                department_id=department_id,
                run_id=as_uuid(run_id),
                iteration=iteration,
                draft_data=draft,
                draft_content=markdown,
            )
            session.add(attempt)
        attempt.draft_data = draft
        attempt.draft_content = markdown
        attempt.guard_notes = guard_notes
        attempt.evaluation = None
        attempt.overall_pass = None
        attempt.tokens = tokens
        attempt.created_at = utcnow()

        section = _section(session, section_id)
        section.draft_status = UNDER_EVALUATION
        section.draft_iterations = iteration
        section.draft_content = markdown
        section.draft_data = draft_data
        section.evaluation_results = None
        session.commit()


def save_evaluation(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    *,
    run_id: UUID | str,
    iteration: int,
    evaluation: dict[str, Any],
    overall_pass: bool,
    statuses: frozenset[str] = PART_2_ACTIVE,
) -> None:
    with Session(engine) as session:
        _guard(session, as_uuid(ticket_id), statuses)
        session.execute(
            update(RfpSectionDraft)
            .where(
                col(RfpSectionDraft.section_id) == as_uuid(section_id),
                col(RfpSectionDraft.run_id) == as_uuid(run_id),
                col(RfpSectionDraft.iteration) == iteration,
            )
            .values(evaluation=evaluation, overall_pass=overall_pass, evaluated_at=utcnow())
        )
        _section(session, section_id).evaluation_results = evaluation
        session.commit()


def finish_section(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    *,
    status: str,
    statuses: frozenset[str] = PART_2_ACTIVE,
) -> None:
    """The section leaves the loop: `passed`, `needs_human_review` or `failed`."""
    with Session(engine) as session:
        _guard(session, as_uuid(ticket_id), statuses)
        section = _section(session, section_id)
        section.draft_status = status
        if status in FINISHED_SECTION:
            section.approval_status = "pending"
            section.ready_at = utcnow()
        session.commit()


def latest_evaluated_attempt(
    engine: Engine, section_id: UUID | str, run_id: UUID | str
) -> RfpSectionDraft | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.execute(
            select(RfpSectionDraft)
            .where(
                col(RfpSectionDraft.section_id) == as_uuid(section_id),
                col(RfpSectionDraft.run_id) == as_uuid(run_id),
                col(RfpSectionDraft.evaluation).is_not(None),
            )
            .order_by(col(RfpSectionDraft.iteration).desc())
            .limit(1)
        ).scalar_one_or_none()


def restore_attempt(
    engine: Engine,
    ticket_id: UUID | str,
    section_id: UUID | str,
    attempt: RfpSectionDraft,
    *,
    draft_data: dict[str, Any],
    statuses: frozenset[str] = PART_2_ACTIVE,
) -> None:
    """Ship the last evaluated attempt as `needs_human_review` (a later attempt broke)."""
    with Session(engine) as session:
        _guard(session, as_uuid(ticket_id), statuses)
        section = _section(session, section_id)
        section.draft_content = attempt.draft_content
        section.draft_data = draft_data
        section.evaluation_results = attempt.evaluation
        section.draft_iterations = attempt.iteration
        section.draft_status = "needs_human_review"
        section.approval_status = "pending"
        section.ready_at = utcnow()
        session.commit()


def load_sections(engine: Engine, ticket_id: UUID | str) -> list[RfpDepartmentSection]:
    """The ticket's sections, in the briefing's department order."""
    with Session(engine, expire_on_commit=False) as session:
        rows = list(
            session.execute(
                select(RfpDepartmentSection).where(
                    col(RfpDepartmentSection.ticket_id) == as_uuid(ticket_id)
                )
            ).scalars()
        )
    return sorted(rows, key=lambda row: DEPARTMENT_IDS.index(row.department_id))  # type: ignore[arg-type]


def complete_drafting(
    engine: Engine,
    ticket_id: UUID | str,
    *,
    payload: dict[str, Any],
    needs_human_review: bool,
    contract_version: str,
    next_stage: str,
) -> str:
    """The Part 3 handoff and the ticket's final status in one transaction. Returns it."""
    key = as_uuid(ticket_id)
    status = (
        TicketStatus.NEEDS_HUMAN_REVIEW.value
        if needs_human_review
        else TicketStatus.WAITING_FOR_APPROVAL.value
    )
    with Session(engine) as session:
        session.execute(delete(RfpProposalHandoff).where(col(RfpProposalHandoff.ticket_id) == key))
        session.add(
            RfpProposalHandoff(
                ticket_id=key,
                contract_version=contract_version,
                next_stage=next_stage,
                status="pending",
                needs_human_review=needs_human_review,
                payload=payload,
            )
        )
        update_if(session, key, PART_2_ACTIVE, status=status, current_stage=None)
        session.commit()
    return status


def close_unfinished_sections(engine: Engine, ticket_id: UUID | str) -> None:
    """After a failed run: sections still in the loop are marked `failed`."""
    with Session(engine) as session:
        session.execute(
            update(RfpDepartmentSection)
            .where(
                col(RfpDepartmentSection.ticket_id) == as_uuid(ticket_id),
                col(RfpDepartmentSection.draft_status).in_(
                    ["pending", "drafting", "under_evaluation"]
                ),
            )
            .values(draft_status="failed")
        )
        session.commit()


def reset_for_redraft(engine: Engine, ticket_id: UUID | str, *, claimed_by: str) -> IntakeHandoff:
    """Back to `drafting` for a new Part 2 run; returns the intake handoff to draft from.

    Allowed after Part 2 ended (`waiting_for_approval`, `needs_human_review`) or
    failed (`failed` with a claimed intake handoff). Refused once Part 3 has
    claimed the proposal or an owner has decided on a section. The attempts
    of earlier runs stay in `rfp_section_drafts`.
    """
    key = as_uuid(ticket_id)
    with Session(engine) as session:
        ticket = session.get(RfpTicket, key)
        if ticket is None:
            raise TicketNotFound(str(key))
        intake = session.get(RfpHandoff, key)
        proposal = session.get(RfpProposalHandoff, key)
        if (
            intake is None
            or ticket.status not in REDRAFTABLE
            or (ticket.status == TicketStatus.FAILED.value and intake.status != "claimed")
        ):
            raise TicketStateError(
                f"a ticket in status {ticket.status!r} cannot be redrafted (only after Part 2: "
                f"{', '.join(sorted(REDRAFTABLE))})"
            )
        if proposal is not None and proposal.status == "claimed":
            raise TicketStateError("the proposal has already been claimed for approval (Part 3)")
        sections = list(
            session.execute(
                select(RfpDepartmentSection).where(col(RfpDepartmentSection.ticket_id) == key)
            ).scalars()
        )
        if any(section.approval_status not in (None, "pending") for section in sections):
            raise TicketStateError("a department owner has already decided on a section")
        session.execute(delete(RfpProposalHandoff).where(col(RfpProposalHandoff.ticket_id) == key))
        for section in sections:
            for field, value in _PART_2_RESET.items():
                setattr(section, field, value)
        now = utcnow()
        previous = ticket.status
        session.execute(
            update(RfpTicket)
            .where(col(RfpTicket.id) == key, col(RfpTicket.status) == previous)
            .values(
                status=DRAFTING,
                current_stage="queued",
                error_stage=None,
                error_message=None,
                updated_at=now,
            )
        )
        intake.status = "claimed"
        intake.claimed_at = now
        intake.claimed_by = claimed_by[:100]
        session.add(
            RfpTicketEvent(
                ticket_id=key,
                node="redraft",
                outcome="decision",
                detail={"previous_status": previous},
            )
        )
        handoff = IntakeHandoff.model_validate(intake.payload)
        session.commit()
    return handoff


def iteration_kpi(engine: Engine) -> dict[str, Any]:
    """Briefing §3: average iterations per section in the generator-evaluator loop (< 2).

    Counted on each section's Part 2 run (`draft_run_id`) from the attempts
    table, so a Part 3 revision (a run of its own) never changes the figure.
    Revisions are reported beside it.
    """
    with Session(engine) as session:
        sections = list(
            session.execute(
                select(col(RfpDepartmentSection.id), col(RfpDepartmentSection.draft_run_id)).where(
                    col(RfpDepartmentSection.draft_status).in_(sorted(FINISHED_SECTION))
                )
            ).all()
        )
        attempts = list(
            session.execute(
                select(
                    col(RfpSectionDraft.section_id),
                    col(RfpSectionDraft.run_id),
                    col(RfpSectionDraft.iteration),
                    col(RfpSectionDraft.overall_pass),
                ).where(col(RfpSectionDraft.section_id).in_([row[0] for row in sections]))
            ).all()
        )
    part_2_run: dict[UUID, UUID | None] = {row[0]: row[1] for row in sections}
    runs: dict[tuple[UUID, UUID], list[tuple[int, bool | None]]] = {}
    for section_id, run_id, iteration, overall_pass in attempts:
        runs.setdefault((section_id, run_id), []).append((iteration, overall_pass))
    part_2 = [
        sorted(rows)
        for (section_id, run_id), rows in runs.items()
        if part_2_run.get(section_id) == run_id
    ]
    revisions = [
        rows for (section_id, run_id), rows in runs.items() if part_2_run.get(section_id) != run_id
    ]
    count = len(part_2)
    return {
        "sections": count,
        "average_iterations_per_section": (
            round(sum(rows[-1][0] for rows in part_2) / count, 2) if count else None
        ),
        "target": "fewer than 2",
        "passed_first_attempt": sum(1 for rows in part_2 if rows[0] == (1, True)),
        "needs_human_review": sum(1 for rows in part_2 if rows[-1][1] is not True),
        "part_3_revisions": len(revisions),
        "part_3_revision_attempts": sum(len(rows) for rows in revisions),
    }
