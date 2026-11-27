"""Evaluate the rules: open an alert when expected activity is overdue, resolve it when it returns.

Three steps, so that the decision itself is a pure function:
1. **observe** every enabled rule's subjects (`sources.observe`), with no lock and
   no transaction held. A source that fails is skipped for this round: its
   alerts stay as they are, never resolved by mistake;
2. **plan** (`plan`, pure): compare what is overdue now with the open alerts.
   - overdue and not open → open (`operational_inactivity_detected`);
   - open and no longer overdue → resolve (`operational_activity_resumed`), as
     `activity_resumed`; a subject that disappeared resolves as `slot_closed`
     for an RFP section (decided or withdrawn) and `activity_resumed` otherwise
     (a branch whose incidents were all picked up);
   - open for a rule now disabled or removed → resolve as `rule_disabled` / `rule_removed`;
3. **apply** in one transaction under `pg_advisory_xact_lock(hashtext('operational_alerts'))`:
   the open alerts are read again inside the lock, then each change writes its
   alert row and its live event (`data.notifications.outbox.append`) together.
   Two API workers, or the API and the CLI, evaluating at once never open the
   same alert twice; the partial unique index is the last line of defence.

The caller wakes the live stream (`notifications.wake`) when anything changed.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col

from data.notifications import outbox
from services.operations import schemas
from services.operations.models import OperationalAlertRow
from services.operations.rules import RfpApprovalRule, Rule, RuleSet, activity_of
from services.operations.schedule import active_minutes_between
from services.operations.sources import Watch, as_utc, observe

logger = logging.getLogger("api.operations.monitor")

LOCK_KEY = "operational_alerts"


@dataclass(frozen=True)
class Assessment:
    overdue: bool
    # Active minutes past the expected period; None when the activity never happened.
    overdue_minutes: int | None


def assess(rule: Rule, watch: Watch, now: datetime) -> Assessment:
    if watch.waiting_since is None:
        return Assessment(overdue=True, overdue_minutes=None)
    active = active_minutes_between(watch.waiting_since, now, rule.operating_hours)
    overdue = active > rule.expected_within_minutes
    return Assessment(overdue, int(active - rule.expected_within_minutes) if overdue else 0)


@dataclass(frozen=True)
class OpenAlert:
    rule: Rule
    watch: Watch
    assessment: Assessment


@dataclass(frozen=True)
class ResolveAlert:
    alert_id: UUID
    reason: schemas.ResolvedReason


Intent = OpenAlert | ResolveAlert


@dataclass(frozen=True)
class OpenKey:
    alert_id: UUID
    rule_id: str
    subject_key: str


def plan(
    rules: RuleSet,
    observed: dict[str, list[Watch]],
    open_alerts: list[OpenKey],
    now: datetime,
) -> list[Intent]:
    """What to open and resolve, from what was observed (`observed`, by rule) and what is open.

    A rule missing from `observed` (its source failed this round) is left alone.
    """
    by_id = rules.by_id()
    remaining = {(alert.rule_id, alert.subject_key): alert for alert in open_alerts}
    intents: list[Intent] = []
    for rule in rules.rules:
        if not rule.enabled or rule.rule_id not in observed:
            continue
        for watch in observed[rule.rule_id]:
            assessment = assess(rule, watch, now)
            existing = remaining.pop((rule.rule_id, watch.subject_key), None)
            if assessment.overdue and existing is None:
                intents.append(OpenAlert(rule, watch, assessment))
            elif not assessment.overdue and existing is not None:
                intents.append(ResolveAlert(existing.alert_id, "activity_resumed"))
        gone: schemas.ResolvedReason = (
            "slot_closed" if isinstance(rule, RfpApprovalRule) else "activity_resumed"
        )
        for key in [key for key in remaining if key[0] == rule.rule_id]:
            intents.append(ResolveAlert(remaining.pop(key).alert_id, gone))
    for (rule_id, _), alert in remaining.items():
        configured = by_id.get(rule_id)
        if configured is None:
            intents.append(ResolveAlert(alert.alert_id, "rule_removed"))
        elif not configured.enabled:
            intents.append(ResolveAlert(alert.alert_id, "rule_disabled"))
        # else: its source failed this round, keep it open
    return intents


@dataclass(frozen=True)
class AlertChange:
    event: str
    alert: schemas.OperationalAlert


def to_schema(row: OperationalAlertRow) -> schemas.OperationalAlert:
    return schemas.OperationalAlert.model_validate(
        {
            "alert_id": row.alert_id,
            "rule_id": row.rule_id,
            "source": row.source,
            "activity": row.activity,
            "subject": row.subject,
            "label": row.label,
            "severity": row.severity,
            "status": row.status,
            "expected_within_minutes": row.expected_within_minutes,
            "waiting_since": as_utc(row.waiting_since),
            "last_activity_at": as_utc(row.last_activity_at),
            "detected_at": as_utc(row.opened_at),
            "overdue_minutes": row.overdue_minutes,
            "details": row.details,
            "acknowledged_at": as_utc(row.acknowledged_at),
            "acknowledged_by": row.acknowledged_by,
            "resolved_at": as_utc(row.resolved_at),
            "resolved_reason": row.resolved_reason,
        }
    )


def announce(session: Session, event: str, row: OperationalAlertRow) -> AlertChange:
    """Write the alert's live event in the caller's transaction (once per alert and event)."""
    alert = to_schema(row)
    outbox.append(
        session, event=event, subject=str(row.alert_id), payload=alert.model_dump(mode="json")
    )
    return AlertChange(event, alert)


def lock(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": LOCK_KEY})


def _observe_all(engine: Engine, rules: RuleSet, now: datetime) -> dict[str, list[Watch]]:
    observed: dict[str, list[Watch]] = {}
    for rule in rules.rules:
        if not rule.enabled:
            continue
        try:
            observed[rule.rule_id] = observe(engine, rule, now=now)
        except Exception as error:  # noqa: BLE001 - one broken source never stops the others
            logger.error("rule %s skipped this round: %s", rule.rule_id, type(error).__name__)
    return observed


def _open_keys(session: Session) -> list[OpenKey]:
    rows = session.execute(
        select(OperationalAlertRow).where(col(OperationalAlertRow.status) == "open")
    ).scalars()
    return [OpenKey(row.alert_id, row.rule_id, row.subject_key) for row in rows]


def evaluate(engine: Engine, rules: RuleSet, *, now: datetime | None = None) -> list[AlertChange]:
    """One evaluation round; returns what was announced (empty when nothing changed)."""
    now = now or datetime.now(timezone.utc)
    observed = _observe_all(engine, rules, now)
    changes: list[AlertChange] = []
    with Session(engine, expire_on_commit=False) as session:
        lock(session)
        for intent in plan(rules, observed, _open_keys(session), now):
            if isinstance(intent, OpenAlert):
                changes.append(_open(session, intent, now))
            else:
                change = _resolve(session, intent, now)
                if change is not None:
                    changes.append(change)
        session.commit()
    for change in changes:
        logger.info("%s %s (%s)", change.event, change.alert.rule_id, change.alert.subject or "-")
    return changes


def preview(engine: Engine, rules: RuleSet, *, now: datetime | None = None) -> list[Intent]:
    """What `evaluate` would do, without writing (CLI `--dry-run`). No alerts table: none open."""
    now = now or datetime.now(timezone.utc)
    observed = _observe_all(engine, rules, now)
    open_alerts: list[OpenKey] = []
    try:
        if inspect(engine).has_table(str(OperationalAlertRow.__tablename__)):
            with Session(engine) as session:
                open_alerts = _open_keys(session)
    except SQLAlchemyError as error:
        # Still report what the reachable sources show; without the alerts, as if none were open.
        logger.error(
            "open alerts unreadable, previewing as if none were open: %s", type(error).__name__
        )
    return plan(rules, observed, open_alerts, now)


def _open(session: Session, intent: OpenAlert, now: datetime) -> AlertChange:
    rule, watch = intent.rule, intent.watch
    row = OperationalAlertRow(
        alert_id=uuid4(),
        rule_id=rule.rule_id,
        source=rule.source,
        subject_key=watch.subject_key,
        subject=watch.subject,
        activity=activity_of(rule),
        label=watch.label,
        severity=rule.severity,
        status="open",
        expected_within_minutes=rule.expected_within_minutes,
        waiting_since=watch.waiting_since,
        last_activity_at=watch.last_activity_at,
        overdue_minutes=intent.assessment.overdue_minutes,
        details=watch.details,
        opened_at=now,
    )
    session.add(row)
    session.flush()
    return announce(session, schemas.INACTIVITY_DETECTED, row)


def _resolve(session: Session, intent: ResolveAlert, now: datetime) -> AlertChange | None:
    row = session.get(OperationalAlertRow, intent.alert_id)
    if row is None or row.status != "open":
        return None
    row.status = "resolved"
    row.resolved_at = now
    row.resolved_reason = intent.reason
    session.add(row)
    session.flush()
    return announce(session, schemas.ACTIVITY_RESUMED, row)
