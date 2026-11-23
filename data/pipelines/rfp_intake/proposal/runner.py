"""The ways to run Part 2: drain the queue, draft one ticket, or redraft it.

- `run_pending` takes Part 1's handoffs from the queue (`handoff.claim_next`,
  FOR UPDATE SKIP LOCKED) and drafts each one. The API runs it as a
  background task right after an intake, so drafting follows automatically.
- `draft_ticket` claims and drafts one specific ticket (CLI).
- `redraft` starts Part 2 again for a ticket that finished or failed it (CLI).

`run_drafting` never raises. A ticket always ends `waiting_for_approval`,
`needs_human_review` or `failed` (with the stage that failed). Only when the
database itself is unreachable can it stay `drafting` / `under_evaluation`;
the stale sweep then closes it.
"""

import logging
import os
import socket
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from data.pipelines.rfp_intake import handoff, store
from data.pipelines.rfp_intake.config import RfpIntakeSettings, get_rfp_settings
from data.pipelines.rfp_intake.handoff import IntakeHandoff
from data.pipelines.rfp_intake.proposal import store as proposal_store
from data.pipelines.rfp_intake.proposal.agents import ProposalAgents, default_agents
from data.pipelines.rfp_intake.proposal.graph import ProposalContext, get_proposal_graph
from data.pipelines.rfp_intake.steps import StageFailed

logger = logging.getLogger("pipelines.rfp_intake.proposal")


def worker_name(prefix: str = "drafting") -> str:
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"[:100]


def _fail(engine: Engine, ticket_id: str, stage: str, message: str) -> None:
    try:
        if store.fail(
            engine, ticket_id, stage=stage, message=message, statuses=store.PART_2_ACTIVE
        ):
            proposal_store.close_unfinished_sections(engine, ticket_id)
            logger.warning("rfp ticket %s drafting failed at %s: %s", ticket_id, stage, message)
    except Exception as error:  # noqa: BLE001 - the database may be the thing that failed
        logger.error(
            "rfp ticket %s could not be marked failed (%s); the stale sweep will close it",
            ticket_id,
            type(error).__name__,
        )


def run_drafting(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    handoff_payload: IntakeHandoff,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Run the Part 2 graph for one claimed ticket. Returns its final status."""
    key = str(ticket_id)
    context = ProposalContext(
        engine=engine,
        agents=agents or default_agents(),
        settings=settings or get_rfp_settings(),
    )
    try:
        get_proposal_graph().invoke(
            {
                "ticket_id": key,
                "run_id": str(uuid4()),
                "handoff": handoff_payload.model_dump(mode="json"),
            },
            context=context,
        )
    except StageFailed as failure:
        _fail(engine, key, failure.stage, failure.message)
    except store.TicketStateError:
        logger.warning("rfp ticket %s stopped: it is no longer being drafted", key)
    except Exception as error:  # noqa: BLE001 - a background task must not raise
        try:
            ticket = store.get_ticket(engine, key)
            stage = (ticket.current_stage if ticket else None) or "drafting"
        except Exception:  # noqa: BLE001
            stage = "drafting"
        _fail(engine, key, stage, f"unexpected {type(error).__name__}")

    try:
        ticket = store.get_ticket(engine, key)
    except Exception:  # noqa: BLE001
        return None
    return ticket.status if ticket else None


def run_pending(
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
    limit: int | None = None,
    claimed_by: str | None = None,
) -> list[tuple[UUID, str | None]]:
    """Draft queued handoffs, oldest first, until the queue is empty or `limit`. Never raises."""
    settings = settings or get_rfp_settings()
    batch = limit if limit is not None else settings.draft_batch_size
    name = claimed_by or worker_name()
    done: list[tuple[UUID, str | None]] = []
    while len(done) < batch:
        try:
            claimed = handoff.claim_next(engine, claimed_by=name)
        except SQLAlchemyError as error:
            logger.error("rfp drafting queue unavailable (%s)", type(error).__name__)
            break
        if claimed is None:
            break
        ticket_id = claimed.handoff.ticket_id
        status = run_drafting(
            ticket_id, engine, handoff_payload=claimed.handoff, agents=agents, settings=settings
        )
        logger.info("rfp ticket %s drafted: %s", ticket_id, status)
        done.append((ticket_id, status))
    return done


def draft_ticket(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Claim one ticket's pending handoff and draft it. TicketStateError if none is pending."""
    claimed = handoff.claim(engine, ticket_id, claimed_by=worker_name("cli"))
    if claimed is None:
        raise store.TicketStateError(
            "this ticket has no pending handoff to draft (it must be intake_complete; use "
            "redraft after Part 2)"
        )
    return run_drafting(
        ticket_id, engine, handoff_payload=claimed.handoff, agents=agents, settings=settings
    )


def redraft(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: ProposalAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Run Part 2 again for a ticket that finished or failed it."""
    intake = proposal_store.reset_for_redraft(engine, ticket_id, claimed_by=worker_name("cli"))
    return run_drafting(ticket_id, engine, handoff_payload=intake, agents=agents, settings=settings)
