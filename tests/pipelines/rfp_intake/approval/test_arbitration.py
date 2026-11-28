"""Briefing §7: conflict triggers, fixed arbiters, deterministic rules (never an LLM vote)."""

import dataclasses
from datetime import datetime, timezone

import pytest

from data.pipelines.rfp_intake.approval import arbitration
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.proposal import rate_card
from tests.pipelines.rfp_intake.approval import support
from tests.pipelines.rfp_intake.proposal.support import (
    BAD_RETURNS_SENTENCE,
    bad_returns,
    good,
    reverse_draft,
)
from tests.pipelines.rfp_intake.test_briefing import BRIEFING, table_rows

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def test_the_triggers_are_the_briefing_section_7_table_verbatim():
    if not BRIEFING.is_file():
        pytest.skip("the briefing is not in the repository")
    rows = table_rows(BRIEFING.read_text(encoding="utf-8"), "Trigger id")
    parsed = [(row[0].strip("`"), row[1], row[2], row[3]) for row in rows]
    expected = [
        (t.trigger_id, t.fires_when, t.arbiter, t.resolution) for t in arbitration.CONFLICT_TRIGGERS
    ]
    assert parsed == expected


# --- detect / rule_on (pure) --------------------------------------------------------------


def view(dept: str, draft: dict, markdown: str = "", rounds: int = 0) -> arbitration.SectionView:
    return arbitration.SectionView(
        department_id=dept,
        draft=draft,
        markdown=markdown,
        approval_status="pending",
        review_rounds=rounds,
    )


def priced(currency: str) -> dict:
    return {"price_lines": [{"item_id": "x", "amount": 1.0, "currency": currency}]}


def test_volume_vs_capacity_fires_only_above_the_warehouse_capacity():
    warehouse = view(
        "warehouse", {**priced("USD"), "committed_capacity": {"monthly_orders": 60000}}
    )
    under = view("lastmile", {**priced("USD"), "assumed_monthly_volume": 5000})
    over = view("lastmile", {**priced("USD"), "assumed_monthly_volume": 80000})

    assert arbitration.detect([warehouse, under], currency="USD", country="US") == []
    [conflict] = arbitration.detect([warehouse, over], currency="USD", country="US")
    assert conflict.trigger_id == "volume-vs-capacity"
    assert (conflict.departments, conflict.volume_cap) == (["lastmile"], 60000)


def test_returns_sla_breach_is_sofia_ramos_for_her_section_and_miguel_torres_for_others():
    sections = [
        view("reverse", {**priced("EUR"), "returns_turnaround_hours": 24}),
        view("warehouse", priced("EUR"), markdown=BAD_RETURNS_SENTENCE),
    ]
    [conflict] = arbitration.detect(sections, currency="EUR", country="ES")
    assert conflict.trigger_id == "returns-sla-breach"
    assert conflict.departments == ["warehouse", "reverse"]

    rulings = arbitration.rule_on(
        [conflict],
        rounds_used=0,
        max_rounds=2,
        revisions_left={"warehouse": 3, "reverse": 3},
        currency="EUR",
        country="ES",
        now=NOW,
    )
    assert [(r.arbiter, r.departments, r.action) for r in rulings] == [
        ("Sofía Ramos", ["reverse"], "request_changes"),
        ("Miguel Torres", ["warehouse"], "request_changes"),
    ]
    assert all("48 hours" in r.instruction for r in rulings)


def test_currency_mismatch_fires_on_the_wrong_currency_or_two_currencies():
    euro, dollar = view("warehouse", priced("EUR")), view("reverse", priced("USD"))
    [conflict] = arbitration.detect([euro, dollar], currency="EUR", country="ES")
    assert (conflict.trigger_id, conflict.departments) == ("currency-mismatch", ["reverse"])
    assert "quote EUR, USD" in conflict.evidence[0]
    assert arbitration.detect([euro], currency="EUR", country="ES") == []


def test_an_unknown_country_is_rejected_by_miguel_torres_at_once():
    [conflict] = arbitration.detect([view("warehouse", priced("USD"))], currency=None, country=None)
    assert conflict.resolvable is False

    [ruling] = arbitration.rule_on(
        [conflict],
        rounds_used=0,
        max_rounds=2,
        revisions_left={"warehouse": 3},
        currency=None,
        country=None,
        now=NOW,
    )
    assert (ruling.arbiter, ruling.action) == ("Miguel Torres", "reject")


