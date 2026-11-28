"""How Part 3 runs: start a ticket's approval, record decisions, and resume the graph.

- `start_pending` drains Part 2's queue (`proposal/handoff.claim_next`, SKIP
  LOCKED). `start` runs one claimed ticket's graph from the beginning, until
  every department's branch is paused for its owner. The API runs it as a
  background task right after drafting (`RFP_AUTO_REQUEST_APPROVAL`).
- `submit_decision` validates an owner's decision (owner account, round,
  draft, comment, revision limit) and stores it as `recorded`. It does not touch
  the graph, so the API answers at once.
- `apply_decisions` is the explicit resume entry point. It takes the
  ticket's lease, matches each recorded decision to its department's open
  interrupt, and resumes the graph with `Command(resume={interrupt_id: ...})`.
  Several departments resume in one call; the others stay paused. A resume
  never re-runs `open_approval`, nor Parts 1 and 2. After a rejection, the
  other open requests are withdrawn (a system decision), so the run can end.
- `resume` continues a run that stopped on a technical failure, from its last
  checkpoint (`invoke(None)`). `recover` does it at API start-up, and applies
  any decision a previous process left recorded.

Nothing here raises to a background task: failures are logged and stored
(`approval_store.fail_run`), and the ticket keeps every decision.
"""

import logging
import os
import socket
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import approvers, checkpoint, decisions
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.approval.graph import (
    REVIEW,
    ApprovalContext,
    ApprovalGraph,
    get_approval_graph,
    graph_version,
)
from data.pipelines.rfp_intake.config import RfpIntakeSettings, get_rfp_settings
from data.pipelines.rfp_intake.models import utcnow
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal.agents import ProposalAgents, default_agents
from data.pipelines.rfp_intake.proposal.handoff import ProposalHandoff
from data.pipelines.rfp_intake.steps import StageFailed

logger = logging.getLogger("pipelines.rfp_intake.approval")

# Bound on resume rounds in one `apply_decisions` call (each needs a new decision).
MAX_ROUNDS_PER_APPLY = 50


def worker_name(prefix: str = "approval") -> str:
    """Unique per call: two threads of one process must not share a lease."""
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"[:100]


def _context(
    engine: Engine, agents: ProposalAgents | None, settings: RfpIntakeSettings | None
) -> ApprovalContext:
    return ApprovalContext(
        engine=engine,
        agents=agents or default_agents(),
        settings=settings or get_rfp_settings(),
    )


def graph_for(engine: Engine, settings: RfpIntakeSettings | None = None) -> ApprovalGraph:
    schema = (settings or get_rfp_settings()).checkpoint_schema
    return get_approval_graph(checkpoint.get_checkpointer(engine, schema))


def _config(ticket_id: UUID | str) -> RunnableConfig:
    return {"configurable": {"thread_id": checkpoint.thread_id(ticket_id)}}


def _lease_ttl(settings: RfpIntakeSettings) -> timedelta:
    return timedelta(minutes=settings.stale_after_minutes)


def open_requests(
    engine: Engine, ticket_id: UUID | str, settings: RfpIntakeSettings | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """(interrupt id, `ApprovalRequest`) for every department paused for its owner."""
    snapshot = graph_for(engine, settings).get_state(_config(ticket_id))
    return [
        (item.id, dict(item.value))
        for item in snapshot.interrupts
        if isinstance(item.value, dict) and item.value.get("kind") == "department_approval"
    ]


def state_trace(
    engine: Engine, ticket_id: UUID | str, settings: RfpIntakeSettings | None = None
) -> list[dict[str, Any]]:
    """The graph state's trace: every node execution, with agent, input, output and times.

    A finished branch's entries are in the run's state. A branch still paused for
    its owner keeps them in its own checkpoint namespace (`review_department:<task>`),
    read here from the checkpointer.
    """
    graph = graph_for(engine, settings)
    snapshot = graph.get_state(_config(ticket_id))
    entries = list(snapshot.values.get("trace") or [])
    saver = graph.checkpointer
    if isinstance(saver, BaseCheckpointSaver):
        for task in snapshot.tasks:
            if task.name != REVIEW or not task.interrupts:
                continue
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": checkpoint.thread_id(ticket_id),
                    "checkpoint_ns": f"{task.name}:{task.id}",
                }
            }
            saved = saver.get_tuple(config)
            if saved is not None:
                entries.extend(saved.checkpoint["channel_values"].get("trace") or [])
    unique = {(entry["node"], entry["started_at"]): entry for entry in entries}
    return sorted(unique.values(), key=lambda entry: (entry["started_at"], entry["finished_at"]))


