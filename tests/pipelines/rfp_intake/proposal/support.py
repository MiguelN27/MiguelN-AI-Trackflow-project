"""Shared fixtures for the Part 2 tests: real handoffs, canned drafts, scripted agents.

No test reaches a model, Qdrant or PostgreSQL. A ticket reaches
`intake_complete` through Part 1's own graph with its scripted agents, so
Part 2 starts from a real `rfp-intake/1` handoff. The scripted Part 2 agents
send canned drafts through the *real* guard (`generators.check`), the real
relevance finalizer and the real compliance rules.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.briefing import Currency
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.llm import NO_USAGE, Usage
from data.pipelines.rfp_intake.proposal import knowledge, rate_card, sections
from data.pipelines.rfp_intake.proposal.agents import ProposalAgents, compliance, generators
from data.pipelines.rfp_intake.proposal.agents import relevance as relevance_agent
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket

USAGE = Usage(prompt_tokens=200, completion_tokens=50)


def settings(**overrides: Any) -> RfpIntakeSettings:
    values: dict[str, Any] = {"compliance_reviewer": False, **overrides}
    return RfpIntakeSettings(_env_file=None, **values)  # type: ignore[call-arg]


def intake_complete(engine: Engine, tmp_path: Path, kind: str = "informal") -> UUID:
    """A ticket through Part 1 (scripted), so its handoff is queued for drafting."""
    ticket_id = new_ticket(engine, tmp_path / "pdf", kind)
    status = runner.run_intake(ticket_id, engine, agents=ScriptedAgents(kind).bundle())
    assert status == "intake_complete"
    return ticket_id


# --- Canned drafts (plain, readable prose; no figure outside the sources) -----------------


def price_lines(dept: str, currency: Currency) -> list[sections.PriceLine]:
    return [
        sections.PriceLine(
            item_id=price.item_id,
            amount=float(price.amount(currency)),
            currency=currency,
        )
        for price in rate_card.prices_for(dept)
    ]


def tiers(dept: str) -> list[sections.TierRow]:
    return [
        sections.TierRow(
            min_monthly_volume=tier.min_volume,
            max_monthly_volume=tier.max_volume,
            discount_pct=tier.discount_pct,
        )
        for tier in rate_card.DISCOUNT_TIERS[dept]
    ]


WAREHOUSE_NARRATIVE = [
    "We receive your stock, check it and put it on our shelves. Each product gets its own place "
    "in our system, so we can find it fast. When an order comes in, our team picks it, packs it "
    "and hands it to the carrier.",
    "You pay for storage by the space your stock uses, not by the unit. New products get a free "
    "first month. We can send you a report on slow stock when you ask, so you can act before "
    "fees grow.",
    "We can start with you soon. Our onboarding plan covers your product data, a test order and "
    "the first shipments. We will agree the start date with you once we know your volumes.",
]

LASTMILE_NARRATIVE = [
    "Our system picks the best carrier for each parcel. It looks at the address, the weight and "
    "how fast the parcel must arrive. You do not need your own deals with carriers.",
    "Most parcels travel by standard delivery. Express delivery is there for orders that must "
    "arrive fast, inside the express area. Your customers can track every parcel.",
    "We plan our routes and our team around your real order flow. When your sales peak, we tell "
    "you early what to expect, so your customers are never surprised.",
]

REVERSE_NARRATIVE = [
    "Your customers ask for a return through your own portal. We create the return label from "
    "the rules you set, such as a wrong size or a faulty product.",
    "When a return reaches our warehouse, a member of our team checks it. We sort it as fit for "
    "resale, fit for repair or not fit. Items fit for resale go back on the shelf.",
    "You decide the rules, and we follow them. We never resell products on our own account. "
    "Every month we can show you which products come back most and why.",
]


def warehouse_draft(currency: Currency, country: str, *, sla: bool) -> sections.WarehouseDraft:
    site = rate_card.SITES[country]
    return sections.WarehouseDraft(
        summary=f"TrackFlow will store and ship your products from our {site.city} warehouse. "
        "We charge for the space your stock uses. You get one contact for every question.",
        narrative=WAREHOUSE_NARRATIVE,
        price_lines=price_lines("warehouse", currency),
        discount_tiers=tiers("warehouse"),
        on_time_sla_pct=90 if sla else None,
        assumptions=["Prices are per unit of service, so the total depends on your volume."],
        open_questions=["How much space will your stock use on average each month?"],
        storage_site=site.city,
        committed_capacity=sections.Capacity(
            pallet_positions=site.pallet_positions, monthly_orders=site.monthly_orders
        ),
        onboarding_business_days=rate_card.ONBOARDING_BUSINESS_DAYS,
    )


def lastmile_draft(
    currency: Currency, country: str, *, volume: int | None
) -> sections.LastmileDraft:
    site = rate_card.SITES[country]
    return sections.LastmileDraft(
        summary="TrackFlow will deliver your orders to your customers across the country. We "
        "choose the carrier for each parcel, so you do not have to.",
        narrative=LASTMILE_NARRATIVE,
        price_lines=price_lines("lastmile", currency),
        discount_tiers=tiers("lastmile"),
        on_time_sla_pct=90,
        assumptions=["Prices are per parcel and depend on its weight and speed."],
        open_questions=["Which regions receive most of your orders?"],
        delivery_options=[
            sections.DeliveryOption(
                service="standard", transit_time="3 to 5 business days within the same country"
            ),
            sections.DeliveryOption(
                service="express",
                transit_time="24 to 48 hours within the same country",
                coverage=site.express_coverage,
            ),
        ],
        carriers_by_destination=[
            sections.CarrierCoverage(
                destination="Within the country", carriers=list(site.carriers[:2])
            ),
        ],
        assumed_monthly_volume=volume,
    )


def reverse_draft(currency: Currency, *, sla: bool = False) -> sections.ReverseDraft:
    return sections.ReverseDraft(
        summary="TrackFlow will handle your returns from the label to the shelf. We follow the "
        "rules you set for each product.",
        narrative=REVERSE_NARRATIVE,
        price_lines=price_lines("reverse", currency),
        discount_tiers=tiers("reverse"),
        on_time_sla_pct=90 if sla else None,
        assumptions=["Prices are per return or per unit, as each line says."],
        open_questions=["How many returns a month should we plan for?"],
        returns_turnaround_hours=rate_card.RETURNS_TURNAROUND_HOURS,
        return_window_days=rate_card.RETURN_WINDOW_DAYS,
        return_shipping_paid_by="The client brand pays return shipping, unless TrackFlow caused "
        "the return",
        international_returns="Handled by hand by Sofía Ramos's team",
    )


# The checklist's compliance failure: a promise the guidelines forbid (§5).
BAD_RETURNS_SENTENCE = "Returns are processed within 24 hours of arrival."


def bad_reverse_draft(currency: Currency) -> sections.ReverseDraft:
    draft = reverse_draft(currency)
    draft.returns_turnaround_hours = 24
    draft.narrative = [*draft.narrative, BAD_RETURNS_SENTENCE]
    return draft


Draft = Callable[[dict[str, Any]], sections.SectionDraft]


def good(dept: str) -> Draft:
    """A compliant draft for whichever client the assignment is for."""

    def draft(assignment: dict[str, Any]) -> sections.SectionDraft:
        currency, country = assignment["currency"], assignment["country"]
        if dept == "warehouse":
            return warehouse_draft(currency, country, sla=assignment["is_sla_section"])
        if dept == "lastmile":
            return lastmile_draft(currency, country, volume=assignment["monthly_volume"])
        return reverse_draft(currency, sla=assignment["is_sla_section"])

    return draft


def bad_returns(assignment: dict[str, Any]) -> sections.SectionDraft:
    return bad_reverse_draft(assignment["currency"])


def all_good() -> dict[str, list[Draft]]:
    return {dept: [good(dept)] for dept in ("warehouse", "lastmile", "reverse")}


def assignment_for(ticket_handoff: Any, dept: str, *, max_iterations: int = 3) -> dict[str, Any]:
    from data.pipelines.rfp_intake.proposal.assignments import build_assignments

    section_ids = {
        d.department_id: UUID(int=n) for n, d in enumerate(ticket_handoff.departments, 1)
    }
    _, assignments = build_assignments(ticket_handoff, section_ids, max_iterations=max_iterations)
    return dict(next(item for item in assignments if item["department_id"] == dept))


def kb_for(assignment: dict[str, Any]) -> list[dict[str, Any]]:
    return knowledge.local_passages(list(assignment["kb_documents"]))


# --- Scripted Part 2 agents ---------------------------------------------------------------


@dataclass
class ScriptedProposalAgents:
    """Canned drafts per department, in order (the last one repeats), through the real guards.

    `fail` maps "generate:<dept>#<attempt>" or "relevance:<dept>#<attempt>" to the
    exception that call raises (once).
    """

    drafts: dict[str, list[Draft]] = field(default_factory=all_good)
    fail: dict[str, BaseException] = field(default_factory=dict)
    knowledge_error: bool = False
    calls: dict[str, list[Any]] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

    def _record(self, name: str, received: Any) -> None:
        self.calls.setdefault(name, []).append(received)
        if name in self.fail:
            raise self.fail.pop(name)

    def _generator(self, dept: str) -> Callable[..., AgentReply]:
        def generate(
            assignment: dict[str, Any],
            passages: list[dict[str, Any]],
            previous: dict[str, Any] | None,
        ) -> AgentReply:
            attempt = self.attempts.get(dept, 0) + 1
            self.attempts[dept] = attempt
            self._record(
                f"generate:{dept}#{attempt}",
                {"assignment": assignment, "passages": passages, "previous": previous},
            )
            sequence = self.drafts[dept]
            output = sequence[min(attempt, len(sequence)) - 1](assignment)
            return AgentReply(generators.check(assignment, passages, output), USAGE)

        return generate

    def judge_relevance(
        self, assignment: dict[str, Any], draft: dict[str, Any], markdown: str
    ) -> AgentReply:
        dept = assignment["department_id"]
        self._record(f"relevance:{dept}#{self.attempts.get(dept, 0)}", markdown)
        judgement = relevance_agent.RelevanceJudgement(
            verdicts=[
                relevance_agent.AspectVerdict(
                    aspect_id=item["id"], verdict="covered", evidence_quote=draft["summary"]
                )
                for item in relevance_agent.requirements(assignment)
            ]
        )
        return AgentReply(
            relevance_agent.finalize(assignment, draft, markdown, judgement).as_json(), USAGE
        )

    def review_compliance(
        self, assignment: dict[str, Any], draft: dict[str, Any], markdown: str, use: bool
    ) -> AgentReply:
        return AgentReply(
            compliance.evaluate_rules(assignment, draft, markdown).as_json(), NO_USAGE
        )

    def retrieve_knowledge(
        self, assignment: dict[str, Any], top_k: int
    ) -> tuple[list[dict[str, Any]], str]:
        self._record(f"knowledge:{assignment['department_id']}", top_k)
        if self.knowledge_error:
            return knowledge.retrieve_for(assignment, top_k)
        return kb_for(assignment), "local"

    def bundle(self) -> ProposalAgents:
        return ProposalAgents(
            generators={dept: self._generator(dept) for dept in self.drafts},
            judge_relevance=self.judge_relevance,
            review_compliance=self.review_compliance,
            retrieve_knowledge=self.retrieve_knowledge,
        )


def ticket_status(engine: Engine, ticket_id: UUID) -> str:
    ticket = store.get_ticket(engine, ticket_id)
    assert ticket is not None
    return ticket.status
