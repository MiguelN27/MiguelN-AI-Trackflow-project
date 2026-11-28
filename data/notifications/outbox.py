"""Write and read the live notification outbox (`live_notifications`).

Writers call `append` inside their own transaction, so an announcement commits
with the change it announces or not at all. On PostgreSQL `append` first takes
`pg_advisory_xact_lock(hashtext('live_notifications'))`: writers are serialized
until their commit, so `seq` order is commit order and a reader that has seen
seq N has seen every row below it, across every event type. The lock is
transaction-scoped, so it works behind Supabase's transaction pooler.

The stream (`services/notifications/stream.py`) reads with `notifications_after`
and `latest_seq`. Nothing here imports `services/`.
"""

from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.notifications.models import LiveNotification, NotificationModel

LOCK_KEY: Final = "live_notifications"
TABLE: Final = str(LiveNotification.__tablename__)


def initialize_schema(engine: Engine) -> None:
    """Create the outbox when missing, with RLS on and no policies (as the other tables)."""
    table = NotificationModel.metadata.tables[TABLE]
    with engine.begin() as connection:
        NotificationModel.metadata.create_all(connection, tables=[table])
        if connection.dialect.name == "postgresql":
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text(f"ALTER TABLE {quote(TABLE)} ENABLE ROW LEVEL SECURITY"))


def append(session: Session, *, event: str, subject: str, payload: dict[str, Any]) -> int:
    """Add one announcement to the caller's transaction; returns its seq (after a flush)."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": LOCK_KEY})
    row = LiveNotification(event=event, subject=subject, payload=payload)
    session.add(row)
    session.flush()
    assert row.seq is not None
    return row.seq


def _as_utc(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def latest_seq(engine: Engine) -> int:
    """The newest seq, 0 when nothing was ever announced."""
    with Session(engine) as session:
        latest = session.execute(select(func.max(col(LiveNotification.seq)))).scalar_one()
    return int(latest or 0)


def notifications_after(engine: Engine, seq: int, *, limit: int) -> list[dict[str, Any]]:
    """The announcements after `seq`, oldest first, at most `limit` of them."""
    with Session(engine) as session:
        rows = (
            session.execute(
                select(LiveNotification)
                .where(col(LiveNotification.seq) > seq)
                .order_by(col(LiveNotification.seq))
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [
        {
            "seq": row.seq,
            "event": row.event,
            "subject": row.subject,
            "payload": row.payload,
            "created_at": _as_utc(row.created_at),
        }
        for row in rows
    ]
