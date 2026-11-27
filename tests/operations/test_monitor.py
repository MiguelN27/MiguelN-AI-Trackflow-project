"""The monitor: an alert opens once when expected activity is overdue, and resolves when it returns.

Every scenario runs `monitor.evaluate` on SQLite with a fixed clock, then checks
both the alert rows and the live events written to the outbox: exactly one
`operational_inactivity_detected` per silence, one `operational_activity_resumed`
when it ends, each payload valid against `OperationalAlert`.
"""

from datetime import timedelta

import pytest

from services.operations import monitor
from services.operations.schemas import OperationalAlert
from services.operations.service import list_alerts
from tests.operations.support import (
    announcements,
    approval_slot,
    at,
    decide,
    incident,
    job_run,
    ops_engine,
    order,
    pick_up,
    product,
    reopen,
    rules,
)

ZARAGOZA_OUTBOUND = {
    "rule_id": "zaragoza-outbound",
    "source": "warehouse_orders",
    "warehouse": "Zaragoza",
    "activity": "outbound_order_created",
    "expected_within_minutes": 120,
    "operating_hours": {
        "timezone": "Europe/Madrid",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "08:00",
        "end": "20:00",
    },
}
NIGHTLY = {
    "rule_id": "nightly-export",
    "source": "scheduled_job",
    "job_name": "nightly_export",
    "expected_within_minutes": 26 * 60,
    "severity": "critical",
}
RFP_SLA = {"rule_id": "rfp-approval-sla", "source": "rfp_approval", "expected_within_minutes": 1440}
INCIDENTS = {
    "rule_id": "incidents-sla-critical",
    "source": "incidents",
    "categories": ["lost_parcel", "carrier_issue"],
    "expected_within_minutes": 240,
    "severity": "critical",
}


@pytest.fixture
def engine(tmp_path, db_path):
    engine = ops_engine(tmp_path / "ops.sqlite")
    yield engine
    engine.dispose()


def events(engine):
    return [(row["event"], row["payload"]) for row in announcements(engine)]


def open_alerts(engine, now):
    return list_alerts(engine, status="open", since_hours=24, now=now)


# --- Locations: warehouse orders -----------------------------------------------------------------


def test_a_warehouse_without_orders_past_its_working_period_gets_one_alert(engine):
    item = product(engine, "Zaragoza")
    order(engine, item, at("2026-11-27 17:00"))  # Friday 18:00 Madrid
    ruleset = rules(ZARAGOZA_OUTBOUND)

    # The weekend does not count: Monday 09:30 Madrid is only 210 working minutes later...
    assert monitor.evaluate(engine, ruleset, now=at("2026-11-28 12:00")) == []
    changes = monitor.evaluate(engine, ruleset, now=at("2026-11-30 08:30"))
    # ...which is past the 120 expected: one alert, announced once.
    assert [change.event for change in changes] == ["operational_inactivity_detected"]
    alert = changes[0].alert
    assert alert.subject == {"warehouse": "Zaragoza", "client_id": None}
    assert alert.activity == "outbound_order_created"
    assert alert.label == "No outbound order registered at Zaragoza"
    assert alert.overdue_minutes == 90
    assert alert.last_activity_at == at("2026-11-27 17:00")

    # Evaluating again, any number of times, never announces it twice.
    for minutes in (1, 30, 120):
        assert (
            monitor.evaluate(
                engine, ruleset, now=at("2026-11-30 08:30") + timedelta(minutes=minutes)
            )
            == []
        )
    assert len(open_alerts(engine, at("2026-11-30 12:00"))) == 1
    assert [event for event, _ in events(engine)] == ["operational_inactivity_detected"]


def test_the_next_order_resolves_the_alert_and_says_so(engine):
    item = product(engine, "Zaragoza")
    ruleset = rules(ZARAGOZA_OUTBOUND)
    [opened] = monitor.evaluate(engine, ruleset, now=at("2026-11-30 10:00"))
    # Never any order: the alert says so instead of inventing a duration.
    assert opened.alert.last_activity_at is None and opened.alert.overdue_minutes is None

    order(engine, item, at("2026-11-30 10:20"))
    [resolved] = monitor.evaluate(engine, ruleset, now=at("2026-11-30 10:21"))

    assert resolved.event == "operational_activity_resumed"
    assert resolved.alert.alert_id == opened.alert.alert_id
    assert resolved.alert.status == "resolved"
    assert resolved.alert.resolved_reason == "activity_resumed"
    assert open_alerts(engine, at("2026-11-30 10:22")) == []


