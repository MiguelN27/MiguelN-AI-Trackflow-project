"""The compliance evaluator's rules: briefing §5, checked deterministically.

`test_a_draft_promising_returns_in_24_hours_fails_compliance` is the
checklist's compliance-failure case: one fixture, one assertion on fail.
"""

import re

import pytest

from data.pipelines.rfp_intake.proposal import guidelines
from data.pipelines.rfp_intake.proposal.agents import compliance
from data.pipelines.rfp_intake.proposal.render import render_section
from tests.pipelines.rfp_intake.proposal import support
from tests.pipelines.rfp_intake.support import ROOT

BRIEFING = ROOT / "agentic-workflow-context.md"


def briefing_section(number: int) -> str:
    text = BRIEFING.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"briefing §{number} not found"
    return match.group(0)


def bullets(section: str) -> list[str]:
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]


def test_the_rules_are_the_briefing_section_5_word_for_word_and_in_order():
    assert [rule.briefing_text for rule in guidelines.GUIDELINES] == bullets(briefing_section(5))


def test_the_conflict_triggers_exist_in_briefing_section_7():
    section_7 = briefing_section(7)
    for rule in guidelines.GUIDELINES:
        if rule.conflict_trigger:
            assert f"`{rule.conflict_trigger}`" in section_7


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        (["warehouse", "lastmile"], "lastmile"),  # Luna Cosmetics
        (["warehouse", "reverse"], "warehouse"),  # ModaViva: its own carrier
        (["reverse"], "reverse"),
        (["reverse", "lastmile", "warehouse"], "lastmile"),
    ],
)
def test_the_sla_is_stated_in_last_mile_else_the_first_active_section(active, expected):
    assert guidelines.sla_department(active) == expected


def test_the_sla_rule_applies_to_the_designated_section_only():
    assert "on-time-sla-stated" in guidelines.applicable_rule_ids(is_sla_section=True)
    assert "on-time-sla-stated" not in guidelines.applicable_rule_ids(is_sla_section=False)
    assert len(guidelines.applicable_rule_ids(is_sla_section=False)) == 4


# --- The fixtures: a ModaViva reverse section (Spain, EUR, not the SLA section) -----------


@pytest.fixture
def reverse_assignment(engine, tmp_path):
    from data.pipelines.rfp_intake import handoff

    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    return support.assignment_for(handoff.load_handoff(engine, ticket_id), "reverse")


def evaluate(assignment, draft_model):
    from data.pipelines.rfp_intake.proposal.agents import generators

    guarded = generators.check(assignment, support.kb_for(assignment), draft_model)["draft"]
    markdown = render_section(assignment, guarded)
    return compliance.evaluate_rules(assignment, guarded, markdown)


def test_a_draft_promising_returns_in_24_hours_fails_compliance(reverse_assignment):
    result = evaluate(reverse_assignment, support.bad_reverse_draft("EUR"))

    assert result.passed is False


def test_the_failure_names_the_returns_rule_and_quotes_the_promise(reverse_assignment):
    result = evaluate(reverse_assignment, support.bad_reverse_draft("EUR"))

    assert result.rule_ids == ["returns-min-48h"]
    quotes = [violation.quote for violation in result.violations]
    assert support.BAD_RETURNS_SENTENCE in quotes
    assert "returns_turnaround_hours: 24" in quotes
    assert result.as_json()["pass"] is False  # the ticket's field name


def test_the_compliant_reverse_section_passes(reverse_assignment):
    result = evaluate(reverse_assignment, support.reverse_draft("EUR"))

    assert result.passed is True
    assert result.checked_rule_ids == [
        "currency-by-country",
        "returns-min-48h",
        "volume-discount-table",
        "no-carrier-rates",
    ]


def test_prices_in_dollars_for_a_spanish_client_fail_the_currency_rule(reverse_assignment):
    result = evaluate(reverse_assignment, support.reverse_draft("USD"))

    assert "currency-by-country" in result.rule_ids
    assert all(v.retryable for v in result.violations)


def test_a_section_without_its_tier_table_fails(reverse_assignment):
    draft = support.reverse_draft("EUR")
    draft.discount_tiers = []

    assert evaluate(reverse_assignment, draft).rule_ids == ["volume-discount-table"]


def test_the_designated_section_must_state_the_sla(engine, tmp_path):
    from data.pipelines.rfp_intake import handoff

    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    assignment = support.assignment_for(handoff.load_handoff(engine, ticket_id), "warehouse")
    assert assignment["is_sla_section"] is True
    draft = support.warehouse_draft("EUR", "ES", sla=False)

    assert evaluate(assignment, draft).rule_ids == ["on-time-sla-stated"]
    assert evaluate(assignment, support.warehouse_draft("EUR", "ES", sla=True)).passed


def test_an_unknown_country_cannot_be_fixed_by_a_rewrite():
    violations, _ = guidelines.check_section({}, "", currency=None, is_sla_section=False)

    currency = [v for v in violations if v.rule_id == "currency-by-country"]
    assert currency and not currency[0].retryable


@pytest.mark.parametrize(
    ("sentence", "hours"),
    [
        ("Returns are processed within 24 hours of arrival.", 24),
        ("We process returns in 1 business day.", 24),
        ("Returns are inspected the same day they arrive.", 24),
        ("Returns are back in stock within 24-36h.", 24),
        ("Returns are classified within 72 hours.", 72),
        ("Customers have a 30-day return window.", 720),
        ("Returns are processed in 2 to 3 days.", 48),
    ],
)
def test_the_fastest_promised_turnaround_is_read(sentence, hours):
    assert guidelines.promised_hours(sentence) == hours


@pytest.mark.parametrize(
    "sentence",
    [
        "Express delivery takes 24 to 48 hours within the same country.",  # not about returns
        "Standard return window: 30 days from delivery.",
        "Returns are classified within 72 hours of arrival.",
    ],
)
def test_what_is_not_a_fast_returns_promise(sentence):
    violations = guidelines._check_returns({}, sentence)
    assert violations == []


@pytest.mark.parametrize(
    ("statement", "discloses"),
    [
        ("We ship with UPS, FedEx and DHL across the country.", False),
        ("| Within the country | UPS, FedEx |", False),
        ("Our negotiated rate with UPS lets us offer this price.", True),
        ("SEUR charges us 3.10 EUR per parcel.", True),
        ("We get a 20% discount from DHL.", True),
        ("Pick-ups and set-ups cost 2.40 USD.", False),  # not the carrier UPS
        ("Local Zaragoza carriers deliver for 3.50 EUR.", True),
    ],
)
def test_a_carrier_next_to_a_rate_is_a_disclosure(statement, discloses):
    assert guidelines.discloses_carrier_rate(statement) is discloses


def test_statements_are_verbatim_passages_of_the_section():
    markdown = "## Title\n\nOne sentence. Two sentences!\n\n| a | b |\n|---|---|\n- A bullet."
    assert guidelines.statements(markdown) == [
        "One sentence.",
        "Two sentences!",
        "| a | b |",
        "A bullet.",
    ]
