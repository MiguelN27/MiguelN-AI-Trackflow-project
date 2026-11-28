"""The rules file: the shipped defaults are valid; a rule it cannot honour stops start-up."""

import json

import pytest

from services.core.config import ConfigurationError
from services.operations import lifecycle
from services.operations.config import DEFAULT_RULES_PATH, get_operations_settings
from services.operations.rules import RulesError, activity_of, get_rules, load_rules

WAREHOUSE_RULE = {
    "rule_id": "zaragoza-outbound",
    "source": "warehouse_orders",
    "warehouse": "Zaragoza",
    "activity": "outbound_order_created",
    "expected_within_minutes": 120,
    "operating_hours": {"timezone": "Europe/Madrid", "start": "08:00", "end": "20:00"},
}


def write(tmp_path, rules):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")
    return path


def test_the_shipped_rules_cover_the_four_kinds_of_expected_activity():
    rules = load_rules(DEFAULT_RULES_PATH)

    assert {rule.source for rule in rules.rules} == {
        "warehouse_orders",
        "scheduled_job",
        "rfp_approval",
        "incidents",
    }
    by_id = rules.by_id()
    assert activity_of(by_id["zaragoza-outbound"]) == "outbound_order_created"
    assert activity_of(by_id["nightly-export"]) == "nightly_export"
    assert activity_of(by_id["rfp-approval-sla"]) == "rfp_section_approval"
    assert activity_of(by_id["incidents-sla-critical"]) == "incident_triage"
    # Each warehouse counts only its own working hours, in its own timezone.
    monterrey, zaragoza = by_id["monterrey-outbound"], by_id["zaragoza-outbound"]
    assert monterrey.operating_hours is not None and zaragoza.operating_hours is not None
    assert monterrey.operating_hours.timezone == "America/Monterrey"
    assert zaragoza.operating_hours.timezone == "Europe/Madrid"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source": "telemetry"}, "source"),
        ({"warehouse": "Los Angeles"}, "warehouse"),
        ({"activity": "order_shipped"}, "activity"),
        ({"expected_within_minutes": 0}, "expected_within_minutes"),
        (
            {"operating_hours": {"timezone": "Mars/Olympus", "start": "08:00", "end": "20:00"}},
            "timezone",
        ),
        (
            {"operating_hours": {"timezone": "Europe/Madrid", "start": "20:00", "end": "08:00"}},
            "start must be before end",
        ),
        ({"rule_id": "Has Spaces"}, "rule_id"),
        ({"windows": 3}, "windows"),
    ],
)
def test_a_rule_the_monitor_cannot_honour_is_refused_with_the_reason(tmp_path, change, message):
    path = write(tmp_path, [{**WAREHOUSE_RULE, **change}])

    with pytest.raises(RulesError) as refused:
        load_rules(path)

    assert str(path) in str(refused.value)
    assert message in str(refused.value)


def test_duplicate_rule_ids_and_unreadable_files_are_refused(tmp_path):
    with pytest.raises(RulesError, match="repeated rule_id: zaragoza-outbound"):
        load_rules(write(tmp_path, [WAREHOUSE_RULE, WAREHOUSE_RULE]))
    with pytest.raises(RulesError, match="cannot be read"):
        load_rules(tmp_path / "missing.json")


def test_an_invalid_rules_file_stops_the_api_at_start_up(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_ACTIVITY_RULES_PATH", str(write(tmp_path, [{"rule_id": "x"}])))
    get_operations_settings.cache_clear()
    get_rules.cache_clear()

    with pytest.raises(ConfigurationError, match="Operational activity rules"):
        lifecycle.initialize_rules()