def _after_invoke(engine: Engine, ticket_id: UUID | str, settings: RfpIntakeSettings) -> str | None:
    """Record where the run stands: paused (and for whom), or finished."""
    run = approval_store.get_run(engine, ticket_id)
    if run is not None and run.status in ("done", "rejected"):
        return run.status
    waiting = [
        request["department_id"] for _, request in open_requests(engine, ticket_id, settings)
    ]
    approval_store.set_run(engine, ticket_id, status="paused" if waiting else "running")
    approval_store.set_waiting_stage(engine, ticket_id, waiting)
    return "paused" if waiting else "running"


def _invoke(
    graph: ApprovalGraph,
    graph_input: Any,
    ticket_id: UUID | str,
    context: ApprovalContext,
) -> bool:
    """One graph call. False after a failure, which is stored on the run and the ticket."""
    try:
        graph.invoke(graph_input, _config(ticket_id), context=context)
    except StageFailed as failure:
        approval_store.fail_run(
            context.engine, ticket_id, stage=failure.stage, message=failure.message
        )
        logger.warning(
            "rfp ticket %s approval failed at %s: %s", ticket_id, failure.stage, failure.message
        )
        return False
    except store.TicketStateError as error:
        logger.warning("rfp ticket %s approval stopped: %s", ticket_id, error)
        return False
    except Exception as error:  # noqa: BLE001 - a background task must not raise
        try:
            ticket = store.get_ticket(context.engine, ticket_id)
            stage = (ticket.current_stage if ticket else None) or "approval"
            approval_store.fail_run(
                context.engine, ticket_id, stage=stage, message=f"unexpected {type(error).__name__}"
            )
        except Exception:  # noqa: BLE001 - the database may be what failed
            pass
        logger.error("rfp ticket %s approval failed: %s", ticket_id, type(error).__name__)
        return False
    return True


def _status(engine: Engine, ticket_id: UUID | str) -> str | None:
    try:
        ticket = store.get_ticket(engine, ticket_id)
    except Exception:  # noqa: BLE001
        return None
    return ticket.status if ticket else None


# --- Starting --------------------------------------------------------------------------------


