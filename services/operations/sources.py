"""Where each rule looks for its expected activity: one `Watch` per subject.

A watch says what is watched (`subject`), since when the expected period runs
(`waiting_since`: the last activity, or when a slot opened; None when the
activity never happened) and the evidence for the alert (`details`). The
monitor decides from it whether the subject is overdue.

All reads are plain queries in their own short sessions: the monitor never
holds a database transaction open while it reads, and a source whose table does
not exist yet (a fresh database, a pipeline that never ran) simply has no
activity. Nothing here writes.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, col
from trackflow_shared.incidents import BRANCH_LABELS, IncidentBranch, IncidentStatus

from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID, TicketStatus
from data.pipelines.rfp_intake.models import RfpDepartmentSection, RfpMetadata, RfpTicket
from services.incidents import db as incidents_db
from services.inventory.models import InboundOrder, OutboundOrder, Product
from services.job_runner.models import job_runs
from services.operations.rules import (
    IncidentsRule,
    RfpApprovalRule,
    Rule,
    ScheduledJobRule,
    WarehouseOrdersRule,
)
from services.operations.schedule import active_minutes_between

# The weekly report's run log (`data/pipelines/.../schema.py`): `reporting.pipeline_runs`.
PIPELINE_SCHEMA = "reporting"
JOB_LABELS = {
    "nightly_export": "The nightly telemetry export",
    "weekly_warehouse_client_performance": "The weekly warehouse & client report",
}


@dataclass(frozen=True)
class Watch:
    subject_key: str
    subject: dict[str, str | None]
    label: str
    waiting_since: datetime | None
    last_activity_at: datetime | None
    details: dict[str, Any] = field(default_factory=dict)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; they were written in UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _has_table(engine: Engine, name: str, schema: str | None = None) -> bool:
    if schema is not None and engine.dialect.name != "postgresql":
        return False
    return inspect(engine).has_table(name, schema=schema)


def observe(engine: Engine, rule: Rule, *, now: datetime) -> list[Watch]:
    if isinstance(rule, WarehouseOrdersRule):
        return [_warehouse_orders(engine, rule)]
    if isinstance(rule, ScheduledJobRule):
        return [_scheduled_job(engine, rule)]
    if isinstance(rule, RfpApprovalRule):
        return _rfp_approvals(engine, rule)
    return _incidents(rule, now=now)


# --- Locations: a warehouse registers orders ----------------------------------------------


def _warehouse_orders(engine: Engine, rule: WarehouseOrdersRule) -> Watch:
    order = OutboundOrder if rule.activity == "outbound_order_created" else InboundOrder
    query = (
        select(func.max(col(order.created_at)))
        .join(Product, col(Product.id) == col(order.product_id))
        .where(col(Product.warehouse) == rule.warehouse)
    )
    if rule.client_id is not None:
        query = query.where(col(Product.client_id) == rule.client_id)
    with Session(engine) as session:
        last = as_utc(session.execute(query).scalar_one())
    warehouse = rule.warehouse.value
    noun = "outbound order" if rule.activity == "outbound_order_created" else "inbound receipt"
    client = f" for {rule.client_id}" if rule.client_id else ""
    return Watch(
        subject_key=f"{warehouse}/{rule.client_id}" if rule.client_id else warehouse,
        subject={"warehouse": warehouse, "client_id": rule.client_id},
        label=f"No {noun} registered at {warehouse}{client}",
        waiting_since=last,
        last_activity_at=last,
        details={"orders_table": str(order.__tablename__)},
    )


# --- Processes: a scheduled job completes ---------------------------------------------------


def _scheduled_job(engine: Engine, rule: ScheduledJobRule) -> Watch:
    if rule.job_name == "nightly_export":
        completed, latest = _job_runs(engine, rule.job_name)
    else:
        completed, latest = _pipeline_runs(engine, rule.job_name)
    details: dict[str, Any] = {}
    if latest is not None:
        details = {"last_run_status": latest[0], "last_run_at": _iso(as_utc(latest[1]))}
    return Watch(
        subject_key=rule.job_name,
        subject={"job_name": rule.job_name},
        label=f"{JOB_LABELS[rule.job_name]} has not completed",
        waiting_since=completed,
        last_activity_at=completed,
        details=details,
    )


def _job_runs(engine: Engine, job_name: str) -> tuple[datetime | None, tuple[str, Any] | None]:
    if not _has_table(engine, str(job_runs.name)):
        return None, None
    with engine.connect() as connection:
        completed = connection.execute(
            select(func.max(job_runs.c.finished_at)).where(
                job_runs.c.job_name == job_name, job_runs.c.status == "completed"
            )
        ).scalar_one()
        latest = connection.execute(
            select(job_runs.c.status, job_runs.c.created_at)
            .where(job_runs.c.job_name == job_name)
            .order_by(job_runs.c.created_at.desc())
            .limit(1)
        ).first()
    return as_utc(completed), (latest[0], latest[1]) if latest else None


def _pipeline_runs(
    engine: Engine, pipeline_name: str
) -> tuple[datetime | None, tuple[str, Any] | None]:
    if not _has_table(engine, "pipeline_runs", schema=PIPELINE_SCHEMA):
        return None, None
    table = f'"{PIPELINE_SCHEMA}".pipeline_runs'
    with engine.connect() as connection:
        completed = connection.execute(
            text(
                f"SELECT max(finished_at) FROM {table} "
                "WHERE pipeline_name = :name AND status = 'completed'"
            ),
            {"name": pipeline_name},
        ).scalar_one()
        latest = connection.execute(
            text(
                f"SELECT status, started_at FROM {table} "
                "WHERE pipeline_name = :name ORDER BY started_at DESC LIMIT 1"
            ),
            {"name": pipeline_name},
        ).first()
    return as_utc(completed), (latest[0], latest[1]) if latest else None


# --- Slots: a department owner decides on an RFP section ------------------------------------


def _rfp_approvals(engine: Engine, rule: RfpApprovalRule) -> list[Watch]:
    if not _has_table(engine, str(RfpDepartmentSection.__tablename__)):
        return []
    query = (
        select(RfpDepartmentSection, RfpTicket, RfpMetadata)
        .join(RfpTicket, col(RfpTicket.id) == col(RfpDepartmentSection.ticket_id))
        .outerjoin(RfpMetadata, col(RfpMetadata.ticket_id) == col(RfpTicket.id))
        .where(
            col(RfpTicket.status) == TicketStatus.WAITING_FOR_APPROVAL.value,
            col(RfpDepartmentSection.approval_status) == "pending",
            col(RfpDepartmentSection.ready_at).is_not(None),
        )
    )
    if rule.departments:
        query = query.where(col(RfpDepartmentSection.department_id).in_(rule.departments))
    with Session(engine) as session:
        rows = session.execute(query).all()
    watches = []
    for section, ticket, metadata in rows:
        department = DEPARTMENT_BY_ID[section.department_id]
        client = metadata.client_name if metadata and metadata.client_name else None
        ready_at = as_utc(section.ready_at)
        watches.append(
            Watch(
                subject_key=f"{ticket.id}:{section.department_id}",
                subject={
                    "ticket_id": str(ticket.id),
                    "rfp_id": str(ticket.rfp_id),
                    "department_id": section.department_id,
                    "owner": department.owner,
                    "client_name": client,
                },
                label=(
                    f"{client or ticket.original_filename}: {department.owner} "
                    f"({department.name}) has not decided on the section"
                ),
                waiting_since=ready_at,
                last_activity_at=ready_at,
                details={
                    "original_filename": ticket.original_filename,
                    "review_rounds": section.review_rounds,
                },
            )
        )
    return watches


# --- Branches: open incidents get picked up -------------------------------------------------


def _incidents(rule: IncidentsRule, *, now: datetime) -> list[Watch]:
    categories = {category.value for category in rule.categories or []}
    branches = {branch.value for branch in rule.branches or []}
    open_by_branch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in incidents_db.get_table().all():
        if incident.get("status") != IncidentStatus.OPEN.value:
            continue
        if categories and incident.get("category") not in categories:
            continue
        if branches and incident.get("branch") not in branches:
            continue
        open_by_branch[str(incident.get("branch"))].append(incident)

    watches = []
    for branch, incidents in sorted(open_by_branch.items()):
        # Sorted by time only: seeded incidents share timestamps, and rows do not compare.
        dated = sorted(
            ((datetime.fromisoformat(str(item["created_at"])), item) for item in incidents),
            key=lambda pair: pair[0],
        )
        oldest_naive, oldest = dated[0]
        oldest_at = as_utc(oldest_naive)
        assert oldest_at is not None
        overdue = sum(
            1
            for created_at, _ in dated
            if active_minutes_between(as_utc(created_at) or now, now, rule.operating_hours)
            > rule.expected_within_minutes
        )
        try:
            branch_label = BRANCH_LABELS["en"][IncidentBranch(branch)]
        except ValueError:
            branch_label = branch
        noun = "incident" if overdue == 1 else "incidents"
        watches.append(
            Watch(
                subject_key=branch,
                subject={"branch": branch},
                label=f"{branch_label}: {overdue} {noun} still open, not picked up",
                waiting_since=oldest_at,
                last_activity_at=None,
                details={
                    "open_incidents": len(incidents),
                    "overdue_incidents": overdue,
                    "oldest_incident_id": oldest.get("id"),
                    "oldest_reference": oldest.get("source_incident_id"),
                    "oldest_created_at": oldest_at.isoformat(),
                },
            )
        )
    return watches