def test_another_warehouse_or_client_is_a_different_subject(engine):
    order(engine, product(engine, "Monterrey"), at("2026-11-30 10:00"))
    order(engine, product(engine, "Zaragoza", client_id="nexa-electronics"), at("2026-11-30 10:00"))
    ruleset = rules(
        ZARAGOZA_OUTBOUND,
        {**ZARAGOZA_OUTBOUND, "rule_id": "zaragoza-stride", "client_id": "stride-athletics"},
    )

    changes = monitor.evaluate(engine, ruleset, now=at("2026-11-30 10:30"))

    # Zaragoza as a whole shipped; Stride Athletics at Zaragoza never did.
    assert [change.alert.subject for change in changes] == [
        {"warehouse": "Zaragoza", "client_id": "stride-athletics"}
    ]


# --- Processes: scheduled jobs --------------------------------------------------------------------


def test_a_job_that_keeps_failing_is_reported_with_its_last_run(engine):
    job_run(engine, "completed", at("2026-11-12 03:40"))
    job_run(engine, "failed", at("2026-11-13 03:31"))
    job_run(engine, "failed", at("2026-11-22 03:31"))
    ruleset = rules(NIGHTLY)

    [change] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 12:00"))

    alert = change.alert
    assert alert.severity == "critical"
    assert alert.subject == {"job_name": "nightly_export"}
    assert alert.label == "The nightly telemetry export has not completed"
    assert alert.last_activity_at == at("2026-11-12 03:40")
    assert alert.details == {
        "last_run_status": "failed",
        "last_run_at": "2026-11-22T03:26:00+00:00",
    }

    job_run(engine, "completed", at("2026-11-26 12:30"))
    [resolved] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 12:31"))
    assert resolved.alert.resolved_reason == "activity_resumed"


def test_a_job_that_never_ran_is_an_alert(engine):
    [change] = monitor.evaluate(engine, rules(NIGHTLY), now=at("2026-11-26 12:00"))

    assert change.alert.last_activity_at is None
    assert change.alert.details == {}


# --- Slots: RFP approvals past their SLA ----------------------------------------------------------


def test_an_owner_who_has_not_decided_past_the_sla_is_named(engine, tmp_path):
    ticket_id, section_id = approval_slot(engine, tmp_path, at("2026-11-25 09:00"))
    ruleset = rules(RFP_SLA)

    assert monitor.evaluate(engine, ruleset, now=at("2026-11-26 08:59")) == []
    [change] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 09:30"))

    alert = change.alert
    assert alert.activity == "rfp_section_approval"
    assert alert.subject["ticket_id"] == str(ticket_id)
    assert set(alert.subject) == {"ticket_id", "rfp_id", "department_id", "owner", "client_name"}
    assert alert.subject["department_id"] == "warehouse"
    assert alert.subject["owner"] == "Ana Whitfield"
    assert (
        alert.label
        == "ModaViva: Ana Whitfield (Warehouse Operations) has not decided on the section"
    )
    assert alert.waiting_since == at("2026-11-25 09:00")

    decide(engine, section_id, "approved")
    [resolved] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 10:00"))
    assert resolved.alert.resolved_reason == "slot_closed"


def test_a_revision_restarts_the_clock(engine, tmp_path):
    _, section_id = approval_slot(engine, tmp_path, at("2026-11-25 09:00"))
    ruleset = rules(RFP_SLA)
    monitor.evaluate(engine, ruleset, now=at("2026-11-26 10:00"))

    reopen(engine, section_id, at("2026-11-26 10:30"))  # requested changes, revised, pending again
    [resolved] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 11:00"))

    assert resolved.alert.resolved_reason == "activity_resumed"
    assert monitor.evaluate(engine, ruleset, now=at("2026-11-27 10:00")) == []
    [reopened] = monitor.evaluate(engine, ruleset, now=at("2026-11-27 11:00"))
    assert reopened.alert.alert_id != resolved.alert.alert_id


# --- Branches: untouched incidents ----------------------------------------------------------------