@pytest.mark.parametrize(
    ("rounds_used", "revisions_left", "action"),
    [(0, 3, "request_changes"), (2, 3, "reject"), (0, 0, "reject")],
)
def test_a_conflict_still_there_at_a_limit_is_rejected(rounds_used, revisions_left, action):
    conflict = arbitration.Conflict(
        trigger_id="currency-mismatch", departments=["reverse"], evidence=["reverse: USD"]
    )
    [ruling] = arbitration.rule_on(
        [conflict],
        rounds_used=rounds_used,
        max_rounds=2,
        revisions_left={"reverse": revisions_left},
        currency="EUR",
        country="ES",
        now=NOW,
    )
    assert ruling.action == action
    assert ruling.rule == arbitration.TRIGGER_BY_ID["currency-mismatch"].resolution


# --- In the graph ---------------------------------------------------------------------------


def test_a_returns_breach_is_ruled_by_sofia_ramos_and_fixed_before_anyone_approves(
    engine, tmp_path
):
    approval = support.drafted(
        engine,
        tmp_path,
        "formal",
        drafts={"warehouse": [good("warehouse")], "reverse": [bad_returns, good("reverse")]},
        max_draft_iterations=1,
    )
    assert approval.status() == "needs_human_review"

    approval.start()

    # The first arbitration ran on Part 2's output: reverse was sent back before review.
    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None
    [ruling] = run.rulings
    assert (ruling["trigger_id"], ruling["arbiter"], ruling["departments"]) == (
        "returns-sla-breach",
        "Sofía Ramos",
        ["reverse"],
    )
    assert run.arbitration_rounds == 1
    requests = approval.requests()
    assert requests["reverse"]["review_round"] == 2
    assert "returns-sla-breach" in requests["reverse"]["reason"]
    assert requests["warehouse"]["review_round"] == 1  # untouched
    revision = approval.agents.calls["generate:reverse#2"][0]["assignment"]["revision_request"]
    assert revision["requested_by"] == "Sofía Ramos (owner of Reverse Logistics)"
    assert approval.sections()["reverse"].draft_status == "passed"

    assert approval.approve_all() == "done"
    document = approval_store.get_document(engine, approval.ticket_id)
    assert document is not None
    assert "Arbitration (returns-sla-breach): Sofía Ramos" in document.content
    assert BAD_RETURNS_SENTENCE not in document.content


def test_volume_vs_capacity_caps_the_volume_and_only_last_mile_revises(
    engine, tmp_path, monkeypatch
):
    # Luna asks for 5,000 orders a month; this warehouse can handle 4,000.
    small = dataclasses.replace(rate_card.SITES["US"], monthly_orders=4000)
    monkeypatch.setitem(rate_card.SITES, "US", small)
    approval = support.opened(engine, tmp_path, "informal")

    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None
    [ruling] = run.rulings
    assert (ruling["trigger_id"], ruling["arbiter"], ruling["volume_cap"]) == (
        "volume-vs-capacity",
        "Miguel Torres",
        4000,
    )
    revision = approval.agents.calls["generate:lastmile#2"][0]["assignment"]
    assert revision["monthly_volume"] == 4000
    sections = approval.sections()
    assert sections["lastmile"].draft_data["draft"]["assumed_monthly_volume"] == 4000
    assert sections["lastmile"].review_rounds == 1
    assert sections["warehouse"].review_rounds == 0

    assert approval.approve_all() == "done"
    document = approval_store.get_document(engine, approval.ticket_id)
    assert document is not None
    assert "4,000 orders a month (capped at warehouse capacity by arbitration)" in document.content


def test_a_conflict_after_approval_reopens_only_the_offending_section(engine, tmp_path):
    def dollars(assignment: dict) -> object:
        return reverse_draft("USD")

    approval = support.opened(
        engine,
        tmp_path,
        "formal",
        # Part 2: good. The owner's revision: priced in USD (wrong for Spain). Then good again.
        drafts={
            "warehouse": [good("warehouse")],
            "reverse": [good("reverse"), dollars, good("reverse")],
        },
        max_draft_iterations=1,
    )
    approval.decide("warehouse", "approve")
    approval.decide("reverse", "request_changes", "Show the international returns cost.")
    assert approval.requests()["reverse"]["needs_human_review"] is True

    # Sofía approves the USD draft anyway; the arbitration catches it at convergence.
    assert approval.decide("reverse", "approve") == "waiting_for_approval"

    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None
    assert [(r["trigger_id"], r["arbiter"], r["departments"]) for r in run.rulings] == [
        ("currency-mismatch", "Miguel Torres", ["reverse"])
    ]
    sections = approval.sections()
    assert sections["warehouse"].approval_status == "approved"  # untouched
    assert sections["reverse"].approval_status == "pending"
    assert sections["reverse"].approved_at is None
    assert set(approval.requests()) == {"reverse"}

    assert approval.decide("reverse", "approve") == "done"
    arbiter = [
        d for d in approval_store.decisions_for(engine, approval.ticket_id) if d.source == "arbiter"
    ]
    assert [(d.department_id, d.action, d.trigger_id) for d in arbiter] == [
        ("reverse", "request_changes", "currency-mismatch")
    ]
