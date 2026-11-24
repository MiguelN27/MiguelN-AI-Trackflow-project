"""What Sales and the owners read about a ticket's approval (API and CLI).

`ticket_detail` is Part 1's ticket detail plus, per section, its approval state
(who approves it, the round, whether the reader may decide, every decision so
far), an `approval` block (run status, arbitration rulings, any failure) and a
summary of the final document. It reads the database only, never the
checkpointer, so the UI can poll it.
"""

from typing import Any

from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import approvers, decisions
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.models import RfpApprovalDecision


def decision_fields(row: RfpApprovalDecision) -> dict[str, Any]:
    return {
        "decision_id": row.id,
        "department_id": row.department_id,
        "review_round": row.review_round,
        "action": row.action,
        "comment": row.comment,
        "source": row.source,
        "decided_by": row.decided_by,
        "decided_by_name": row.decided_by_name,
        "trigger_id": row.trigger_id,
        "status": row.status,
        "status_note": row.status_note,
        "created_at": store.as_utc(row.created_at),
        "applied_at": store.as_utc(row.applied_at),
    }


def _section_approval(
    section: dict[str, Any],
    row: Any,
    *,
    ticket_status: str,
    run_status: str | None,
    history: list[dict[str, Any]],
    viewer_email: str | None,
    settings: RfpIntakeSettings,
) -> dict[str, Any]:
    dept = section["department_id"]
    used = (row.review_rounds or 0) if row is not None else 0
    review_round = used + 1
    status = section.get("approval_status")
    pending_owner_decision = any(
        d["status"] == "recorded" and d["review_round"] == review_round for d in history
    )
    awaiting = (
        ticket_status == approval_store.WAITING
        and run_status in ("paused", "running", "failed")
        and status == "pending"
    )
    return {
        "approver": row.approver if row is not None else None,
        "approved_by": row.approved_by if row is not None else None,
        "approved_at": store.as_utc(row.approved_at) if row is not None else None,
        "review_round": review_round,
        "review_rounds": used,
        "max_review_rounds": settings.max_revision_rounds,
        "allowed_actions": decisions.allowed_actions(used, settings.max_revision_rounds),
        "approver_configured": approvers.approver_email(settings, dept) is not None,
        "awaiting_decision": awaiting and not pending_owner_decision,
        "decision_pending": pending_owner_decision,
        "can_decide": (
            awaiting
            and not pending_owner_decision
            and approvers.is_owner(settings, dept, viewer_email)
        ),
        "decisions": history,
    }


def ticket_detail(
    engine: Engine,
    ticket_id: Any,
    *,
    viewer_email: str | None,
    settings: RfpIntakeSettings,
) -> dict[str, Any] | None:
    detail = store.get_ticket_detail(engine, ticket_id)
    if detail is None:
        return None
    run = approval_store.get_run(engine, ticket_id)
    rows = {row.department_id: row for row in approval_store.load_sections(engine, ticket_id)}
    history: dict[str, list[dict[str, Any]]] = {}
    for decision in approval_store.decisions_for(engine, ticket_id):
        history.setdefault(decision.department_id, []).append(decision_fields(decision))
    for section in detail["sections"]:
        section.update(
            _section_approval(
                section,
                rows.get(section["department_id"]),
                ticket_status=detail["status"],
                run_status=run.status if run else None,
                history=history.get(section["department_id"], []),
                viewer_email=viewer_email,
                settings=settings,
            )
        )
    active = [s for s in detail["sections"] if s.get("approval_status")]
    detail["approval"] = (
        {
            "status": run.status,
            "thread_id": run.thread_id,
            "started_at": store.as_utc(run.started_at),
            "finished_at": store.as_utc(run.finished_at),
            "arbitration_rounds": run.arbitration_rounds,
            "max_arbitration_rounds": settings.max_arbitration_rounds,
            "rulings": list(run.rulings),
            "error_stage": run.error_stage,
            "error_message": run.error_message,
            "approved": sum(1 for s in active if s["approval_status"] == "approved"),
            "sections": len(active),
            "viewer_departments": approvers.departments_of(settings, viewer_email),
        }
        if run
        else None
    )
    document = approval_store.get_document(engine, ticket_id)
    detail["final_document"] = (
        {
            "generated_at": store.as_utc(document.generated_at),
            "currency": document.currency,
            "content_sha256": document.content_sha256,
            "sections": [section["department_id"] for section in document.sections],
        }
        if document
        else None
    )
    return detail


def document_fields(engine: Engine, ticket_id: Any) -> dict[str, Any] | None:
    """The stored final document (briefing §2.3 fields plus the Markdown)."""
    document = approval_store.get_document(engine, ticket_id)
    if document is None:
        return None
    return {
        "ticket_id": document.ticket_id,
        "rfp_id": document.rfp_id,
        "client_name": document.client_name,
        "currency": document.currency,
        "sections": document.sections,
        "generated_at": store.as_utc(document.generated_at),
        "content": document.content,
        "content_sha256": document.content_sha256,
        "approval_thread_id": document.approval_thread_id,
    }
