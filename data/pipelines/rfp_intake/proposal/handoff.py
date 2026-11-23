"""The hand-off to Part 3 (approval): `rfp-proposal/1` in `rfp_proposal_handoffs`.

The ticket's acceptance criterion: "The handoff to Part 3 must include, for
every department, both the generated content and a structured evaluation
result. Sections that exhausted the iteration limit ship with
`needs_human_review` — they are not dropped."

`ProposalHandoff` carries, per department, the department-specific "for
approval" ticket of the visual reference:
- the owner who approves it (briefing §2.1) and `approval_status: pending`;
- a one-line summary of what was assigned;
- the Markdown draft and its `EvaluationResult`;
- the structured facts Part 3's conflict triggers read (§7: currency, SLA %,
  returns turnaround, committed capacity and assumed volume).

Part 3 takes work with `claim_next`. A ticket arrives in
`waiting_for_approval` (every section passed) or in `needs_human_review` (at
least one section did not); both are Part 3's to handle. The contract and
its JSON Schema are in `docs/rfp/proposal-handoff-contract.md` (kept in sync
by a test).
"""

from datetime import date, datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.pipelines.rfp_intake.briefing import (
    DEPARTMENT_BY_ID,
    DEPARTMENT_IDS,
    Currency,
    DepartmentId,
    Service,
)
from data.pipelines.rfp_intake.handoff import HandoffClient, IntakeHandoff
from data.pipelines.rfp_intake.models import RfpDepartmentSection, RfpProposalHandoff, utcnow
from data.pipelines.rfp_intake.proposal import rate_card
from data.pipelines.rfp_intake.proposal.results import EvaluationResult
from data.pipelines.rfp_intake.store import as_utc, as_uuid

CONTRACT_VERSION: Final = "rfp-proposal/1"
NEXT_STAGE: Final = "approval"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)


class FactPrice(_Contract):
    item_id: str
    label: str
    amount: float
    currency: str
    unit: str
    source: Literal["knowledge_base", "commercial_indicative"]


class FactTier(_Contract):
    min_monthly_volume: int
    max_monthly_volume: int | None
    discount_pct: float


class FactCapacity(_Contract):
    pallet_positions: int | None
    monthly_orders: int | None


class FactCarriers(_Contract):
    destination: str
    carriers: list[str]


class SectionFacts(_Contract):
    """What the section commits to, as data (for Part 3's §7 conflict triggers)."""

    currencies: list[str] = Field(description="Every currency the section's prices are in")
    price_lines: list[FactPrice]
    discount_tiers: list[FactTier]
    on_time_sla_pct: float | None
    returns_turnaround_hours: int | None
    onboarding_business_days: int | None
    committed_capacity: FactCapacity | None
    assumed_monthly_volume: int | None
    carriers_by_destination: list[FactCarriers]
    open_questions: list[str]
    assumptions: list[str]


class ProposalSection(_Contract):
    """One department's assignment ticket "for approval" (visual reference)."""

    section_id: UUID
    department_id: DepartmentId
    department_name: str
    owner: str = Field(description="Approves this section in Part 3 (briefing §2.1)")
    is_sla_section: bool = Field(description="This section states the proposal's on-time SLA")
    draft_status: Literal["passed", "needs_human_review"]
    needs_human_review: bool
    iterations: int = Field(ge=1)
    max_iterations: int
    approval_status: Literal["pending"] = "pending"
    assignment_summary: str
    draft_content: str
    facts: SectionFacts
    evaluation: EvaluationResult
    knowledge_source: str
    review_note: str | None = None


class ProposalMetrics(_Contract):
    sections: int
    average_iterations_per_section: float = Field(description="Briefing §3 KPI: fewer than 2")
    passed_first_attempt: int
    needs_human_review: int


