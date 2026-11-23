"""How every node of the RFP graphs runs: progress, an event row, and safe failures.

Shared by the Part 1 intake graph and the Part 2 proposal graphs. A node's
work runs inside `run_step`, which:

- moves the ticket's `current_stage` (live progress for the UI), but only
  while the ticket is in one of the statuses the graph works on;
- writes an `rfp_ticket_events` row with the duration and the tokens spent;
- turns any failure into `StageFailed`, whose message is safe to store and show.
"""

import time
from collections.abc import Callable
from typing import Any, Final

from langgraph.types import RetryPolicy
from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import conversion, store
from data.pipelines.rfp_intake.llm import IntakeModelError, InvalidModelReply


class StageFailed(RuntimeError):
    """A node failed. `message` is safe to store and show."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


# Errors whose own message is written by this package and safe to show.
_SAFE_ERRORS: Final = (
    conversion.ConversionError,
    IntakeModelError,
    store.TicketNotFound,
)


def safe_message(error: BaseException) -> str:
    if isinstance(error, _SAFE_ERRORS):
        return str(error)
    # A driver or library message can quote a connection URL or a document.
    return f"unexpected {type(error).__name__}"


def _is_invalid_reply(error: BaseException) -> bool:
    return isinstance(error, StageFailed) and isinstance(error.__cause__, InvalidModelReply)


# One more attempt when the model's reply did not match its schema. Gateway
# errors are already retried by the OpenAI client.
MODEL_RETRY: Final = RetryPolicy(max_attempts=2, initial_interval=1.0, retry_on=_is_invalid_reply)

StepWork = Callable[[], tuple[dict[str, Any], dict[str, Any] | None, int]]


def run_step(
    engine: Engine,
    ticket_id: str,
    stage: str,
    work: StepWork,
    *,
    statuses: frozenset[str] = store.ANALYZING_ONLY,
) -> dict[str, Any]:
    """Run `work` as `stage` of the ticket; its state update, event detail and tokens."""
    store.set_stage(engine, ticket_id, stage, statuses=statuses)
    started = time.perf_counter()
    try:
        update, detail, tokens = work()
    except store.TicketStateError:
        raise
    except Exception as error:
        message = safe_message(error)
        store.record_event(
            engine,
            ticket_id,
            stage,
            "failed",
            {"error": type(error).__name__, "message": message},
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise StageFailed(stage, message) from error
    store.record_event(
        engine,
        ticket_id,
        stage,
        "completed",
        detail,
        duration_ms=int((time.perf_counter() - started) * 1000),
        tokens=tokens,
    )
    return update
