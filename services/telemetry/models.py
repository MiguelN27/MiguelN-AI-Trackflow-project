"""Wire contract for frontend telemetry batches.

Mirrors the envelope in `docs/telemetry/event-schemas.json`. Per-event
`properties` are not validated here: the stub only proves the transport.
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
    model_config = ConfigDict(extra="forbid")

    events: list[TelemetryEvent] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class TelemetryReceipt(BaseModel):
    received: int
