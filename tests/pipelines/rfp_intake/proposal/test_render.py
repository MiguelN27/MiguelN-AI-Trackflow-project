"""The rendered section: the same headings for every department, the SLA where it belongs."""

import pytest

from data.pipelines.rfp_intake import handoff
from data.pipelines.rfp_intake.proposal import sections
from data.pipelines.rfp_intake.proposal.agents import generators
from data.pipelines.rfp_intake.proposal.render import prose_for_readability, render_section
from tests.pipelines.rfp_intake.proposal import support


@pytest.fixture
def modaviva(engine, tmp_path):
    return handoff.load_handoff(engine, support.intake_complete(engine, tmp_path, "formal"))


@pytest.fixture
def luna(engine, tmp_path):
    return handoff.load_handoff(engine, support.intake_complete(engine, tmp_path, "informal"))


def rendered(intake, dept, draft_model):
    assignment = support.assignment_for(intake, dept)
    draft = generators.check(assignment, support.kb_for(assignment), draft_model)["draft"]
    return assignment, draft, render_section(assignment, draft)


def test_every_section_has_the_fixed_headings_in_order(luna):
    _, _, markdown = rendered(luna, "lastmile", support.lastmile_draft("USD", "US", volume=5000))

    headings = [line for line in markdown.splitlines() if line.startswith("#")]
    assert headings == [
        "## Last Mile and Carrier Management: Luna Cosmetics",
        sections.SCOPE_HEADING,
        sections.PRICING_HEADING,
        sections.DISCOUNT_TABLE_HEADING,
        sections.COMMITMENTS_HEADING,
        sections.CARRIERS_HEADING,
        sections.ASSUMPTIONS_HEADING,
        sections.PENDING_HEADING,
    ]
    assert "For approval by Carlos Vega. Prices in USD." in markdown
    assert "| Standard delivery, parcel up to 2 kg (3 to 5 business days) | 6.90 USD |" in markdown
    assert "| 25,000 or more shipments a month | 12% |" in markdown


def test_the_sla_and_its_caveats_are_stated_in_the_designated_section_only(modaviva):
    _, _, warehouse = rendered(
        modaviva, "warehouse", support.warehouse_draft("EUR", "ES", sla=True)
    )
    _, _, reverse = rendered(modaviva, "reverse", support.reverse_draft("EUR", sla=True))

    assert "TrackFlow commits to 90% on-time deliveries per month, measured by country" in warehouse
    # ModaViva ships with its own carrier.
    assert "for the deliveries TrackFlow manages" in warehouse
    assert "5% compensation" in warehouse and "up to 40%" in warehouse
    assert "on-time" not in reverse


def test_the_warehouse_section_states_who_may_negotiate_storage(modaviva):
    _, _, markdown = rendered(modaviva, "warehouse", support.warehouse_draft("EUR", "ES", sla=True))

    assert "negotiated only by Miguel Torres" in markdown
    assert "| Storage, per cubic meter occupied | 16.00 EUR | per m³ per month |" in markdown
    assert "except storage, per cubic meter occupied and sku setup (one-off)" in markdown
    assert "Committed capacity: 5,000 pallet positions and up to 45,000 orders a month." in markdown


def test_pending_information_keeps_the_handoffs_open_questions(modaviva):
    assignment, _, markdown = rendered(
        modaviva, "warehouse", support.warehouse_draft("EUR", "ES", sla=True)
    )

    pending = markdown.split(sections.PENDING_HEADING)[1]
    for question in assignment["open_questions"]:
        assert question.rstrip(".?") in pending


def test_readability_reads_the_prose_without_tables_or_headings(luna):
    _, draft, _ = rendered(luna, "warehouse", support.warehouse_draft("USD", "US", sla=False))

    prose = prose_for_readability(draft)

    assert "|" not in prose and "#" not in prose
    assert prose.startswith("TrackFlow will store and ship your products")
