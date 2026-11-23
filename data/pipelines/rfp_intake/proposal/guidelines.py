"""The company guidelines a proposal section is checked against (briefing §5).

The ticket's `company-BRIEFING.md` is the root `agentic-workflow-context.md`;
its §5 "Business Constraints (Guidelines for the Compliance Evaluator)" lists
five rules. Each is one `Guideline` here, with the briefing's own words (a
test parses §5 so the two cannot drift), the sections it applies to, the fix
the generator is told, and the Part 3 conflict trigger (§7) it feeds.

The checks are deterministic and they are the authority: a model reviewer
(`agents/compliance.py`) may add violations, never remove one.

Where a rule applies:
- "every proposal must state the on-time delivery SLA" is checked on one
  designated section (decided with the developer, 2026-11-23): `lastmile`
  when last mile is in scope, otherwise the first active section in the
  briefing's order (`sla_department`).
- The other four apply to every section: each section is quoted in the
  client's currency, has its own volume discount tiers, and must not promise
  fast returns or disclose carrier rates.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from data.pipelines.rfp_intake.briefing import COUNTRY_NAMES, DEPARTMENT_IDS, DepartmentId
from data.pipelines.rfp_intake.proposal import rate_card
from data.pipelines.rfp_intake.proposal.results import RuleId, Violation
from data.pipelines.rfp_intake.proposal.sections import DISCOUNT_TABLE_HEADING
from data.pipelines.rfp_intake.text import squash

Applies = Literal["every_section", "sla_section"]

RETURNS_FLOOR_HOURS: Final = 48


@dataclass(frozen=True)
class Guideline:
    rule_id: RuleId
    briefing_text: str
    applies: Applies
    remedy: str
    conflict_trigger: str | None = None


# Briefing §5, verbatim and in order.
GUIDELINES: Final[tuple[Guideline, ...]] = (
    Guideline(
        rule_id="currency-by-country",
        briefing_text=(
            "Pricing is quoted in USD for US operations and in EUR for Spain operations — "
            "determined by the `client_country` field."
        ),
        applies="every_section",
        remedy="Quote every price in {currency} only ({country} operations).",
        conflict_trigger="currency-mismatch",
    ),
    Guideline(
        rule_id="on-time-sla-stated",
        briefing_text="Every proposal must state the on-time delivery SLA (%) TrackFlow is "
        "committing to.",
        applies="sla_section",
        remedy=(
            "This section carries the proposal's SLA: set on_time_sla_pct to {sla} "
            "({sla}% on-time deliveries per month, measured by country)."
        ),
    ),
    Guideline(
        rule_id="returns-min-48h",
        briefing_text="No proposal may promise returns processing in under 48 hours.",
        applies="every_section",
        remedy=(
            "No proposal may promise returns processing in under 48 hours: use the rate "
            "card's turnaround of {turnaround} hours and remove any faster promise."
        ),
        conflict_trigger="returns-sla-breach",
    ),
    Guideline(
        rule_id="volume-discount-table",
        briefing_text="Every proposal must include a volume-based discount tier table.",
        applies="every_section",
        remedy="Include discount_tiers exactly as your department's rate card tiers.",
    ),
    Guideline(
        rule_id="no-carrier-rates",
        briefing_text=(
            "No proposal may disclose negotiated rates with specific carriers — only the "
            "final cost offered to the client."
        ),
        applies="every_section",
        remedy=(
            "Never pair a carrier with a rate, a discount or what TrackFlow pays it; quote "
            "only TrackFlow's own price to the client."
        ),
    ),
)

GUIDELINE_BY_ID: Final[dict[str, Guideline]] = {rule.rule_id: rule for rule in GUIDELINES}


def sla_department(active: Sequence[str]) -> DepartmentId:
    """The section that must state the on-time delivery SLA: lastmile, else the first."""
    present = [dept for dept in DEPARTMENT_IDS if dept in active]
    if not present:
        raise ValueError("a proposal has at least one department")
    return "lastmile" if "lastmile" in present else present[0]


def applicable_rule_ids(*, is_sla_section: bool) -> list[RuleId]:
    return [
        rule.rule_id for rule in GUIDELINES if rule.applies == "every_section" or is_sla_section
    ]


def remedy(rule_id: str, currency: str | None, country: str | None) -> str:
    return GUIDELINE_BY_ID[rule_id].remedy.format(
        currency=currency or "the client's currency",
        country=COUNTRY_NAMES.get(country or "", "the client's"),
        sla=rate_card.ON_TIME_SLA_PCT,
        turnaround=rate_card.RETURNS_TURNAROUND_HOURS,
    )


# --- Reading a rendered section -----------------------------------------------------------

_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(¿¡])")
_LIST_MARKER: Final = re.compile(r"^(?:[-*•]|\d{1,2}[.)])\s+")
_TABLE_RULE: Final = re.compile(r"^\|?[\s:|-]+\|?$")


def statements(markdown: str) -> list[str]:
    """The section's sentences and table rows, each a verbatim passage of it."""
    found: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _TABLE_RULE.match(line):
            continue
        if line.startswith("|"):
            found.append(line)
            continue
        line = _LIST_MARKER.sub("", line)
        found.extend(part.strip() for part in _SENTENCE_BREAK.split(line) if part.strip())
    return found