class ProposalHandoff(_Contract):
    """What Part 2 hands to Part 3 (approval by each department owner)."""

    contract_version: Literal["rfp-proposal/1"] = CONTRACT_VERSION
    next_stage: Literal["approval"] = NEXT_STAGE
    ticket_id: UUID
    rfp_id: UUID
    status: Literal["waiting_for_approval", "needs_human_review"]
    client: HandoffClient
    currency: Currency | None
    services_requested: list[Service]
    monthly_volume: int | None
    deadline: date | None
    deadline_text: str | None
    sla_department: DepartmentId
    rate_card_version: str
    max_iterations: int
    sections: list[ProposalSection] = Field(min_length=1)
    metrics: ProposalMetrics
    client_questions: list[str]
    unassigned_requirements: list[str]
    generated_at: datetime

    @model_validator(mode="after")
    def one_section_per_department_in_order(self) -> "ProposalHandoff":
        ids = [section.department_id for section in self.sections]
        if ids != [dept for dept in DEPARTMENT_IDS if dept in ids]:
            raise ValueError("sections must be unique and in the briefing's department order")
        return self


def _pct(value: float) -> str:
    return f"{int(value)}%" if float(value).is_integer() else f"{value}%"


def assignment_summary(section: ProposalSection) -> str:
    """One line for the approval ticket: what was drafted and how it went."""
    facts = section.facts
    parts = [
        f"{len(facts.price_lines)} price(s) in {', '.join(facts.currencies) or 'no currency'}",
        f"{len(facts.discount_tiers)} discount tier(s)",
    ]
    if facts.onboarding_business_days is not None:
        parts.append(f"onboarding {facts.onboarding_business_days} business days")
    if facts.returns_turnaround_hours is not None:
        parts.append(f"returns turnaround {facts.returns_turnaround_hours} hours")
    if section.is_sla_section and facts.on_time_sla_pct is not None:
        parts.append(f"states the {_pct(facts.on_time_sla_pct)} on-time SLA")
    parts.append(f"{len(facts.open_questions)} pending question(s)")
    if section.draft_status == "passed":
        outcome = f"passed every check on attempt {section.iterations} of {section.max_iterations}"
    else:
        failed = []
        evaluation = section.evaluation
        if not evaluation.readability.passed:
            failed.append("readability")
        if not evaluation.relevance.passed:
            failed.append("relevance")
        if not evaluation.compliance.passed:
            failed.append("compliance: " + ", ".join(evaluation.compliance.rule_ids))
        outcome = (
            f"needs human review after {section.iterations} of {section.max_iterations} "
            f"attempts ({'; '.join(failed) or 'see the note'})"
        )
    return "; ".join(parts) + f". {outcome[0].upper()}{outcome[1:]}."


def section_facts(draft: dict[str, Any], currency_hint: str | None = None) -> SectionFacts:
    lines = []
    for line in draft.get("price_lines") or []:
        price = rate_card.PRICE_BY_ID.get(line["item_id"])
        lines.append(
            FactPrice(
                item_id=line["item_id"],
                label=price.label if price else line["item_id"],
                amount=float(line["amount"]),
                currency=line["currency"],
                unit=price.unit if price else "",
                source=price.source if price else "commercial_indicative",
            )
        )
    capacity = draft.get("committed_capacity")
    return SectionFacts(
        currencies=sorted({line.currency for line in lines}),
        price_lines=lines,
        discount_tiers=[FactTier(**tier) for tier in draft.get("discount_tiers") or []],
        on_time_sla_pct=draft.get("on_time_sla_pct"),
        returns_turnaround_hours=draft.get("returns_turnaround_hours"),
        onboarding_business_days=draft.get("onboarding_business_days"),
        committed_capacity=FactCapacity(**capacity) if capacity else None,
        assumed_monthly_volume=draft.get("assumed_monthly_volume"),
        carriers_by_destination=[
            FactCarriers(destination=row["destination"], carriers=list(row["carriers"]))
            for row in draft.get("carriers_by_destination") or []
        ],
        open_questions=list(draft.get("open_questions") or []),
        assumptions=list(draft.get("assumptions") or []),
    )


