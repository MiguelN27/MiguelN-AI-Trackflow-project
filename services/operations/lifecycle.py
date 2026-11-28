"""The monitor's place in the API lifespan (`services/main.py`).

Start-up validates the rules file (a bad rule refuses start-up, like any
configuration error), creates the alerts table and the live outbox when
missing, and starts the monitor: one asyncio task that evaluates the rules
every `OPS_MONITOR_INTERVAL_SECONDS` in a worker thread, then wakes the live
stream when an alert opened or resolved. A failing round is logged and the next
one runs as usual: the monitor never takes the API down. Shutdown cancels it.
"""

import asyncio
import contextlib
import logging

import anyio
from sqlalchemy.exc import SQLAlchemyError

from data.notifications import outbox
from services.core.config import ConfigurationError
from services.inventory.database import get_engine
from services.notifications import stream as notifications
from services.operations import models
from services.operations.config import get_operations_settings
from services.operations.monitor import evaluate
from services.operations.rules import RulesError, get_rules

logger = logging.getLogger("api.operations")

_task: asyncio.Task[None] | None = None


def initialize_rules() -> None:
    """Validate the rules file; an invalid one stops start-up, naming the file and the problem."""
    try:
        get_rules()
    except RulesError as error:
        raise ConfigurationError(f"Operational activity rules: {error}") from None


def initialize() -> None:
    initialize_rules()
    engine = get_engine()
    try:
        outbox.initialize_schema(engine)
        models.initialize_schema(engine)
    except SQLAlchemyError:
        raise ConfigurationError(
            "Operational alerts schema initialization failed. Check PostgreSQL permissions "
            "and availability."
        ) from None


async def run_once() -> int:
    """One evaluation in a worker thread; wakes the stream if anything changed."""
    try:
        changes = await anyio.to_thread.run_sync(lambda: evaluate(get_engine(), get_rules()))
    except Exception as error:  # noqa: BLE001 - logged, retried next round
        logger.error("operational monitor round failed: %s", type(error).__name__)
        return 0
    if changes:
        notifications.wake()
    return len(changes)


async def _loop(interval: float) -> None:
    while True:
        await run_once()
        await asyncio.sleep(interval)


def start() -> None:
    global _task
    settings = get_operations_settings()
    if not settings.monitor_enabled or (_task is not None and not _task.done()):
        return
    _task = asyncio.create_task(_loop(settings.monitor_interval_seconds), name="ops-monitor")
    logger.info(
        "operational monitor started: %d rules, every %.0f s",
        len(get_rules().rules),
        settings.monitor_interval_seconds,
    )


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
