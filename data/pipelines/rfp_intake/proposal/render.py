"""A structured section draft as the Markdown handed to approval (`draft_content`).

Deterministic: the generator writes structured fields and prose, and this
module lays them out under the same headings for every department. Facts the
knowledge base fixes (the SLA caveats, the storage grace period, who may
negotiate storage) are written here from the rate card, not by the model, so
they are worded the same way in every proposal.

Amounts are printed in the currency the draft states, unchanged: a wrong
currency must stay visible for the compliance check, not be silently fixed.
"""

from decimal import Decimal, InvalidOperation
from typing import Any, Final

from data.pipelines.rfp_intake.briefing import COMMERCIAL_DIRECTOR
from data.pipelines.rfp_intake.proposal import rate_card
from data.pipelines.rfp_intake.proposal.sections import (
    ASSUMPTIONS_HEADING,
    CARRIERS_HEADING,
    COMMITMENTS_HEADING,
    DISCOUNT_TABLE_HEADING,
    PENDING_HEADING,
    PRICING_HEADING,
    SCOPE_HEADING,
)
from data.pipelines.rfp_intake.text import comparable, squash

NO_PRICES: Final = "No price could be quoted in this draft."
NOTHING_PENDING: Final = "Nothing pending."


def _cell(text: Any) -> str:
    return squash(str(text)).replace("|", "/")


def _sentence(text: str) -> str:
    text = squash(text)
    return text if not text or text[-1] in ".!?" else f"{text}."


