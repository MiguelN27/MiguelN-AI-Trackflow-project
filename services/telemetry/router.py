"""Ingest endpoint for frontend telemetry batches.

Public on purpose: the plan accepts anonymous pre-login events. Each event is
validated on its own and the valid ones are stored in `telemetry_events`.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from services.telemetry import service
from services.telemetry.models import TelemetryBatch, TelemetryReceipt

MAX_BODY_BYTES = 256 * 1024

logger = logging.getLogger("api.telemetry")

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post("/events", response_model=TelemetryReceipt)
async def receive_events(request: Request) -> TelemetryReceipt:
    """Store the valid events of a batch and report what was stored and rejected.

    The raw body is parsed here rather than declared as a model parameter
    because `navigator.sendBeacon` sends `text/plain` to avoid a CORS preflight.
    A batch with rejected events still answers 200: resending it cannot make
    those events valid. Only a storage failure answers 5xx, which the frontend
    retries.
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

    try:
        return await run_in_threadpool(service.ingest, batch.events)
    except service.TelemetryStorageUnavailable as exc:
        logger.error("telemetry storage failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry storage unavailable"
        ) from None
