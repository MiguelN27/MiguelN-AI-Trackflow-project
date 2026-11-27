"""Reads and the one write staff make: the alert list, and acknowledging an alert."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from services.operations import schemas
from services.operations.models import OperationalAlertRow
from services.operations.monitor import AlertChange, announce, lock, to_schema

StatusFilter = Literal["open", "resolved", "all"]
LIST_LIMIT = 200


class AlertNotFound(LookupError):
    """No alert has this id."""


class AlertResolved(RuntimeError):
    """The alert is resolved already: the activity came back, there is nothing to take on."""


def list_alerts(
    engine: Engine, *, status: StatusFilter, since_hours: int, now: datetime | None = None
) -> list[schemas.OperationalAlert]:
    """Open alerts (all of them), and alerts resolved within `since_hours`; newest first."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=since_hours)
    is_open = col(OperationalAlertRow.status) == "open"
    resolved_recently = (col(OperationalAlertRow.status) == "resolved") & (
        col(OperationalAlertRow.resolved_at) >= since
    )
    condition = {"open": is_open, "resolved": resolved_recently}.get(
        status, or_(is_open, resolved_recently)
    )
    with Session(engine) as session:
        rows = session.execute(
            select(OperationalAlertRow)
            .where(condition)
            .order_by(col(OperationalAlertRow.opened_at).desc())
            .limit(LIST_LIMIT)
        ).scalars()
        return [to_schema(row) for row in rows]


def acknowledge(
    engine: Engine, alert_id: UUID, *, by: str, now: datetime | None = None
) -> tuple[schemas.OperationalAlert, AlertChange | None]:
    """Mark an open alert as taken in charge, once, and announce it to every tab.

    Acknowledging again returns the alert unchanged, with no second event. Under
    the monitor's lock, so an alert being resolved at the same moment is either
    acknowledged first or refused as resolved.
    """
    now = now or datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as session:
        lock(session)
        row = session.get(OperationalAlertRow, alert_id)
        if row is None:
            raise AlertNotFound(str(alert_id))
        if row.status != "open":
            raise AlertResolved(str(alert_id))
        if row.acknowledged_at is not None:
            return to_schema(row), None
        row.acknowledged_at = now
        row.acknowledged_by = by
        session.add(row)
        session.flush()
        change = announce(session, schemas.ALERT_ACKNOWLEDGED, row)
        session.commit()
    return change.alert, change
