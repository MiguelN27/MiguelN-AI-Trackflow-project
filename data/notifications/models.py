"""`live_notifications`: the one outbox behind `GET /notifications/stream`.

Every live event the backoffice receives is a row here, written in the same
transaction as the change it announces: a new RFP ticket (`rfp_ticket_created`),
an operational inactivity alert opening, resolving or being acknowledged. One
table means one ordered `seq`, which is the stream's `id:` and `Last-Event-ID`,
so one cursor resumes every kind of event.

`(event, subject)` is unique: an event is announced once per subject (a ticket,
an alert), however often the code that writes it runs. It has its own registry
(`NotificationModel.metadata`), like the RFP and inventory tables, so each
domain initializes only its own tables.
"""

from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import JSON, Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

JSON_TYPE: Final = JSON().with_variant(JSONB(), "postgresql")


class NotificationModel(SQLModel, registry=registry()):
    """Base of the outbox table: its own metadata, `NotificationModel.metadata`."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveNotification(NotificationModel, table=True):
    __tablename__ = "live_notifications"
    __table_args__ = (
        UniqueConstraint("event", "subject", name="uq_live_notification_event_subject"),
    )

    seq: int | None = Field(default=None, primary_key=True)
    # The SSE event name (`rfp_ticket_created`, `operational_inactivity_detected`, ...).
    event: str = Field(max_length=64, nullable=False)
    # What the event is about: a ticket id, an alert id. Unique per event.
    subject: str = Field(max_length=200, nullable=False)
    # The event's `data`, as JSON, validated against the event's model when sent.
    payload: dict[str, Any] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
