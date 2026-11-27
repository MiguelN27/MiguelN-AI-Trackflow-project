"""`GET /notifications/stream`: the backoffice's live events (Server-Sent Events). HTTP only.

`/rfp/notifications/stream` (`services/rfp/router.py`) is the same handler under
the route the RFP notifications shipped with.
"""

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sse_starlette import EventSourceResponse
from starlette.background import BackgroundTask

from services.auth.dependencies import StaffUser, oauth2_scheme
from services.core.security import decode_access_token
from services.inventory.database import get_engine
from services.notifications import stream as notifications
from services.notifications.config import get_live_stream_settings

logger = logging.getLogger("api.notifications")

router = APIRouter(prefix="/notifications", tags=["notifications"])

STREAM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    200: {
        "description": (
            "A Server-Sent Events stream. Named events: `rfp_ticket_created` (`data:` = the "
            "ticket as `GET /rfp/tickets` lists it); `operational_inactivity_detected`, "
            "`operational_activity_resumed`, `operational_alert_acknowledged` (`data:` = the "
            "alert as `GET /operations/alerts` returns it); `rfp_stream_ready` (caught up: "
            "cursor, replayed count, heartbeat); `rfp_stream_resync` (the missed events cannot "
            "be replayed: reload the snapshots once). `id:` is the outbox seq, resumed with "
            "`Last-Event-ID`. `: keep-alive` comment frames while idle."
        ),
        "content": {"text/event-stream": {}},
    },
    503: {"description": "The notification feed cannot be read; retry with backoff."},
}


async def stream_notifications(
    user: StaffUser,
    token: Annotated[str, Depends(oauth2_scheme)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    """Push every live event to this dashboard, the moment it is committed.

    Staff only, with the same bearer JWT as every other route: 401/403 before any
    event byte. `Last-Event-ID` resumes after a dropped connection: the events
    missed since that id are replayed first. The stream ends normally when the
    token expires or after `LIVE_STREAM_MAX_SECONDS`; the client resumes it.
    """
    claims = decode_access_token(token) or {}
    settings = get_live_stream_settings()
    try:
        stream = await notifications.open_stream(
            get_engine(),
            last_event_id=last_event_id,
            token_expires_at=float(claims.get("exp", 0)),
            settings=settings,
        )
    except notifications.StreamUnavailable as error:
        logger.error("live notification stream unavailable: %s", type(error).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Live notifications unavailable"
        ) from None
    return EventSourceResponse(
        stream.frames(),
        ping=settings.heartbeat_seconds,
        ping_message_factory=notifications.keep_alive,
        send_timeout=notifications.SEND_TIMEOUT_SECONDS,
        sep=notifications.SEP,
        # The generator's own cleanup does this too; this covers a stream that never started.
        background=BackgroundTask(stream.close),
    )


router.add_api_route(
    "/stream",
    stream_notifications,
    methods=["GET"],
    response_class=EventSourceResponse,
    responses=STREAM_RESPONSES,
)
