"""Cross-department conflicts and their fixed arbiters (briefing §7). No model involved.

Briefing §7: "Arbitration must be a dedicated graph node driven by detectable
contradictions in structured state, not agents negotiating among themselves."
The three triggers below are the briefing's table, verbatim (a test parses
§7 so the two cannot drift). For each one:

- `detect` finds the contradiction in the sections' structured drafts (the
  `facts` Part 2 hands over) and in their text, using the same §5 checks the
  compliance evaluator uses;
- `rule_on` applies the briefing's resolution rule deterministically, in the
  name of the briefing's arbiter (decided with the developer, 2026-11-23: the
  rule is applied automatically and recorded under the named arbiter; the
  arbiter is not asked through an interrupt).

A ruling either sends the offending sections back (`request_changes`, with a
fixed instruction for their generator and, for volume, the capped volume) or,
when the conflict cannot be resolved (unknown country) or is still there at
the iteration limit, rejects them. A rejected section ends the proposal.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from data.pipelines.rfp_intake.briefing import (
    COMMERCIAL_DIRECTOR,
    COMMERCIAL_DIRECTOR_TITLE,
    COUNTRY_NAMES,
    DEPARTMENT_BY_ID,
    DEPARTMENT_IDS,
    DepartmentId,
)
from data.pipelines.rfp_intake.proposal import guidelines, rate_card
from data.pipelines.rfp_intake.text import first_line

TriggerId = Literal["volume-vs-capacity", "returns-sla-breach", "currency-mismatch"]


@dataclass(frozen=True)
class ConflictTrigger:
    trigger_id: TriggerId
    fires_when: str
    arbiter: str
    resolution: str


# Briefing §7, verbatim and in order.
CONFLICT_TRIGGERS: Final[tuple[ConflictTrigger, ...]] = (
    ConflictTrigger(
        trigger_id="volume-vs-capacity",
        fires_when=(
            "`warehouse` committed capacity (pallets/SKU or onboarding throughput) cannot "
            "support the monthly volume assumed by `lastmile`"
        ),
        arbiter="Miguel Torres (Commercial Director)",
        resolution=(
            "Cap the proposal volume to warehouse capacity; `lastmile` must revise quoted "
            "volume/cost downward"
        ),
    ),
    ConflictTrigger(
        trigger_id="returns-sla-breach",
        fires_when=(
            "`reverse` (or any section) promises returns turnaround under 48 hours (violates §5)"
        ),
        arbiter=(
            "Sofía Ramos rejects her section; if another dept still embeds that promise, "
            "Miguel Torres"
        ),
        resolution=(
            "Force `request_changes` on every section stating under-48h returns; no final "
            "doc until compliant"
        ),
    ),
    ConflictTrigger(
        trigger_id="currency-mismatch",
        fires_when=(
            "Two active sections quote different currencies, or currency ≠ `client_country` "
            "mapping (US→USD, Spain→EUR)"
        ),
        arbiter="Miguel Torres",
        resolution=(
            "Rewrite offending sections to the country currency; reject if unresolved after "
            "iteration limit"
        ),
    ),
)
TRIGGER_BY_ID: Final[dict[str, ConflictTrigger]] = {t.trigger_id: t for t in CONFLICT_TRIGGERS}

REVERSE_OWNER: Final = DEPARTMENT_BY_ID["reverse"].owner


class SectionView(TypedDict):
    """What arbitration reads of one active section."""

    department_id: str
    draft: dict[str, Any]
    markdown: str
    approval_status: str | None
    review_rounds: int


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Conflict(_Model):
    trigger_id: TriggerId
    # The offending sections: the ones the rule acts on.
    departments: list[DepartmentId]
    evidence: list[str]
    # False when no rewrite can fix it (the client's country is unknown).
    resolvable: bool = True
    volume_cap: int | None = None


class Ruling(_Model):
    trigger_id: TriggerId
    arbiter: str
    arbiter_basis: str
    # The briefing's resolution rule, verbatim.
    rule: str
    round: int
    departments: list[DepartmentId]
    action: Literal["request_changes", "reject"]
    # What the offending sections' generators are told (or why they are rejected).
    instruction: str
    volume_cap: int | None = None
    evidence: list[str]
    ruled_at: datetime


def _in_order(departments: set[str]) -> list[DepartmentId]:
    return [dept for dept in DEPARTMENT_IDS if dept in departments]


def _quotes(violations: list[Any]) -> list[str]:
    return [first_line(v.quote or v.message, 160) for v in violations]


def _volume_vs_capacity(by_dept: dict[str, SectionView], country: str | None) -> Conflict | None:
    warehouse, lastmile = by_dept.get("warehouse"), by_dept.get("lastmile")
    if warehouse is None or lastmile is None:
        return None
    assumed = lastmile["draft"].get("assumed_monthly_volume")
    committed = (warehouse["draft"].get("committed_capacity") or {}).get("monthly_orders")
    source = "the warehouse section commits"
    if committed is None:
        site = rate_card.SITES.get(country or "")
        committed = site.monthly_orders if site else None
        source = "the warehouse site handles (rate card)"
    if assumed is None or committed is None or assumed <= committed:
        return None
    return Conflict(
        trigger_id="volume-vs-capacity",
        departments=["lastmile"],
        evidence=[
            f"Last mile assumes {assumed:,} orders a month; {source} {committed:,} orders a month."
        ],
        volume_cap=int(committed),
    )


def _returns_sla_breach(sections: list[SectionView]) -> Conflict | None:
    offending: dict[str, list[str]] = {}
    for section in sections:
        found = guidelines.returns_violations(section["draft"], section["markdown"])
        if found:
            offending[section["department_id"]] = _quotes(found)
    if not offending:
        return None
    return Conflict(
        trigger_id="returns-sla-breach",
        departments=_in_order(set(offending)),
        evidence=[f"{dept}: {quote}" for dept, quotes in offending.items() for quote in quotes],
    )


def _currency_mismatch(sections: list[SectionView], currency: str | None) -> Conflict | None:
    if currency is None:
        return Conflict(
            trigger_id="currency-mismatch",
            departments=_in_order({section["department_id"] for section in sections}),
            evidence=[
                "The client's country is unknown, so no proposal currency can be set "
                "(US→USD, Spain→EUR). Sales must ask the client and run the intake again."
            ],
            resolvable=False,
        )
    offending: dict[str, list[str]] = {}
    quoted: set[str] = set()
    for section in sections:
        quoted |= {str(line.get("currency")) for line in section["draft"].get("price_lines") or []}
        found = guidelines.currency_violations(section["draft"], section["markdown"], currency)
        if found:
            offending[section["department_id"]] = _quotes(found)
    if not offending:
        return None
    evidence = [f"{dept}: {quote}" for dept, quotes in offending.items() for quote in quotes]
    if len(quoted) > 1:
        evidence.insert(0, f"The sections quote {', '.join(sorted(quoted))}; expected {currency}.")
    return Conflict(
        trigger_id="currency-mismatch",
        departments=_in_order(set(offending)),
        evidence=evidence,
    )


def detect(
    sections: list[SectionView], *, currency: str | None, country: str | None
) -> list[Conflict]:
    """Every §7 contradiction in the active sections, in the briefing's trigger order."""
    by_dept = {section["department_id"]: section for section in sections}
    found = [
        _volume_vs_capacity(by_dept, country),
        _returns_sla_breach(sections),
        _currency_mismatch(sections, currency),
    ]
    return [conflict for conflict in found if conflict is not None]


