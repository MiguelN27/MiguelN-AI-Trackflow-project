"""Stub ingest endpoint for frontend telemetry batches.

Public on purpose: the plan accepts anonymous pre-login events. Nothing is
stored yet; accepted batches are only logged.
"""

import logging
from collections import Counter

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from services.telemetry.models import TelemetryBatch, TelemetryReceipt

MAX_BODY_BYTES = 256 * 1024

logger = logging.getLogger("api.telemetry")

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post("/events", response_model=TelemetryReceipt)
async def receive_events(request: Request) -> TelemetryReceipt:
    """Accept a batch of events.

    The raw body is parsed here rather than declared as a model parameter
    because `navigator.sendBeacon` sends `text/plain` to avoid a CORS preflight.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Batch too large")

    try:
        batch = TelemetryBatch.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()],
        ) from None

    counts = Counter(event.event_type for event in batch.events)
    logger.info("telemetry batch received=%d types=%s", len(batch.events), dict(counts))
    return TelemetryReceipt(received=len(batch.events))
