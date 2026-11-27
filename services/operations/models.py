"""`operational_alerts`: one row per inactivity alert, open until the activity returns.

The partial unique index allows one **open** alert per rule and subject (a
warehouse, a job, an RFP section, a branch): however many API workers or CLI runs
evaluate the rules at once, the same silence is never announced twice. A resolved
alert stays as history; the next silence opens a new one.

Own registry (`OperationsModel.metadata`), like the RFP and outbox tables; JSON
columns are JSONB on PostgreSQL and JSON on SQLite; RLS on, no policies.
"""

from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

JSON_TYPE: Final = JSON().with_variant(JSONB(), "postgresql")
OPEN_ONLY: Final = text("status = 'open'")


class OperationsModel(SQLModel, registry=registry()):
    """Base of the operations tables: their own metadata, `OperationsModel.metadata`."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(*, nullable: bool) -> Any:
    column = Column(DateTime(timezone=True), nullable=nullable)
    return Field(default=None, sa_column=column) if nullable else Field(sa_column=column)


class OperationalAlertRow(OperationsModel, table=True):
    __tablename__ = "operational_alerts"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved')", name="ck_operational_alert_status"),
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="ck_operational_alert_resolved",
        ),
        Index(
            "uq_operational_alert_open",
            "rule_id",
            "subject_key",
            unique=True,
            postgresql_where=OPEN_ONLY,
            sqlite_where=OPEN_ONLY,
        ),
        Index("operational_alerts_opened_idx", "opened_at"),
    )

    alert_id: UUID = Field(default_factory=uuid4, primary_key=True)
    rule_id: str = Field(max_length=64, nullable=False)
    source: str = Field(max_length=32, nullable=False)
    # Identifies the subject within the rule ("Zaragoza", "<ticket_id>:warehouse", "central").
    subject_key: str = Field(max_length=200, nullable=False)
    subject: dict[str, Any] = Field(sa_column=Column(JSON_TYPE, nullable=False))
    activity: str = Field(max_length=64, nullable=False)
    label: str = Field(max_length=300, nullable=False)
    severity: str = Field(max_length=16, nullable=False)
    status: str = Field(default="open", max_length=16, nullable=False)
    expected_within_minutes: int = Field(nullable=False)
    waiting_since: datetime | None = _timestamp(nullable=True)
    last_activity_at: datetime | None = _timestamp(nullable=True)
    overdue_minutes: int | None = Field(default=None)
    details: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON_TYPE, nullable=False)
    )
    opened_at: datetime = _timestamp(nullable=False)
    resolved_at: datetime | None = _timestamp(nullable=True)
    resolved_reason: str | None = Field(default=None, max_length=32)
    acknowledged_at: datetime | None = _timestamp(nullable=True)
    acknowledged_by: str | None = Field(default=None, max_length=254)


TABLE: Final = str(OperationalAlertRow.__tablename__)


def initialize_schema(engine: Engine) -> None:
    """Create the alerts table when missing, with RLS on and no policies."""
    table = OperationsModel.metadata.tables[TABLE]
    with engine.begin() as connection:
        OperationsModel.metadata.create_all(connection, tables=[table])
        if connection.dialect.name == "postgresql":
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text(f"ALTER TABLE {quote(TABLE)} ENABLE ROW LEVEL SECURITY"))
