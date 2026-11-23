"""The one way to run an intake: `run_intake` (API background task and CLI alike).

It never raises. Whatever happens, the ticket ends `discarded`,
`intake_complete` or `failed` with the stage that failed - "don't fail
silently". Only when the database itself is unreachable can a ticket stay
`analyzing`; the stale sweep (`store.sweep_abandoned`) then closes it.
"""

import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.agents import IntakeAgents, default_agents
from data.pipelines.rfp_intake.config import RfpIntakeSettings, get_rfp_settings
from data.pipelines.rfp_intake.graph import IntakeContext, StageFailed, get_intake_graph

logger = logging.getLogger("pipelines.rfp_intake")


def _fail(engine: Engine, ticket_id: str, stage: str, message: str) -> None:
    try:
        if store.fail(engine, ticket_id, stage=stage, message=message):
            logger.warning("rfp ticket %s failed at %s: %s", ticket_id, stage, message)
    except Exception as error:  # noqa: BLE001 - the database may be the thing that failed
        logger.error(
            "rfp ticket %s could not be marked failed (%s); the stale sweep will close it",
            ticket_id,
            type(error).__name__,
        )


def run_intake(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: IntakeAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Run the graph for one `analyzing` ticket. Returns its final status."""
    key = str(ticket_id)
    context = IntakeContext(
        engine=engine,
        agents=agents or default_agents(),
        settings=settings or get_rfp_settings(),
    )
    try:
        get_intake_graph().invoke({"ticket_id": key}, context=context)
    except StageFailed as failure:
        _fail(engine, key, failure.stage, failure.message)
    except store.TicketStateError:
        logger.warning("rfp ticket %s stopped: it is no longer analyzing", key)
    except Exception as error:  # noqa: BLE001 - a background task must not raise
        try:
            ticket = store.get_ticket(engine, key)
            stage = (ticket.current_stage if ticket else None) or "pipeline"
        except Exception:  # noqa: BLE001
            stage = "pipeline"
        _fail(engine, key, stage, f"unexpected {type(error).__name__}")

    try:
        ticket = store.get_ticket(engine, key)
    except Exception:  # noqa: BLE001
        return None
    return ticket.status if ticket else None


def reprocess(
    ticket_id: UUID | str,
    engine: Engine,
    *,
    agents: IntakeAgents | None = None,
    settings: RfpIntakeSettings | None = None,
) -> str | None:
    """Reset a `failed` or `discarded` ticket and run it again from the PDF."""
    store.reset_ticket(engine, ticket_id)
    return run_intake(ticket_id, engine, agents=agents, settings=settings)


def sweep(engine: Engine, settings: RfpIntakeSettings | None = None) -> list[UUID]:
    """Close the tickets whose run died with its process."""
    minutes = (settings or get_rfp_settings()).stale_after_minutes
    closed = store.sweep_abandoned(engine, older_than=timedelta(minutes=minutes))
    if closed:
        logger.warning("marked %d abandoned rfp ticket(s) failed", len(closed))
    return closed
