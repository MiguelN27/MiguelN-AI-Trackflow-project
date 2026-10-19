"""Wire contract for frontend telemetry batches.

`TelemetryEvent` mirrors the envelope in `docs/telemetry/event-schemas.json` and
is the contract every stored event meets. Per-event `properties` are not checked
against their individual schemas yet.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_BATCH_SIZE = 100


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eventId: UUID
    timestamp: datetime
    sessionId: UUID
    userId: UUID | None
    event_type: str = Field(pattern=r"^[a-z]+(_[a-z]+)+$", max_length=64)
    schemaVersion: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    requestId: UUID
    properties: dict[str, Any]


class TelemetryBatch(BaseModel):
    """The `{"events": [...]}` envelope, with each event left unparsed.

    Events are validated one by one afterwards, so a bad event is rejected on
    its own instead of failing the whole batch.
    """

    model_config = ConfigDict(extra="forbid")

    events: list[Any] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class TelemetryReceipt(BaseModel):
    """`received` is always `stored + rejected`."""

    received: int
    stored: int
    rejected: int
