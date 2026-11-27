"""`/operations/alerts`: the open (and recently resolved) inactivity alerts, and acknowledging one.

HTTP only. Staff only, like the live stream that carries the same alerts. The
list is the snapshot a dashboard loads first (and again after a fresh or
resynced connection); the live events keep it current. Acknowledging is a plain
POST whose result reaches every tab as `operational_alert_acknowledged`.
"""

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError

from services.auth.dependencies import StaffUser
from services.inventory.database import get_engine
from services.notifications import stream as notifications
from services.operations import service
from services.operations.schemas import OperationalAlert, OperationalAlertList

logger = logging.getLogger("api.operations")

router = APIRouter(prefix="/operations", tags=["operations"])


def _unavailable(error: Exception, what: str) -> HTTPException:
    logger.error("%s failed: %s", what, type(error).__name__)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Operational alerts unavailable")


@router.get("/alerts", response_model=OperationalAlertList)
def list_operational_alerts(
    user: StaffUser,
    alert_status: Annotated[Literal["open", "resolved", "all"], Query(alias="status")] = "open",
    since_hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24 * 7,
) -> OperationalAlertList:
    """Every open alert, and with `status=resolved|all` those resolved in `since_hours`."""
    try:
        alerts = service.list_alerts(get_engine(), status=alert_status, since_hours=since_hours)
    except SQLAlchemyError as error:
        raise _unavailable(error, "operational alert list") from None
    return OperationalAlertList(alerts=alerts)


@router.post("/alerts/{alert_id}/acknowledge", response_model=OperationalAlert)
def acknowledge_operational_alert(
    alert_id: UUID, user: StaffUser, background: BackgroundTasks
) -> OperationalAlert:
    """Take an open alert in charge. Again: unchanged. Resolved: 409. Unknown: 404."""
    try:
        alert, change = service.acknowledge(get_engine(), alert_id, by=user.email)
    except service.AlertNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational alert not found") from None
    except service.AlertResolved:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The activity has resumed: this alert is resolved."
        ) from None
    except SQLAlchemyError as error:
        raise _unavailable(error, "operational alert acknowledgement") from None
    if change is not None:
        background.add_task(notifications.wake)
    return alert
