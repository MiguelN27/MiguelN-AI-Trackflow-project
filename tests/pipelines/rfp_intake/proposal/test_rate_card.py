"""The rate card and the section formats: grounded in the knowledge base and briefing §2.1."""

import pytest

from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID, DEPARTMENT_IDS
from data.pipelines.rfp_intake.proposal import guidelines, knowledge, rate_card, sections
from data.pipelines.rfp_intake.text import appears_in


def kb_document(name: str) -> str:
    return (knowledge.KB_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "entry",
    [*rate_card.PRICES, *rate_card.TERMS],
    ids=lambda entry: getattr(entry, "item_id", getattr(entry, "term_id", "")),
)
def test_every_knowledge_base_figure_quotes_its_document_verbatim(entry):
    if entry.source != "knowledge_base":
        assert entry.quote is None
        return
    assert entry.quote and appears_in(entry.quote, kb_document(entry.reference))


def test_the_storage_price_is_the_knowledge_base_rate_per_cubic_meter_and_never_discounted():
    storage = rate_card.PRICE_BY_ID["storage_m3_month"]

    assert storage.amounts == {"USD": "18.00", "EUR": "16.00"}
    assert storage.discountable is False


def test_the_committed_sla_and_return_terms_respect_the_guidelines():
    assert rate_card.ON_TIME_SLA_PCT == 90
    assert rate_card.RETURNS_TURNAROUND_HOURS >= guidelines.RETURNS_FLOOR_HOURS
    assert rate_card.RETURN_WINDOW_DAYS == 30


def test_every_price_has_both_currencies_and_names_no_carrier():
    for price in rate_card.PRICES:
        assert set(price.amounts) == {"USD", "EUR"}
        assert price.amount("USD") > 0 and price.amount("EUR") > 0
        assert not guidelines.discloses_carrier_rate(f"{price.label} {price.amounts['USD']} USD")


@pytest.mark.parametrize("dept", DEPARTMENT_IDS)
def test_every_department_has_contiguous_ascending_tiers_from_zero(dept):
    tiers = rate_card.DISCOUNT_TIERS[dept]

    assert tiers[0].min_volume == 0 and tiers[-1].max_volume is None
    for lower, upper in zip(tiers, tiers[1:], strict=False):
        assert upper.min_volume == (lower.max_volume or 0) + 1
        assert upper.discount_pct > lower.discount_pct
    assert any(price.discountable for price in rate_card.prices_for(dept))


def test_the_sites_and_carriers_are_the_knowledge_base_ones():
    coverage = kb_document(rate_card.CARRIERS_DOC)
    assert rate_card.SITES["US"].city == "Los Angeles"
    assert rate_card.SITES["ES"].city == "Zaragoza"
    for name in rate_card.CARRIER_NAMES:
        assert name in coverage


def test_the_card_shows_one_currency_to_the_generator():
    text = rate_card.render_text("warehouse", "EUR", "ES")

    assert "16.00 EUR" in text
    assert "USD" not in text
    assert "Zaragoza" in text


@pytest.mark.parametrize("dept", DEPARTMENT_IDS)
def test_the_required_elements_are_what_briefing_2_1_says_the_department_contributes(dept):
    contributes = DEPARTMENT_BY_ID[dept].contributes.casefold()
    labels = [element.label.casefold() for element in sections.REQUIRED_ELEMENTS[dept]]

    assert all(label in contributes for label in labels)
    for part in contributes.split(", "):
        assert any(label in part for label in labels), part


@pytest.mark.parametrize("dept", DEPARTMENT_IDS)
def test_each_department_has_its_own_generator_tool_and_output_model(dept):
    assert sections.TOOL_NAMES[dept].startswith("write_")
    assert issubclass(sections.DRAFT_MODELS[dept], sections.SectionDraft)
    for document in sections.KB_DOCUMENTS[dept]:
        assert (knowledge.KB_DIR / document).is_file()
