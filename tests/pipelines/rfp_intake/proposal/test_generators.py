"""The generator agents: what they are given, and what their guard lets through."""

import pytest

from data.pipelines.rfp_intake import handoff
from data.pipelines.rfp_intake.llm import InvalidModelReply
from data.pipelines.rfp_intake.proposal import sections
from data.pipelines.rfp_intake.proposal.agents import generators
from tests.pipelines.rfp_intake.proposal import support
from tests.pipelines.rfp_intake.support import sample_markdown


@pytest.fixture
def luna(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    return handoff.load_handoff(engine, ticket_id)


@pytest.fixture
def lastmile(luna):
    return support.assignment_for(luna, "lastmile")


def test_the_lastmile_generator_drafts_from_the_handoff_rate_card_and_knowledge_base(
    model, lastmile
):
    fake = model(support.lastmile_draft("USD", "US", volume=5000).model_dump())
    passages = support.kb_for(lastmile)

    reply = generators.generator_for("lastmile")(lastmile, passages, None)

    request = fake.requests[-1]
    assert request["tools"][0]["function"]["name"] == "write_lastmile_section"
    assert request["tool_choice"] == "required"
    assert "Carlos Vega" in fake.system
    assert "[returns-min-48h] No proposal may promise returns processing" in fake.system
    assert "THIS section carries the proposal's SLA" in fake.system
    assert "<<untrusted kind=rfp_analysis>>" in fake.user
    assert "we just crossed 5,000 orders a month" in fake.user  # Part 1's key aspect
    assert "6.90 USD" in fake.user and " EUR" not in fake.user
    assert "UPS: full national coverage" in fake.user  # the knowledge base
    assert reply.result["guard_notes"] == []
    assert reply.result["draft"]["on_time_sla_pct"] == 90


def test_the_generator_never_reads_the_pdf_or_its_markdown(model, lastmile):
    fake = model(support.lastmile_draft("USD", "US", volume=5000).model_dump())

    generators.generator_for("lastmile")(lastmile, [], None)

    document = sample_markdown("informal")
    paragraphs = [p.strip() for p in document.split("\n\n") if len(p.strip()) > 80]
    assert paragraphs and not any(paragraph in fake.user for paragraph in paragraphs)


def test_a_retry_carries_the_previous_draft_and_the_evaluators_feedback(model, lastmile):
    fake = model(support.lastmile_draft("USD", "US", volume=5000).model_dump())
    previous = {
        "iteration": 1,
        "draft": {"summary": "An earlier draft."},
        "feedback": "1. [no-carrier-rates] Remove the UPS rate.",
    }

    generators.generator_for("lastmile")(lastmile, [], previous)

    assert "YOUR PREVIOUS DRAFT (attempt 1)" in fake.user
    assert "An earlier draft." in fake.user
    assert "FEEDBACK TO FIX" in fake.user and "Remove the UPS rate." in fake.user


def test_the_guard_drops_what_is_invented_or_off_the_rate_card(lastmile):
    draft = support.lastmile_draft("USD", "US", volume=5000)
    draft.summary += " We expect 12,000 orders a month by spring."
    draft.price_lines[0].amount = 5.00  # not the card's 6.90
    draft.price_lines.append(
        sections.PriceLine(item_id="fuel_surcharge", amount=1.0, currency="USD")
    )
    draft.discount_tiers[1].discount_pct = 7
    draft.on_time_sla_pct = 95
    draft.assumed_monthly_volume = 7000
    draft.carriers_by_destination[0].carriers.append("RapidShip")

    result = generators.check(lastmile, support.kb_for(lastmile), draft)

    guarded, notes = result["draft"], result["guard_notes"]
    assert "12,000" not in guarded["summary"]
    assert {line["item_id"] for line in guarded["price_lines"]} == {
        "shipment_standard_5kg",
        "shipment_express",
        "shipment_international",
    }
    assert guarded["discount_tiers"] == []
    assert guarded["on_time_sla_pct"] is None
    assert guarded["assumed_monthly_volume"] is None
    assert guarded["carriers_by_destination"][0]["carriers"] == ["UPS", "FedEx"]
    assert len(notes) == 7
    assert any("12000 is not in the RFP" in note for note in notes)


def test_the_guard_leaves_a_wrong_currency_for_compliance_to_fail(lastmile):
    draft = support.lastmile_draft("EUR", "US", volume=5000)  # EUR card prices, US client

    guarded = generators.check(lastmile, [], draft)["draft"]

    assert {line["currency"] for line in guarded["price_lines"]} == {"EUR"}


def test_only_the_sla_section_keeps_an_sla_figure(luna):
    warehouse = support.assignment_for(luna, "warehouse")
    draft = support.warehouse_draft("USD", "US", sla=True)

    assert generators.check(warehouse, [], draft)["draft"]["on_time_sla_pct"] is None


def test_a_reply_that_breaks_the_schema_is_an_invalid_reply(model, lastmile):
    model('{"price_lines": "not a list"')

    with pytest.raises(InvalidModelReply):
        generators.generator_for("lastmile")(lastmile, [], None)


def test_a_generator_refuses_another_departments_task(lastmile):
    with pytest.raises(ValueError, match="warehouse generator got a lastmile task"):
        generators.generator_for("warehouse")(lastmile, [], None)