# --- currency-by-country ------------------------------------------------------------------

_CURRENCY_MARKERS: Final[dict[str, re.Pattern[str]]] = {
    "USD": re.compile(r"\bUSD\b|US\$|\$|\bdollars?\b", re.IGNORECASE),
    "EUR": re.compile(r"\bEUR\b|€|\beuros?\b", re.IGNORECASE),
}


def _check_currency(draft: dict[str, Any], markdown: str, currency: str | None) -> list[Violation]:
    if currency is None:
        return [
            Violation(
                rule_id="currency-by-country",
                message="The client's country is unknown, so the proposal currency cannot be "
                "set (US -> USD, Spain -> EUR). Sales must ask the client.",
                retryable=False,
            )
        ]
    found: list[Violation] = []
    for line in draft.get("price_lines") or []:
        if line.get("currency") != currency:
            found.append(
                Violation(
                    rule_id="currency-by-country",
                    message=f"The price of {line.get('item_id')} is in {line.get('currency')}, "
                    f"not {currency}.",
                    quote=f"{line.get('item_id')}: {line.get('amount')} {line.get('currency')}",
                )
            )
    other = "EUR" if currency == "USD" else "USD"
    for statement in statements(markdown):
        if _CURRENCY_MARKERS[other].search(statement):
            found.append(
                Violation(
                    rule_id="currency-by-country",
                    message=f"The section mentions {other}; this client is quoted in {currency}.",
                    quote=statement,
                )
            )
    return found


# --- on-time-sla-stated -------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{int(value)}%" if float(value).is_integer() else f"{value}%"


def _check_sla(draft: dict[str, Any], markdown: str) -> list[Violation]:
    pct = draft.get("on_time_sla_pct")
    if pct is None:
        return [
            Violation(
                rule_id="on-time-sla-stated",
                message="This section must state the on-time delivery SLA (%) TrackFlow "
                "commits to, and it does not.",
            )
        ]
    if not re.search(rf"(?<![\d.]){re.escape(_pct(pct))}", markdown):
        return [
            Violation(
                rule_id="on-time-sla-stated",
                message=f"The on-time delivery SLA ({_pct(pct)}) is not stated in the section.",
            )
        ]
    return []


# --- returns-min-48h ----------------------------------------------------------------------

_RETURNS_WORD: Final = re.compile(
    r"\b(?:returns?|returned|reverse|rma|devoluci\w*)\b", re.IGNORECASE
)
_NUMBER: Final = r"(\d+(?:[.,]\d+)?)"
_UNIT: Final = r"(hours?|hrs?|h|business days?|working days?|days?)"
_RANGE: Final = re.compile(
    rf"{_NUMBER}\s*(?:-|–|—|to|or)\s*{_NUMBER}\s*-?\s*{_UNIT}(?![A-Za-z])", re.IGNORECASE
)
_SINGLE: Final = re.compile(rf"{_NUMBER}\s*-?\s*{_UNIT}(?![A-Za-z])", re.IGNORECASE)
_WORDED: Final = re.compile(
    r"\b(?:within|in|under|less than)\s+(?:a|one)\s+(?:business\s+|working\s+)?day\b"
    r"|\bsame[- ]day\b|\bnext[- ]day\b|\bovernight\b|\bwithin\s+hours\b",
    re.IGNORECASE,
)


def _hours(value: str, unit: str) -> float:
    number = float(value.replace(",", "."))
    return number if unit.lower().startswith("h") else number * 24


def promised_hours(statement: str) -> float | None:
    """The fastest turnaround a sentence promises, in hours; None when it states none."""
    candidates: list[float] = []
    for low, _high, unit in _RANGE.findall(statement):
        candidates.append(_hours(low, unit))
    without_ranges = _RANGE.sub(" ", statement)
    for value, unit in _SINGLE.findall(without_ranges):
        candidates.append(_hours(value, unit))
    if _WORDED.search(statement):
        candidates.append(24.0)
    return min(candidates) if candidates else None