def _instruction(
    conflict: Conflict, arbiter: str, currency: str | None, country: str | None
) -> str:
    prefix = f"Arbitration ({conflict.trigger_id}, ruled by {arbiter}): "
    if conflict.trigger_id == "volume-vs-capacity":
        cap = conflict.volume_cap or 0
        return prefix + (
            f"the warehouse can support at most {cap:,} orders a month. Cap the proposal "
            f"volume at {cap:,} orders a month: plan the deliveries for that volume and revise "
            "the quoted volume and cost downward to match."
        )
    if conflict.trigger_id == "returns-sla-breach":
        return prefix + (
            "no proposal may promise returns processing in under 48 hours (§5). Remove every "
            "promise under 48 hours and use the rate card's turnaround of "
            f"{rate_card.RETURNS_TURNAROUND_HOURS} hours."
        )
    other = "EUR" if currency == "USD" else "USD"
    return prefix + (
        f"quote every price and amount in {currency} only, the currency of "
        f"{COUNTRY_NAMES.get(country or '', 'the client')} operations, and remove any "
        f"mention of {other}."
    )


def _arbiters(conflict: Conflict) -> list[tuple[str, str, list[DepartmentId]]]:
    """(arbiter, basis, departments) per the briefing's arbiter column."""
    commercial = (COMMERCIAL_DIRECTOR, COMMERCIAL_DIRECTOR_TITLE)
    if conflict.trigger_id != "returns-sla-breach":
        return [(*commercial, list(conflict.departments))]
    groups: list[tuple[str, str, list[DepartmentId]]] = []
    if "reverse" in conflict.departments:
        groups.append((REVERSE_OWNER, "owner of Reverse Logistics", ["reverse"]))
    others: list[DepartmentId] = [dept for dept in conflict.departments if dept != "reverse"]
    if others:
        groups.append((*commercial, others))
    return groups


def rule_on(
    conflicts: list[Conflict],
    *,
    rounds_used: int,
    max_rounds: int,
    revisions_left: dict[str, int],
    currency: str | None,
    country: str | None,
    now: datetime,
) -> list[Ruling]:
    """The briefing's resolution for each conflict, as rulings in the arbiter's name.

    `request_changes` while the conflict can be fixed and the limits allow it;
    `reject` when it cannot be fixed, the arbitration rounds are used up, or an
    offending section has no revision left (briefing: "reject if unresolved
    after iteration limit"; "no final doc until compliant").
    """
    rulings: list[Ruling] = []
    for conflict in conflicts:
        trigger = TRIGGER_BY_ID[conflict.trigger_id]
        for arbiter, basis, departments in _arbiters(conflict):
            exhausted = [dept for dept in departments if revisions_left.get(dept, 0) <= 0]
            reject_reason = None
            if not conflict.resolvable:
                reject_reason = "it cannot be resolved by rewriting a section"
            elif rounds_used >= max_rounds:
                reject_reason = f"it is still unresolved after {rounds_used} arbitration round(s)"
            elif exhausted:
                reject_reason = f"{', '.join(exhausted)} has used every revision allowed"
            if reject_reason:
                action: Literal["request_changes", "reject"] = "reject"
                instruction = (
                    f"Arbitration ({conflict.trigger_id}, ruled by {arbiter}): rejected because "
                    f"{reject_reason}. " + " ".join(conflict.evidence)
                )
            else:
                action = "request_changes"
                instruction = _instruction(conflict, arbiter, currency, country)
            rulings.append(
                Ruling(
                    trigger_id=conflict.trigger_id,
                    arbiter=arbiter,
                    arbiter_basis=basis,
                    rule=trigger.resolution,
                    round=rounds_used + 1,
                    departments=departments,
                    action=action,
                    instruction=instruction,
                    volume_cap=conflict.volume_cap,
                    evidence=list(conflict.evidence),
                    ruled_at=now,
                )
            )
    return rulings