def build_proposal(
    intake: IntakeHandoff,
    sections: list[RfpDepartmentSection],
    *,
    sla_department: str,
    max_iterations: int,
) -> ProposalHandoff:
    """The Part 3 payload, from the stored sections (what is persisted is what ships)."""
    wanted = {dept.department_id for dept in intake.departments}
    built: list[ProposalSection] = []
    for row in sections:
        if row.department_id not in wanted:
            continue
        data = row.draft_data or {}
        evaluation = EvaluationResult.model_validate(row.evaluation_results)
        section = ProposalSection(
            section_id=row.id,
            department_id=row.department_id,  # type: ignore[arg-type]
            department_name=DEPARTMENT_BY_ID[row.department_id].name,
            owner=row.owner,
            is_sla_section=bool(data.get("is_sla_section")),
            draft_status=row.draft_status,  # type: ignore[arg-type]
            needs_human_review=row.draft_status == "needs_human_review",
            iterations=row.draft_iterations,
            max_iterations=max_iterations,
            assignment_summary="",
            draft_content=row.draft_content or "",
            facts=section_facts(data.get("draft") or {}),
            evaluation=evaluation,
            knowledge_source=str(data.get("knowledge_source") or "none"),
            review_note=data.get("review_note"),
        )
        section.assignment_summary = assignment_summary(section)
        built.append(section)
    if {section.department_id for section in built} != wanted:
        raise ValueError("every department of the intake handoff must have a finished section")
    count = len(built)
    needs_review = sum(1 for section in built if section.needs_human_review)
    return ProposalHandoff(
        ticket_id=intake.ticket_id,
        rfp_id=intake.rfp_id,
        status="needs_human_review" if needs_review else "waiting_for_approval",
        client=intake.client,
        currency=intake.client.currency,
        services_requested=list(intake.services_requested),
        monthly_volume=intake.monthly_volume,
        deadline=intake.deadline,
        deadline_text=intake.deadline_text,
        sla_department=sla_department,  # type: ignore[arg-type]
        rate_card_version=rate_card.RATE_CARD_VERSION,
        max_iterations=max_iterations,
        sections=built,
        metrics=ProposalMetrics(
            sections=count,
            average_iterations_per_section=round(
                sum(section.iterations for section in built) / count, 2
            ),
            passed_first_attempt=sum(
                1
                for section in built
                if section.draft_status == "passed" and section.iterations == 1
            ),
            needs_human_review=needs_review,
        ),
        client_questions=list(intake.sales_brief.client_questions),
        unassigned_requirements=list(intake.sales_brief.unassigned_requirements),
        generated_at=utcnow(),
    )


def json_schema() -> dict[str, Any]:
    return ProposalHandoff.model_json_schema(by_alias=True)


def load_proposal_handoff(engine: Engine, ticket_id: UUID | str) -> ProposalHandoff | None:
    with Session(engine) as session:
        row = session.get(RfpProposalHandoff, as_uuid(ticket_id))
        return ProposalHandoff.model_validate(row.payload) if row else None


class ClaimedProposal(BaseModel):
    proposal: ProposalHandoff
    claimed_at: datetime
    claimed_by: str


def claim_next(engine: Engine, *, claimed_by: str) -> ClaimedProposal | None:
    """For Part 3: the oldest pending proposal, claimed (SKIP LOCKED on PostgreSQL).

    It leaves the ticket's status to Part 3, which owns `waiting_for_approval`.
    """
    with Session(engine) as session:
        row = session.execute(
            select(RfpProposalHandoff)
            .where(
                col(RfpProposalHandoff.status) == "pending",
                col(RfpProposalHandoff.next_stage) == NEXT_STAGE,
            )
            .order_by(col(RfpProposalHandoff.created_at))
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None:
            return None
        now = utcnow()
        row.status = "claimed"
        row.claimed_at = now
        row.claimed_by = claimed_by[:100]
        proposal = ProposalHandoff.model_validate(row.payload)
        session.commit()
    return ClaimedProposal(proposal=proposal, claimed_at=as_utc(now) or now, claimed_by=claimed_by)
