"""The format of each department's proposal section, from briefing §2.1.

§2.1 says what each department contributes to the proposal:

- `warehouse`: "Storage capacity, cost per pallet/SKU, onboarding time"
- `lastmile`:  "Cost per shipment, available carriers by destination, delivery SLA"
- `reverse`:   "Returns processing cost and turnaround time (if the client requests it)"

Those are the section's required elements (`REQUIRED_ELEMENTS`, kept equal to
the briefing by a test), and the generators' output models carry a field for
each, so a draft states them as structured data, not only as prose. The same
fields are the structured state Part 3's conflict triggers (§7) read.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake.proposal import rate_card

# The fixed headings every rendered section uses, in order.
SCOPE_HEADING: Final = "### Scope and approach"
PRICING_HEADING: Final = "### Pricing"
DISCOUNT_TABLE_HEADING: Final = "### Volume discount tiers"
COMMITMENTS_HEADING: Final = "### Service commitments"
CARRIERS_HEADING: Final = "### Carriers by destination"
ASSUMPTIONS_HEADING: Final = "### Assumptions"
PENDING_HEADING: Final = "### Pending information"


# --- The generators' output ---------------------------------------------------------------


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PriceLine(_Lenient):
    item_id: str = Field(description="A rate card item_id of your department, exactly")
    amount: float = Field(description="The rate card amount for that item, unchanged")
    currency: str = Field(description="The client's currency: USD or EUR")
    note: str = Field(default="", description="Optional, short; no carrier names")


class TierRow(_Lenient):
    min_monthly_volume: int
    max_monthly_volume: int | None = None
    discount_pct: float


class SectionDraft(_Lenient):
    """Shared by every department's section."""

    summary: str = Field(
        default="", description="Two or three plain sentences: what TrackFlow offers this client"
    )
    narrative: list[str] = Field(
        default_factory=list,
        description="Two to four short paragraphs explaining the approach, for this client",
    )
    price_lines: list[PriceLine] = Field(default_factory=list)
    discount_tiers: list[TierRow] = Field(
        default_factory=list, description="Your department's volume discount tiers, unchanged"
    )
    on_time_sla_pct: float | None = Field(
        default=None,
        description="TrackFlow's on-time delivery SLA (%), only when this section carries it",
    )
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(
        default_factory=list, description="What the client or a department still has to tell us"
    )


class Capacity(_Lenient):
    pallet_positions: int | None = None
    monthly_orders: int | None = None


class WarehouseDraft(SectionDraft):
    """Write the Warehouse Operations section of the pricing proposal."""

    storage_site: str | None = Field(default=None, description="The warehouse city")
    committed_capacity: Capacity | None = None
    onboarding_business_days: int | None = None


class DeliveryOption(_Lenient):
    service: str = Field(description="standard, express or international")
    transit_time: str
    coverage: str = ""


class CarrierCoverage(_Lenient):
    destination: str
    carriers: list[str] = Field(default_factory=list)


class LastmileDraft(SectionDraft):
    """Write the Last Mile and Carrier Management section of the pricing proposal."""

    delivery_options: list[DeliveryOption] = Field(default_factory=list)
    carriers_by_destination: list[CarrierCoverage] = Field(default_factory=list)
    assumed_monthly_volume: int | None = Field(
        default=None, description="The client's monthly orders, only if the RFP states it"
    )


class ReverseDraft(SectionDraft):
    """Write the Reverse Logistics (returns) section of the pricing proposal."""

    returns_turnaround_hours: int | None = None
    return_window_days: int | None = None
    return_shipping_paid_by: str | None = None
    international_returns: str | None = None


DRAFT_MODELS: Final[dict[str, type[SectionDraft]]] = {
    "warehouse": WarehouseDraft,
    "lastmile": LastmileDraft,
    "reverse": ReverseDraft,
}

TOOL_NAMES: Final[dict[str, str]] = {
    "warehouse": "write_warehouse_section",
    "lastmile": "write_lastmile_section",
    "reverse": "write_returns_section",
}


