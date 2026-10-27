"""Telemetry endpoints: batch ingest and the technical report.

`POST /telemetry/events` is public on purpose: the plan accepts anonymous
pre-login events. Each event is validated on its own and the valid ones are
stored in `telemetry_events`.

`GET /telemetry/report` serves the operational metrics calculated from those
events. It is unauthenticated at this stage, like the incident routes: it
returns aggregates only - counts, rates, latencies per route template - and no
user, session or event identifiers.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from services.telemetry import report, service
from services.telemetry.models import TelemetryBatch, TelemetryReceipt, TelemetryReport

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


@router.get("/report", response_model=TelemetryReport)
async def get_report(
    start_date: datetime | None = Query(
        default=None,
        description="Start of the period, inclusive, in ISO 8601. Read as UTC without an "
        "offset. Defaults to seven days before end_date.",
    ),
    end_date: datetime | None = Query(
        default=None,
        description="End of the period, exclusive, in ISO 8601. Read as UTC without an "
        "offset. Defaults to now.",
    ),
) -> TelemetryReport:
    """Operational metrics for the period: volume, error rate, latency, dominant events.

    The same period asked again within 60 seconds is answered from the cache
    without recalculating.
    """
    try:
        return await run_in_threadpool(
            report.get_report, start_date, end_date, now=datetime.now(timezone.utc)
        )
    except report.InvalidPeriod as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["query", "start_date"], "msg": str(exc), "type": "value_error"}],
        ) from None
    except report.ReportUnavailable as exc:
        logger.error("telemetry report failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry report unavailable"
        ) from None