def _amount(value: Any) -> str:
    try:
        return rate_card.format_amount(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return _cell(value)


def _pct(value: float) -> str:
    return f"{int(value)}%" if float(value).is_integer() else f"{value}%"


def sla_sentences(pct: float, department_id: str) -> list[str]:
    scope = "" if department_id == "lastmile" else " for the deliveries TrackFlow manages"
    compensation = rate_card.TERM_BY_ID["sla_compensation_pct"].value
    peak = rate_card.TERM_BY_ID["peak_extension_pct"].value
    return [
        f"On-time delivery SLA: TrackFlow commits to {_pct(pct)} on-time deliveries per month, "
        f"measured by country{scope}.",
        f"If a country falls below {_pct(pct)} for two consecutive months, the client is "
        f"entitled to a {compensation}% compensation on that country's monthly fee.",
        "The SLA is not guaranteed on declared high-demand dates (Black Friday, Christmas, "
        f"January Sales in Spain), when delivery times may extend by up to {peak}%.",
    ]


def _commitments(assignment: dict[str, Any], draft: dict[str, Any]) -> list[str]:
    department_id = assignment["department_id"]
    lines: list[str] = []
    if department_id == "warehouse":
        site = draft.get("storage_site")
        capacity = draft.get("committed_capacity") or {}
        if site:
            lines.append(f"Storage site: {_cell(site)}.")
        if capacity.get("pallet_positions") or capacity.get("monthly_orders"):
            parts = []
            if capacity.get("pallet_positions"):
                parts.append(f"{capacity['pallet_positions']:,} pallet positions")
            if capacity.get("monthly_orders"):
                parts.append(f"up to {capacity['monthly_orders']:,} orders a month")
            lines.append(f"Committed capacity: {' and '.join(parts)}.")
        if draft.get("onboarding_business_days") is not None:
            lines.append(f"Onboarding: {draft['onboarding_business_days']} business days.")
        grace = rate_card.TERM_BY_ID["storage_grace_days"].value
        fee = rate_card.TERM_BY_ID["long_term_fee_pct"].value
        threshold = rate_card.TERM_BY_ID["preferential_storage_threshold_m3"].value
        lines.append(
            f"The first {grace} days of any new product in inventory incur no storage charge."
        )
        lines.append(
            "Inventory with more than 180 days of no movement pays a long-term inventory fee "
            f"of {fee}% over the standard storage rate, starting on day 181."
        )
        lines.append(
            f"Clients averaging more than {threshold} cubic meters a month can ask for a "
            f"preferential storage rate, negotiated only by {COMMERCIAL_DIRECTOR}."
        )
    elif department_id == "lastmile":
        for option in draft.get("delivery_options") or []:
            coverage = f" ({_cell(option['coverage'])})" if option.get("coverage") else ""
            lines.append(
                f"{_cell(option.get('service', '')).capitalize()} delivery: "
                f"{_cell(option.get('transit_time', ''))}{coverage}."
            )
        if draft.get("assumed_monthly_volume"):
            lines.append(
                f"Planned volume: {draft['assumed_monthly_volume']:,} orders a month, as the "
                "client states."
            )
    elif department_id == "reverse":
        if draft.get("returns_turnaround_hours") is not None:
            lines.append(
                f"Returns turnaround: {draft['returns_turnaround_hours']} hours from arrival at "
                "the warehouse to classification."
            )
        if draft.get("return_window_days") is not None:
            lines.append(f"Return window: {draft['return_window_days']} days from delivery.")
        if draft.get("return_shipping_paid_by"):
            lines.append(_sentence(f"Return shipping: {draft['return_shipping_paid_by']}"))
        if draft.get("international_returns"):
            lines.append(_sentence(f"International returns: {draft['international_returns']}"))
    if assignment.get("is_sla_section") and draft.get("on_time_sla_pct") is not None:
        lines.extend(sla_sentences(draft["on_time_sla_pct"], department_id))
    return lines


def _pending(assignment: dict[str, Any], draft: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    pending: list[str] = []
    for question in [*(draft.get("open_questions") or []), *assignment.get("open_questions", [])]:
        key = comparable(question)
        if key and key not in seen:
            seen.add(key)
            pending.append(_sentence(question))
    return pending


def render_section(assignment: dict[str, Any], draft: dict[str, Any]) -> str:
    department_id = assignment["department_id"]
    client = assignment.get("client_name") or "the client"
    currency = assignment.get("currency") or "the client's currency (country unknown)"
    out: list[str] = [
        f"## {assignment['department_name']}: {_cell(client)}",
        "",
        f"For approval by {assignment['owner']}. Prices in {currency}. Rate card "
        f"{rate_card.RATE_CARD_VERSION}: indicative prices, to be confirmed by Commercial.",
        "",
    ]
    if draft.get("summary"):
        out += [_sentence(draft["summary"]), ""]

    out += [SCOPE_HEADING, ""]
    for paragraph in draft.get("narrative") or []:
        if squash(paragraph):
            out += [squash(paragraph), ""]

    out += [PRICING_HEADING, ""]
    lines = draft.get("price_lines") or []
    if lines:
        out += ["| Item | Price | Unit | Note |", "|---|---|---|---|"]
        for line in lines:
            price = rate_card.PRICE_BY_ID.get(line.get("item_id", ""))
            label = price.label if price else line.get("item_id", "")
            unit = price.unit if price else ""
            out.append(
                f"| {_cell(label)} | {_amount(line.get('amount'))} {_cell(line.get('currency'))} "
                f"| {_cell(unit)} | {_cell(line.get('note') or '')} |"
            )
    else:
        out.append(NO_PRICES)
    out.append("")

    out += [DISCOUNT_TABLE_HEADING, ""]
    tiers = draft.get("discount_tiers") or []
    unit_name = rate_card.TIER_UNIT[department_id]
    if tiers:
        excluded = [
            rate_card.PRICE_BY_ID[line["item_id"]].label.lower()
            for line in lines
            if line.get("item_id") in rate_card.PRICE_BY_ID
            and not rate_card.PRICE_BY_ID[line["item_id"]].discountable
        ]
        applies = "every price above" + (f" except {' and '.join(excluded)}" if excluded else "")
        out += [
            f"Discounts by {unit_name} a month apply to {applies}.",
            "",
            f"| Monthly volume ({unit_name}) | Discount |",
            "|---|---|",
        ]
        for tier in tiers:
            band = rate_card.tier_range(
                rate_card.DiscountTier(
                    int(tier["min_monthly_volume"]),
                    tier.get("max_monthly_volume"),
                    int(tier["discount_pct"]),
                ),
                unit_name,
            )
            out.append(f"| {band} | {_pct(tier['discount_pct'])} |")
    else:
        out.append("No volume discount tiers in this draft.")
    out.append("")

    out += [COMMITMENTS_HEADING, ""]
    out += [f"- {line}" for line in _commitments(assignment, draft)] or ["- None stated."]
    out.append("")

    if department_id == "lastmile":
        out += [CARRIERS_HEADING, ""]
        rows = [row for row in draft.get("carriers_by_destination") or [] if row.get("carriers")]
        if rows:
            out += ["| Destination | Carriers |", "|---|---|"]
            out += [
                f"| {_cell(row['destination'])} | {_cell(', '.join(row['carriers']))} |"
                for row in rows
            ]
        else:
            out.append("No carriers listed in this draft.")
        out.append("")

    out += [ASSUMPTIONS_HEADING, ""]
    assumptions = [_sentence(item) for item in draft.get("assumptions") or [] if squash(item)]
    out += [f"- {item}" for item in assumptions] or ["- None."]
    out.append("")

    out += [PENDING_HEADING, ""]
    out += [f"- {item}" for item in _pending(assignment, draft)] or [f"- {NOTHING_PENDING}"]
    return "\n".join(out).rstrip() + "\n"


def prose_for_readability(draft: dict[str, Any]) -> str:
    """The section's own prose (summary, narrative, assumptions), without tables or headings."""
    parts = [draft.get("summary") or "", *(draft.get("narrative") or [])]
    parts += draft.get("assumptions") or []
    return "\n\n".join(_sentence(part) for part in parts if squash(part))