def test_untouched_incidents_alert_once_per_branch_not_once_per_incident(engine):
    for hours in (30, 20, 6):
        incident(at("2026-11-26 12:00") - timedelta(hours=hours), branch="central")
    incident(at("2026-11-26 11:00"), branch="central")  # recent: not overdue yet
    incident(at("2026-11-26 06:00"), branch="zaragoza_warehouse", reference="TRF-000482")
    incident(at("2026-11-20 06:00"), branch="la_warehouse", category="other")  # another rule's
    ruleset = rules(INCIDENTS)

    changes = monitor.evaluate(engine, ruleset, now=at("2026-11-26 12:00"))

    by_branch = {change.alert.subject["branch"]: change.alert for change in changes}
    assert set(by_branch) == {"central", "zaragoza_warehouse"}
    assert by_branch["central"].label == "Central: 3 incidents still open, not picked up"
    assert by_branch["central"].details["open_incidents"] == 4
    assert by_branch["central"].details["overdue_incidents"] == 3
    assert by_branch["zaragoza_warehouse"].details["oldest_reference"] == "TRF-000482"
    assert by_branch["zaragoza_warehouse"].label == (
        "Zaragoza — Warehouse: 1 incident still open, not picked up"
    )


def test_incidents_registered_at_the_same_instant_are_counted(engine):
    # The helpdesk seed gives many incidents the same `created_at` (found on the real data).
    for _ in range(3):
        incident(at("2024-01-01 00:00"), branch="central", reference=None)

    [change] = monitor.evaluate(engine, rules(INCIDENTS), now=at("2026-11-26 12:00"))

    assert change.alert.details["overdue_incidents"] == 3


def test_picking_the_incidents_up_resolves_the_branch(engine):
    zaragoza = incident(at("2026-11-26 06:00"), branch="zaragoza_warehouse")
    ruleset = rules(INCIDENTS)
    monitor.evaluate(engine, ruleset, now=at("2026-11-26 12:00"))

    pick_up(zaragoza)
    [resolved] = monitor.evaluate(engine, ruleset, now=at("2026-11-26 12:05"))

    assert resolved.event == "operational_activity_resumed"
    assert resolved.alert.resolved_reason == "activity_resumed"


# --- The rules change, a source fails -------------------------------------------------------------


def test_disabling_or_removing_a_rule_resolves_its_alerts(engine):
    monitor.evaluate(engine, rules(NIGHTLY, ZARAGOZA_OUTBOUND), now=at("2026-11-30 12:00"))

    changes = monitor.evaluate(
        engine, rules({**NIGHTLY, "enabled": False}), now=at("2026-11-30 12:01")
    )

    reasons = {change.alert.rule_id: change.alert.resolved_reason for change in changes}
    assert reasons == {"nightly-export": "rule_disabled", "zaragoza-outbound": "rule_removed"}


def test_a_source_that_fails_keeps_its_alerts_as_they_are(engine, monkeypatch):
    ruleset = rules(NIGHTLY, ZARAGOZA_OUTBOUND)
    monitor.evaluate(engine, ruleset, now=at("2026-11-30 12:00"))
    real_observe = monitor.observe

    def failing(engine_, rule, *, now):
        if rule.rule_id == "nightly-export":
            raise TypeError("any failure, not only a database one")
        return real_observe(engine_, rule, now=now)

    monkeypatch.setattr(monitor, "observe", failing)
    job_run(engine, "completed", at("2026-11-30 12:30"))  # would resolve it, if it could be read

    assert monitor.evaluate(engine, ruleset, now=at("2026-11-30 12:31")) == []
    assert {alert.rule_id for alert in open_alerts(engine, at("2026-11-30 12:32"))} == {
        "nightly-export",
        "zaragoza-outbound",
    }


# --- The live events ------------------------------------------------------------------------------


def test_every_change_is_one_live_event_whose_data_is_the_alert(engine):
    item = product(engine, "Zaragoza")
    ruleset = rules(ZARAGOZA_OUTBOUND)
    monitor.evaluate(engine, ruleset, now=at("2026-11-30 10:00"))
    order(engine, item, at("2026-11-30 10:20"))
    monitor.evaluate(engine, ruleset, now=at("2026-11-30 10:21"))

    rows = announcements(engine)

    assert [row["event"] for row in rows] == [
        "operational_inactivity_detected",
        "operational_activity_resumed",
    ]
    alerts = [OperationalAlert.model_validate(row["payload"]) for row in rows]
    assert alerts[0].alert_id == alerts[1].alert_id
    assert {row["subject"] for row in rows} == {str(alerts[0].alert_id)}
    assert [alert.status for alert in alerts] == ["open", "resolved"]


def test_preview_reports_without_writing(engine):
    product(engine, "Zaragoza")

    intents = monitor.preview(engine, rules(ZARAGOZA_OUTBOUND), now=at("2026-11-30 10:00"))

    assert [type(intent).__name__ for intent in intents] == ["OpenAlert"]
    assert announcements(engine) == []
    assert open_alerts(engine, at("2026-11-30 10:00")) == []