def _check_returns(draft: dict[str, Any], markdown: str) -> list[Violation]:
    found: list[Violation] = []
    turnaround = draft.get("returns_turnaround_hours")
    if turnaround is not None and turnaround < RETURNS_FLOOR_HOURS:
        found.append(
            Violation(
                rule_id="returns-min-48h",
                message=f"The section promises a returns turnaround of {turnaround} hours, "
                f"under the {RETURNS_FLOOR_HOURS}-hour minimum.",
                quote=f"returns_turnaround_hours: {turnaround}",
            )
        )
    for statement in statements(markdown):
        if not _RETURNS_WORD.search(statement):
            continue
        hours = promised_hours(statement)
        if hours is not None and hours < RETURNS_FLOOR_HOURS:
            found.append(
                Violation(
                    rule_id="returns-min-48h",
                    message=f"A returns promise under {RETURNS_FLOOR_HOURS} hours "
                    f"({hours:g} hours).",
                    quote=statement,
                )
            )
    return found


# --- volume-discount-table ----------------------------------------------------------------


def _check_discount_table(draft: dict[str, Any], markdown: str) -> list[Violation]:
    tiers = draft.get("discount_tiers") or []
    if len(tiers) >= 2 and DISCOUNT_TABLE_HEADING in markdown:
        return []
    return [
        Violation(
            rule_id="volume-discount-table",
            message="The section has no volume-based discount tier table.",
        )
    ]


# --- no-carrier-rates ---------------------------------------------------------------------

# Acronyms are matched case-sensitively: "set-ups" is not UPS.
_CARRIER: Final = re.compile(
    r"\b(?:UPS|DHL|MRW|SEUR)\b|\b[Ff]ed[Ee]x\b|\b(?i:local\s+(?:zaragoza\s+)?carriers?)\b"
)
_MONEY: Final = re.compile(
    r"\d(?:[\d.,]*\d)?\s*(?:USD|EUR|dollars?|euros?)(?![A-Za-z])"
    r"|\d(?:[\d.,]*\d)?\s*[€$]|[€$]\s*\d",
    re.IGNORECASE,
)
_RATE_TALK: Final = re.compile(
    r"negotiat\w*|rebate\w*|wholesale|contract(?:ed)?\s+rates?|our\s+(?:cost|rate)s?"
    r"|we\s+pay|pays?\s+us|commission",
    re.IGNORECASE,
)
_DISCOUNT_PCT: Final = re.compile(
    r"\d+(?:[.,]\d+)?\s?%\s*(?:discount|off|rebate|below|cheaper)"
    r"|(?:discount|rebate)\w*\s+(?:of\s+)?\d+(?:[.,]\d+)?\s?%",
    re.IGNORECASE,
)


def discloses_carrier_rate(statement: str) -> bool:
    if not _CARRIER.search(statement):
        return False
    return bool(
        _MONEY.search(statement) or _RATE_TALK.search(statement) or _DISCOUNT_PCT.search(statement)
    )


def _check_carrier_rates(draft: dict[str, Any], markdown: str) -> list[Violation]:
    found: list[Violation] = []
    for line in draft.get("price_lines") or []:
        if _CARRIER.search(f"{line.get('item_id', '')} {line.get('note', '')}"):
            found.append(
                Violation(
                    rule_id="no-carrier-rates",
                    message=f"The price of {line.get('item_id')} names a carrier.",
                    quote=squash(str(line.get("note") or line.get("item_id"))),
                )
            )
    for statement in statements(markdown):
        if discloses_carrier_rate(statement):
            found.append(
                Violation(
                    rule_id="no-carrier-rates",
                    message="A carrier appears next to a rate or what TrackFlow pays it.",
                    quote=statement,
                )
            )
    return found


# --- All rules ----------------------------------------------------------------------------


def check_section(
    draft: dict[str, Any],
    markdown: str,
    *,
    currency: str | None,
    is_sla_section: bool,
) -> tuple[list[Violation], list[RuleId]]:
    """Every applicable rule checked on one rendered section: (violations, checked rule ids)."""
    checks = {
        "currency-by-country": lambda: _check_currency(draft, markdown, currency),
        "on-time-sla-stated": lambda: _check_sla(draft, markdown),
        "returns-min-48h": lambda: _check_returns(draft, markdown),
        "volume-discount-table": lambda: _check_discount_table(draft, markdown),
        "no-carrier-rates": lambda: _check_carrier_rates(draft, markdown),
    }
    checked = applicable_rule_ids(is_sla_section=is_sla_section)
    violations: list[Violation] = []
    for rule_id in checked:
        violations.extend(checks[rule_id]())
    return violations, checked