# --- Required elements (briefing §2.1) ----------------------------------------------------


def _has_price(*item_ids: str) -> Callable[[dict[str, Any]], bool]:
    return lambda draft: any(
        line.get("item_id") in item_ids for line in draft.get("price_lines") or []
    )


def _capacity(draft: dict[str, Any]) -> bool:
    capacity = draft.get("committed_capacity") or {}
    return bool(capacity.get("pallet_positions") or capacity.get("monthly_orders"))


@dataclass(frozen=True)
class RequiredElement:
    label: str
    present: Callable[[dict[str, Any]], bool]
    fix: str


REQUIRED_ELEMENTS: Final[dict[str, tuple[RequiredElement, ...]]] = {
    "warehouse": (
        RequiredElement(
            "Storage capacity",
            _capacity,
            "state committed_capacity (pallet positions and orders a month) from the rate card",
        ),
        RequiredElement(
            "cost per pallet/SKU",
            _has_price("storage_m3_month", "inbound_pallet", "sku_setup"),
            "add the price lines storage_m3_month, inbound_pallet and sku_setup",
        ),
        RequiredElement(
            "onboarding time",
            lambda draft: draft.get("onboarding_business_days") is not None,
            "state onboarding_business_days from the rate card",
        ),
    ),
    "lastmile": (
        RequiredElement(
            "Cost per shipment",
            _has_price(*(p.item_id for p in rate_card.prices_for("lastmile"))),
            "add the per-shipment price lines from the rate card",
        ),
        RequiredElement(
            "available carriers by destination",
            lambda draft: any(
                row.get("carriers") for row in draft.get("carriers_by_destination") or []
            ),
            "list carriers_by_destination with the knowledge base's carriers for the country",
        ),
        RequiredElement(
            "delivery SLA",
            lambda draft: bool(draft.get("delivery_options")),
            "list delivery_options with the knowledge base's delivery times",
        ),
    ),
    "reverse": (
        RequiredElement(
            "Returns processing cost",
            _has_price("return_processing"),
            "add the price line return_processing",
        ),
        RequiredElement(
            "turnaround time",
            lambda draft: draft.get("returns_turnaround_hours") is not None,
            "state returns_turnaround_hours from the rate card (never under 48)",
        ),
    ),
}


def missing_required(department_id: str, draft: dict[str, Any]) -> list[RequiredElement]:
    return [element for element in REQUIRED_ELEMENTS[department_id] if not element.present(draft)]


# --- Knowledge-base queries ---------------------------------------------------------------

# One query per subject: each extra query is another embedding call.
KB_QUERIES: Final[dict[str, tuple[str, ...]]] = {
    "warehouse": ("storage pricing per cubic meter, grace period and long-term inventory fee",),
    "lastmile": (
        "carrier coverage by country and destination",
        "delivery SLA: on-time commitment, delivery times and high-demand dates",
    ),
    "reverse": ("returns policy: return window, labels, inspection and return costs",),
}
SLA_QUERY: Final = KB_QUERIES["lastmile"][1]

# The documents a department reads when the vector database is unavailable.
KB_DOCUMENTS: Final[dict[str, tuple[str, ...]]] = {
    "warehouse": (rate_card.STORAGE_DOC,),
    "lastmile": (rate_card.CARRIERS_DOC, rate_card.SLA_DOC),
    "reverse": (rate_card.RETURNS_DOC,),
}


def kb_queries(department_id: str, *, is_sla_section: bool) -> list[str]:
    queries = list(KB_QUERIES[department_id])
    if is_sla_section and SLA_QUERY not in queries:
        queries.append(SLA_QUERY)
    return queries


def kb_documents(department_id: str, *, is_sla_section: bool) -> list[str]:
    documents = list(KB_DOCUMENTS[department_id])
    if is_sla_section and rate_card.SLA_DOC not in documents:
        documents.append(rate_card.SLA_DOC)
    return documents
