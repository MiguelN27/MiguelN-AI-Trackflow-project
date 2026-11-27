"""The operational alert, as the live stream and the REST routes carry it.

One shape for the three live events (`operational_inactivity_detected`,
`operational_activity_resumed`, `operational_alert_acknowledged`) and for
`GET /operations/alerts`, so the backoffice upserts an alert by `alert_id`
whichever way it arrived. Mirrored in `uis/backoffice/lib/operations-live.ts`.
"""

from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

INACTIVITY_DETECTED: Final = "operational_inactivity_detected"
ACTIVITY_RESUMED: Final = "operational_activity_resumed"
ALERT_ACKNOWLEDGED: Final = "operational_alert_acknowledged"

Source = Literal["warehouse_orders", "scheduled_job", "rfp_approval", "incidents"]
Severity = Literal["warning", "critical"]
AlertStatus = Literal["open", "resolved"]
# `activity_resumed`: the activity came back (an order, a completed run, incidents picked up);
# `slot_closed`: an RFP section is no longer waiting (decided, or withdrawn with its proposal);
# `rule_disabled` / `rule_removed`: the rules file no longer watches it.
ResolvedReason = Literal["activity_resumed", "slot_closed", "rule_disabled", "rule_removed"]


class OperationalAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    rule_id: str
    source: Source
    # What was expected: `outbound_order_created`, `nightly_export`, `rfp_section_approval`,
    # `incident_triage`.
    activity: str
    # Who or where, in the existing vocabularies: {warehouse, client_id}, {job_name},
    # {ticket_id, rfp_id, department_id, owner, client_name}, {branch}.
    subject: dict[str, str | None]
    label: str
    severity: Severity
    status: AlertStatus
    expected_within_minutes: int = Field(gt=0)
    # When the expected period started counting: the last activity, or when the slot opened.
    waiting_since: datetime | None
    # The last time the expected activity happened; null when it never did.
    last_activity_at: datetime | None
    detected_at: datetime
    # Active minutes past the expected period when detected; null when it never happened.
    overdue_minutes: int | None
    details: dict[str, Any] = Field(default_factory=dict)
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    resolved_reason: ResolvedReason | None = None


class OperationalAlertList(BaseModel):
    alerts: list[OperationalAlert]
