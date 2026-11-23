"""The assignment orchestrator: one `SectionAssignment` per department of the handoff.

In the visual reference this is the "assignment orchestrator" between the
workstream structure (Part 1's handoff) and the department-scoped
generation. It is deterministic on purpose: Part 1 already decided which
departments the scope needs, and asking a model again could only add drift.

An assignment is everything a department's generator and evaluators see.
It comes from the `IntakeHandoff` alone, never from the PDF or its Markdown:
the shared client facts, the Part 1 summary, this department's own key
aspects, needs, ask and open questions, the rate card in the client's
currency, the guidelines that apply, and whether it carries the SLA.
"""

from typing import Any, TypedDict
from uuid import UUID

from data.pipelines.rfp_intake.handoff import HandoffDepartment, IntakeHandoff
from data.pipelines.rfp_intake.proposal import guidelines, rate_card, sections


class SectionAssignment(TypedDict):
    section_id: str
    department_id: str
    department_name: str
    owner: str
    client_name: str | None
    country: str | None
    currency: str | None
    services_requested: list[str]
    monthly_volume: int | None
    deadline: str | None
    deadline_text: str | None
    budget_range: str | None
    summary: str
    objective: str
    ask: str
    key_aspects: list[dict[str, str]]
    needed_from_department: list[str]
    open_questions: list[str]
    is_sla_section: bool
    sla_department: str
    required_elements: list[str]
    applicable_rule_ids: list[str]
    rate_card_version: str
    rate_card: str
    kb_queries: list[str]
    kb_documents: list[str]
    max_iterations: int


def build_assignment(
    handoff: IntakeHandoff,
    department: HandoffDepartment,
    *,
    section_id: UUID | str,
    sla_department: str,
    max_iterations: int,
) -> SectionAssignment:
    dept_id = department.department_id
    is_sla = dept_id == sla_department
    currency = handoff.client.currency
    country = handoff.client.country
    return SectionAssignment(
        section_id=str(section_id),
        department_id=dept_id,
        department_name=department.department_name,
        owner=department.owner,
        client_name=handoff.client.name,
        country=country,
        currency=currency,
        services_requested=list(handoff.services_requested),
        monthly_volume=handoff.monthly_volume,
        deadline=handoff.deadline.isoformat() if handoff.deadline else None,
        deadline_text=handoff.deadline_text,
        budget_range=handoff.budget_range,
        summary=handoff.sales_brief.summary,
        objective=department.objective,
        ask=department.ask,
        key_aspects=[aspect.model_dump() for aspect in department.key_aspects],
        needed_from_department=list(department.needed_from_department),
        open_questions=list(department.open_questions),
        is_sla_section=is_sla,
        sla_department=sla_department,
        required_elements=[element.label for element in sections.REQUIRED_ELEMENTS[dept_id]],
        applicable_rule_ids=list(guidelines.applicable_rule_ids(is_sla_section=is_sla)),
        rate_card_version=rate_card.RATE_CARD_VERSION,
        rate_card=rate_card.render_text(dept_id, currency, country),
        kb_queries=sections.kb_queries(dept_id, is_sla_section=is_sla),
        kb_documents=sections.kb_documents(dept_id, is_sla_section=is_sla),
        max_iterations=max_iterations,
    )


def build_assignments(
    handoff: IntakeHandoff, section_ids: dict[str, UUID], *, max_iterations: int
) -> tuple[str, list[SectionAssignment]]:
    """(the SLA department, one assignment per department in the briefing's order)."""
    sla = guidelines.sla_department([dept.department_id for dept in handoff.departments])
    return sla, [
        build_assignment(
            handoff,
            department,
            section_id=section_ids[department.department_id],
            sla_department=sla,
            max_iterations=max_iterations,
        )
        for department in handoff.departments
    ]


def as_dict(assignment: SectionAssignment) -> dict[str, Any]:
    return dict(assignment)
