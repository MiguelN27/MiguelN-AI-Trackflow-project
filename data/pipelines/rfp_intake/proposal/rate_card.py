"""TrackFlow's rate card: the only prices a proposal draft may quote.

OWNED BY COMMERCIAL (Miguel Torres, Commercial Director). Edit the values here;
bump `RATE_CARD_VERSION` with every change, so each draft records the card it
was priced from.

Two kinds of figure, marked per entry by `source`:

- `knowledge_base`: copied from the company knowledge base
  (`docs/company-knowledge-base/`, indexed in Qdrant), with the verbatim
  `quote` a test checks against the document. Storage price, grace period,
  long-term fee, the 90% on-time SLA, delivery times, the 30-day return window.
- `commercial_indicative`: INDICATIVE list prices and terms defined for this
  draft workflow, for what the knowledge base does not price (cost per
  shipment, returns processing, inbound and pick & pack fees, onboarding
  time, capacity, the volume discount tiers). Commercial must confirm them
  before a proposal leaves TrackFlow; every draft says the card is indicative.

The knowledge base is the authority where both speak: storage is charged per
cubic meter occupied, never per unit, so the briefing's "cost per pallet/SKU"
(§2.1) is covered by the inbound-per-pallet and SKU-setup fees. Storage is
never discountable: a preferential storage rate (clients averaging more than
50 cubic meters) is negotiated only by Miguel Torres.

No entry names a carrier: proposals quote TrackFlow's own price to the client,
never a carrier's rate (briefing §5).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from data.pipelines.rfp_intake.briefing import Country, Currency, DepartmentId

RATE_CARD_VERSION: Final = "2026-11-indicative-1"
RATE_CARD_OWNER: Final = "Miguel Torres (Commercial Director)"

Source = Literal["knowledge_base", "commercial_indicative"]

STORAGE_DOC: Final = "trackflow-storage-pricing.en.md"
SLA_DOC: Final = "trackflow-sla-delivery.en.md"
CARRIERS_DOC: Final = "trackflow-carrier-coverage.en.md"
RETURNS_DOC: Final = "trackflow-returns-policy.en.md"
INDICATIVE: Final = "Commercial indicative list price (to be confirmed by Miguel Torres)"


@dataclass(frozen=True)
class Price:
    item_id: str
    department_id: DepartmentId
    label: str
    unit: str
    amounts: Mapping[Currency, str]
    source: Source
    discountable: bool
    reference: str
    quote: str | None = None

    def amount(self, currency: Currency) -> Decimal:
        return Decimal(self.amounts[currency])


@dataclass(frozen=True)
class Term:
    """A commitment that is not a price: a time, a percentage, a capacity."""

    term_id: str
    department_id: DepartmentId | None
    label: str
    value: int
    unit: str
    source: Source
    reference: str
    quote: str | None = None


@dataclass(frozen=True)
class DiscountTier:
    min_volume: int
    max_volume: int | None
    discount_pct: int


@dataclass(frozen=True)
class Site:
    country: Country
    city: str
    pallet_positions: int
    monthly_orders: int
    express_coverage: str
    carriers: tuple[str, ...] = field(default=())


PRICES: Final[tuple[Price, ...]] = (
    # --- Warehouse Operations (Ana Whitfield) ---------------------------------------------
    Price(
        item_id="storage_m3_month",
        department_id="warehouse",
        label="Storage, per cubic meter occupied",
        unit="per m³ per month",
        amounts={"USD": "18.00", "EUR": "16.00"},
        source="knowledge_base",
        discountable=False,
        reference=STORAGE_DOC,
        quote=(
            "Standard rate: 18 USD per cubic meter per month (Los Angeles) or 16 EUR "
            "per cubic meter per month (Zaragoza)"
        ),
    ),
    Price(
        item_id="inbound_pallet",
        department_id="warehouse",
        label="Inbound receiving",
        unit="per pallet",
        amounts={"USD": "9.50", "EUR": "8.50"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="sku_setup",
        department_id="warehouse",
        label="SKU setup (one-off)",
        unit="per SKU",
        amounts={"USD": "0.30", "EUR": "0.25"},
        source="commercial_indicative",
        discountable=False,
        reference=INDICATIVE,
    ),
    Price(
        item_id="pick_pack_order",
        department_id="warehouse",
        label="Pick and pack",
        unit="per order",
        amounts={"USD": "2.40", "EUR": "2.10"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="pick_pack_extra_item",
        department_id="warehouse",
        label="Each additional item in the same order",
        unit="per item",
        amounts={"USD": "0.45", "EUR": "0.40"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    # --- Last Mile and Carrier Management (Carlos Vega) -----------------------------------
    Price(
        item_id="shipment_standard_2kg",
        department_id="lastmile",
        label="Standard delivery, parcel up to 2 kg (3 to 5 business days)",
        unit="per shipment",
        amounts={"USD": "6.90", "EUR": "4.90"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="shipment_standard_5kg",
        department_id="lastmile",
        label="Standard delivery, parcel from 2 to 5 kg (3 to 5 business days)",
        unit="per shipment",
        amounts={"USD": "8.90", "EUR": "6.40"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="shipment_express",
        department_id="lastmile",
        label="Express delivery (24 to 48 hours, express coverage area only)",
        unit="per shipment",
        amounts={"USD": "12.50", "EUR": "8.90"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="shipment_international",
        department_id="lastmile",
        label="International delivery between the United States and Spain "
        "(7 to 12 business days, customs not included)",
        unit="per shipment",
        amounts={"USD": "24.00", "EUR": "22.00"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    # --- Reverse Logistics (Sofía Ramos) --------------------------------------------------
    Price(
        item_id="return_processing",
        department_id="reverse",
        label="Returns processing (receive, inspect, classify and restock)",
        unit="per return",
        amounts={"USD": "3.20", "EUR": "2.80"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="return_recondition",
        department_id="reverse",
        label="Reconditioning, when the client's rules ask for it",
        unit="per unit",
        amounts={"USD": "1.80", "EUR": "1.60"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
    Price(
        item_id="return_disposal",
        department_id="reverse",
        label="Disposal, when the client's rules ask for it",
        unit="per unit",
        amounts={"USD": "0.60", "EUR": "0.50"},
        source="commercial_indicative",
        discountable=True,
        reference=INDICATIVE,
    ),
)

PRICE_BY_ID: Final[dict[str, Price]] = {price.item_id: price for price in PRICES}

TERMS: Final[tuple[Term, ...]] = (
    Term(
        term_id="storage_grace_days",
        department_id="warehouse",
        label="Storage grace period for any new product",
        value=30,
        unit="days",
        source="knowledge_base",
        reference=STORAGE_DOC,
        quote="The first 30 days of any new product in inventory incur no storage charge",
    ),
    Term(
        term_id="long_term_fee_pct",
        department_id="warehouse",
        label="Long-term inventory fee over the standard rate, from day 181 without movement",
        value=150,
        unit="%",
        source="knowledge_base",
        reference=STORAGE_DOC,
        quote='pays a "long-term inventory" fee of 150% over the standard rate, starting on '
        "day 181",
    ),
    Term(
        term_id="preferential_storage_threshold_m3",
        department_id="warehouse",
        label="Average monthly volume above which a preferential storage rate can be negotiated",
        value=50,
        unit="m³",
        source="knowledge_base",
        reference=STORAGE_DOC,
        quote="Clients with more than 50 cubic meters of average monthly volume can negotiate a "
        "preferential rate",
    ),
    Term(
        term_id="onboarding_business_days",
        department_id="warehouse",
        label="Onboarding time, from signed contract to first order shipped",
        value=10,
        unit="business days",
        source="commercial_indicative",
        reference=INDICATIVE,
    ),
    Term(
        term_id="on_time_sla_pct",
        department_id=None,
        label="On-time delivery SLA, per month, measured by country",
        value=90,
        unit="%",
        source="knowledge_base",
        reference=SLA_DOC,
        quote="The contractually committed SLA is 90% on-time deliveries per month, measured by "
        "country",
    ),
    Term(
        term_id="sla_compensation_pct",
        department_id=None,
        label="Compensation on that country's monthly fee after two consecutive months below 90%",
        value=5,
        unit="%",
        source="knowledge_base",
        reference=SLA_DOC,
        quote="the client is entitled to a 5% compensation on that country's monthly fee",
    ),
    Term(
        term_id="peak_extension_pct",
        department_id=None,
        label="Possible extension of delivery times on declared high-demand dates",
        value=40,
        unit="%",
        source="knowledge_base",
        reference=SLA_DOC,
        quote="delivery times may extend by up to an additional 40%",
    ),
    Term(
        term_id="returns_turnaround_hours",
        department_id="reverse",
        label="Returns turnaround, from arrival at the warehouse to classification",
        value=72,
        unit="hours",
        source="commercial_indicative",
        reference=INDICATIVE,
    ),
    Term(
        term_id="return_window_days",
        department_id="reverse",
        label="Standard return window from delivery",
        value=30,
        unit="days",
        source="knowledge_base",
        reference=RETURNS_DOC,
        quote="Standard return window: 30 days from delivery",
    ),
)

TERM_BY_ID: Final[dict[str, Term]] = {term.term_id: term for term in TERMS}

ON_TIME_SLA_PCT: Final = TERM_BY_ID["on_time_sla_pct"].value
RETURNS_TURNAROUND_HOURS: Final = TERM_BY_ID["returns_turnaround_hours"].value
ONBOARDING_BUSINESS_DAYS: Final = TERM_BY_ID["onboarding_business_days"].value
RETURN_WINDOW_DAYS: Final = TERM_BY_ID["return_window_days"].value

# Delivery times are the knowledge base's (`trackflow-sla-delivery.en.md`).
DELIVERY_TIMES: Final[dict[str, str]] = {
    "standard": "3 to 5 business days within the same country",
    "express": "24 to 48 hours within the same country",
    "international": "7 to 12 business days between the United States and Spain, subject to "
    "customs",
}

# Where each country's operation runs, per the knowledge base (Los Angeles and
# Zaragoza). Capacity is an indicative Operations figure (`commercial_indicative`).
SITES: Final[dict[str, Site]] = {
    "US": Site(
        country="US",
        city="Los Angeles",
        pallet_positions=6000,
        monthly_orders=60000,
        express_coverage="Los Angeles metropolitan area",
        carriers=("UPS", "FedEx", "DHL"),
    ),
    "ES": Site(
        country="ES",
        city="Zaragoza",
        pallet_positions=5000,
        monthly_orders=45000,
        express_coverage="Zaragoza-Madrid corridor",
        carriers=("MRW", "SEUR", "DHL", "local Zaragoza carriers"),
    ),
}

# Every carrier the knowledge base names, for the "no carrier rates" check.
CARRIER_NAMES: Final = ("UPS", "FedEx", "DHL", "MRW", "SEUR")

# The volume each department's tiers count, per month.
TIER_UNIT: Final[dict[str, str]] = {
    "warehouse": "orders",
    "lastmile": "shipments",
    "reverse": "returns",
}

# Volume-based discount tiers (briefing §5: every proposal includes a tier
# table). Applied to the department's discountable prices only.
DISCOUNT_TIERS: Final[dict[str, tuple[DiscountTier, ...]]] = {
    "warehouse": (
        DiscountTier(0, 2499, 0),
        DiscountTier(2500, 9999, 5),
        DiscountTier(10000, 24999, 8),
        DiscountTier(25000, None, 12),
    ),
    "lastmile": (
        DiscountTier(0, 2499, 0),
        DiscountTier(2500, 9999, 5),
        DiscountTier(10000, 24999, 8),
        DiscountTier(25000, None, 12),
    ),
    "reverse": (
        DiscountTier(0, 499, 0),
        DiscountTier(500, 1999, 5),
        DiscountTier(2000, 4999, 8),
        DiscountTier(5000, None, 12),
    ),
}


def prices_for(department_id: str) -> tuple[Price, ...]:
    return tuple(price for price in PRICES if price.department_id == department_id)


def terms_for(department_id: str) -> tuple[Term, ...]:
    return tuple(term for term in TERMS if term.department_id in (department_id, None))


def format_amount(amount: Decimal) -> str:
    return f"{amount:.2f}"


def format_volume(value: int) -> str:
    return f"{value:,}"


def tier_range(tier: DiscountTier, unit: str) -> str:
    if tier.max_volume is None:
        return f"{format_volume(tier.min_volume)} or more {unit} a month"
    return f"{format_volume(tier.min_volume)} to {format_volume(tier.max_volume)} {unit} a month"


def _price_text(price: Price, currency: Currency | None) -> str:
    if currency is not None:
        return f"{format_amount(price.amount(currency))} {currency}"
    # The client's country is unknown: both, so the draft can still be written.
    return " or ".join(f"{format_amount(price.amount(code))} {code}" for code in ("USD", "EUR"))


def render_text(department_id: str, currency: Currency | None, country: str | None) -> str:
    """The card as one department's generator reads it: the client's currency, no other."""
    shown = currency or "USD for US operations or EUR for Spain (the country is unknown)"
    lines = [f"RATE CARD {RATE_CARD_VERSION} (prices in {shown}; indicative where marked)"]
    lines.append("Prices (item_id: label, amount, unit):")
    for price in prices_for(department_id):
        marker = "" if price.source == "knowledge_base" else " [indicative]"
        discount = "discountable" if price.discountable else "not discountable"
        lines.append(
            f"- {price.item_id}: {price.label}, {_price_text(price, currency)} "
            f"{price.unit} ({discount}){marker}"
        )
    unit = TIER_UNIT[department_id]
    lines.append(f"Volume discount tiers (by {unit} a month, on discountable prices only):")
    for tier in DISCOUNT_TIERS[department_id]:
        lines.append(f"- {tier_range(tier, unit)}: {tier.discount_pct}%")
    lines.append("Terms:")
    for term in terms_for(department_id):
        marker = "" if term.source == "knowledge_base" else " [indicative]"
        lines.append(f"- {term.term_id}: {term.label}: {term.value} {term.unit}{marker}")
    site = SITES.get(country or "")
    if site is not None:
        lines.append(
            f"Site: {site.city}. Capacity: {format_volume(site.pallet_positions)} pallet "
            f"positions, up to {format_volume(site.monthly_orders)} orders a month [indicative]. "
            f"Express coverage: {site.express_coverage}."
        )
        lines.append(f"Carriers in this country: {', '.join(site.carriers)}.")
    lines.append("Delivery times: " + "; ".join(f"{k}: {v}" for k, v in DELIVERY_TIMES.items()))
    return "\n".join(lines)