def start(
    proposal: ProposalHandoff,
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Run a claimed proposal's approval graph until every branch waits for its owner."""
    context = _context(engine, agents, settings)
    ticket_id = proposal.ticket_id
    thread = checkpoint.thread_id(ticket_id)
    graph = graph_for(engine, context.settings)
    if graph.get_state(_config(ticket_id)).values:
        raise store.TicketStateError(
            "this ticket's approval graph has already started; resume it instead"
        )
    approval_store.create_run(engine, ticket_id, thread_id=thread, graph_version=graph_version())
    worker = worker_name()
    approval_store.acquire_lease(engine, ticket_id, worker, ttl=_lease_ttl(context.settings))
    try:
        if _invoke(
            graph,
            {
                "ticket_id": str(ticket_id),
                "thread_id": thread,
                "proposal": proposal.model_dump(mode="json", by_alias=True),
            },
            ticket_id,
            context,
        ):
            _after_invoke(engine, ticket_id, context.settings)
    finally:
        approval_store.release_lease(engine, ticket_id, worker)
    # Decisions can arrive while the graph was starting.
    return apply_decisions(ticket_id, engine, agents=context.agents, settings=context.settings)


def start_pending(
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
    limit: int | None = None,
    claimed_by: str | None = None,
) -> list[tuple[UUID, str | None]]:
    """Open approval for queued proposals, oldest first. Never raises."""
    settings = settings or get_rfp_settings()
    batch = limit if limit is not None else settings.draft_batch_size
    name = claimed_by or worker_name()
    started: list[tuple[UUID, str | None]] = []
    while len(started) < batch:
        try:
            claimed = proposal_handoff.claim_next(engine, claimed_by=name)
        except SQLAlchemyError as error:
            logger.error("rfp approval queue unavailable (%s)", type(error).__name__)
            break
        if claimed is None:
            break
        ticket_id = claimed.proposal.ticket_id
        try:
            status = start(claimed.proposal, engine, agents=agents, settings=settings)
        except Exception as error:  # noqa: BLE001 - one ticket must not stop the queue
            logger.error(
                "rfp ticket %s approval could not start: %s", ticket_id, type(error).__name__
            )
            status = _status(engine, ticket_id)
        logger.info("rfp ticket %s approval opened: %s", ticket_id, status)
        started.append((ticket_id, status))
    return started


# --- Decisions --------------------------------------------------------------------------------


def submit_decision(
    engine: Engine,
    ticket_id: UUID | str,
    department_id: str,
    *,
    action: str,
    comment: str | None,
    review_round: int,
    decided_by_email: str,
    settings: RfpIntakeSettings | None = None,
) -> dict[str, Any]:
    """Validate an owner's decision and record it. `DecisionRefused` explains a refusal.

    The graph is not touched: `apply_decisions` resumes it.
    """
    settings = settings or get_rfp_settings()
    ticket = store.get_ticket(engine, ticket_id)
    if ticket is None:
        raise store.TicketNotFound(str(ticket_id))
    try:
        section = approval_store.load_section(engine, ticket_id, department_id)
    except store.TicketNotFound:
        raise decisions.DecisionRefused(
            "not_found", f"This proposal has no {department_id} section."
        ) from None
    if ticket.status != approval_store.WAITING or section.approval_status != "pending":
        raise decisions.DecisionRefused(
            "not_waiting",
            "This section is not waiting for a decision"
            + (
                " (it is being revised)."
                if section.approval_status == "changes_requested"
                else f" (it is {section.approval_status or 'not in approval yet'})."
            ),
        )
    current_round = (section.review_rounds or 0) + 1
    sha = decisions.draft_sha256(section.draft_content)
    cleaned = decisions.clean_comment(comment)
    decisions.check_decision(
        settings=settings,
        department_id=department_id,
        review_round=review_round,
        draft_sha=sha,
        revisions_used=section.review_rounds or 0,
        action=action,
        comment=cleaned,
        source="owner",
        decided_by_email=decided_by_email,
        expected_round=current_round,
        expected_sha=sha,
    )
    if cleaned and len(cleaned) > decisions.COMMENT_MAX_CHARS:
        raise decisions.DecisionRefused(
            "invalid", f"Keep the comment under {decisions.COMMENT_MAX_CHARS} characters."
        )
    try:
        row = approval_store.record_decision(
            engine,
            ticket_id=ticket_id,
            section_id=section.id,
            department_id=department_id,
            review_round=current_round,
            draft_sha256=sha,
            action=action,
            comment=cleaned,
            source="owner",
            decided_by=decided_by_email.strip().lower(),
            decided_by_name=approvers.owner_of(department_id),
        )
    except IntegrityError:
        raise decisions.DecisionRefused(
            "already_decided", "A decision on this round of the section is already recorded."
        ) from None
    return _decision_fields(row)


def _decision_fields(row: Any) -> dict[str, Any]:
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


def _payload(row: Any) -> dict[str, Any]:
    """A stored decision as the graph's resume value (`ApprovalDecision`)."""
    owner = row.source == "owner"
    return decisions.ApprovalDecision(
        decision_id=row.id,
        department_id=row.department_id,
        review_round=row.review_round,
        draft_sha256=row.draft_sha256 or "",
        action=row.action,
        comment=row.comment,
        decided_by=decisions.Decider(
            source="owner" if owner else "system",
            email=row.decided_by if owner else None,
            name=row.decided_by_name,
        ),
        decided_at=store.as_utc(row.created_at) or utcnow(),
    ).model_dump(mode="json")


def _withdraw_open_requests(
    engine: Engine, ticket_id: UUID | str, requests: list[tuple[str, dict[str, Any]]]
) -> None:
    """After a rejection, every other open request ends: a system `withdrawn` decision."""
    rejected = approval_store.rejected_departments(engine, ticket_id)
    if not rejected:
        return
    pending = {row.department_id for row in approval_store.recorded_decisions(engine, ticket_id)}
    for _, request in requests:
        dept = request["department_id"]
        if dept in rejected or dept in pending:
            continue
        approval_store.record_decision(
            engine,
            ticket_id=ticket_id,
            section_id=request["section_id"],
            department_id=dept,
            review_round=request["review_round"],
            draft_sha256=request["draft_sha256"],
            action="withdrawn",
            comment=f"{', '.join(rejected)} was rejected, so the proposal does not go out.",
            source="system",
            decided_by="system",
            decided_by_name="system",
        )


def _resume_map(
    engine: Engine, ticket_id: UUID | str, requests: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """{interrupt id: decision} for every recorded decision that answers an open request."""
    open_by_dept = {request["department_id"]: (iid, request) for iid, request in requests}
    resume: dict[str, Any] = {}
    for row in approval_store.recorded_decisions(engine, ticket_id):
        match = open_by_dept.get(row.department_id)
        if match is None or match[0] in resume:
            approval_store.mark_decision(
                engine, row.id, "stale", "No open request matched it when it was applied."
            )
            continue
        interrupt_id, request = match
        if row.review_round != request["review_round"] or (
            row.draft_sha256 and row.draft_sha256 != request["draft_sha256"]
        ):
            approval_store.mark_decision(
                engine, row.id, "stale", "The section had moved on (revised or decided)."
            )
            continue
        resume[interrupt_id] = _payload(row)
    return resume


def apply_decisions(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Resume the ticket's graph with every recorded decision. Returns the ticket's status.

    One process at a time: without the lease this returns at once, and the
    holder applies the decision in its next round.
    """
    context = _context(engine, agents, settings)
    graph = graph_for(engine, context.settings)
    if approval_store.get_run(engine, ticket_id) is None:
        return _status(engine, ticket_id)
    for _ in range(MAX_ROUNDS_PER_APPLY):
        worker = worker_name()
        if not approval_store.acquire_lease(
            engine, ticket_id, worker, ttl=_lease_ttl(context.settings)
        ):
            return _status(engine, ticket_id)
        failed = False
        try:
            for _ in range(MAX_ROUNDS_PER_APPLY):
                requests = open_requests(engine, ticket_id, context.settings)
                _withdraw_open_requests(engine, ticket_id, requests)
                resume_values = _resume_map(engine, ticket_id, requests)
                if not resume_values:
                    break
                if not _invoke(graph, Command(resume=resume_values), ticket_id, context):
                    failed = True  # stored on the run; `resume` continues it
                    break
                _after_invoke(engine, ticket_id, context.settings)
        finally:
            approval_store.release_lease(engine, ticket_id, worker)
        if failed:
            break
        # A decision recorded while the lease was held, after the last check, is taken now.
        requests = open_requests(engine, ticket_id, context.settings)
        if not _resume_map_preview(engine, ticket_id, requests):
            break
    return _status(engine, ticket_id)


def _resume_map_preview(
    engine: Engine, ticket_id: UUID | str, requests: list[tuple[str, dict[str, Any]]]
) -> bool:
    """Is there a recorded decision for an open request (without marking anything)?"""
    open_depts = {request["department_id"] for _, request in requests}
    if approval_store.rejected_departments(engine, ticket_id) and open_depts:
        return True
    return any(
        row.department_id in open_depts
        for row in approval_store.recorded_decisions(engine, ticket_id)
    )


# --- Recovery ------------------------------------------------------------------------------------


def resume(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Continue a run that stopped on a failure, from its last checkpoint; then apply decisions."""
    context = _context(engine, agents, settings)
    run = approval_store.get_run(engine, ticket_id)
    if run is None:
        raise store.TicketStateError("this ticket's approval has not started")
    if run.status in ("done", "rejected"):
        return _status(engine, ticket_id)
    graph = graph_for(engine, context.settings)
    snapshot = graph.get_state(_config(ticket_id))
    if not snapshot.next:
        return _status(engine, ticket_id)
    if run.graph_version != graph_version():
        store.record_event(
            engine,
            ticket_id,
            "resume",
            "warning",
            {"part": 3, "graph_version": run.graph_version, "current": graph_version()},
        )
    worker = worker_name()
    if not approval_store.acquire_lease(
        engine, ticket_id, worker, ttl=_lease_ttl(context.settings)
    ):
        raise store.TicketStateError("another worker is resuming this ticket right now")
    try:
        pending_tasks = [task for task in snapshot.tasks if not task.interrupts]
        if pending_tasks or run.status == "failed":
            approval_store.clear_failure(engine, ticket_id)
            if _invoke(graph, None, ticket_id, context):
                _after_invoke(engine, ticket_id, context.settings)
    finally:
        approval_store.release_lease(engine, ticket_id, worker)
    return apply_decisions(ticket_id, engine, agents=context.agents, settings=context.settings)


def recover(
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> list[UUID]:
    """At start-up: resume runs a dead process left behind, and apply stranded decisions."""
    touched: list[UUID] = []
    try:
        stalled = approval_store.runs_to_recover(engine)
        waiting = approval_store.tickets_with_recorded_decisions(engine)
    except SQLAlchemyError as error:
        logger.error("rfp approval recovery unavailable (%s)", type(error).__name__)
        return touched
    for ticket_id in dict.fromkeys([*stalled, *waiting]):
        try:
            if ticket_id in stalled:
                resume(ticket_id, engine, agents=agents, settings=settings)
            else:
                apply_decisions(ticket_id, engine, agents=agents, settings=settings)
            touched.append(ticket_id)
        except Exception as error:  # noqa: BLE001 - one ticket must not stop the others
            logger.error(
                "rfp ticket %s approval recovery failed: %s", ticket_id, type(error).__name__
            )
    return touched
