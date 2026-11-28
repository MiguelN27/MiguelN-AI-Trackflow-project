"""The Part 2 graphs: `rfp_proposal` and its per-section loop `rfp_proposal_section`.

Outer graph (the visual reference, "departmental mapping & deliverable finalization"):

    START -> plan_assignments -(dispatch_sections: Send per department)-> draft_section xN
          -> synthesize_proposal -> complete_drafting -> END

- `plan_assignments` is the assignment orchestrator. It turns Part 1's
  handoff into one assignment per department. Deterministic, and never the PDF.
- `draft_section` runs one department's generator-evaluator loop (the
  subgraph below). The departments run in parallel. A technical failure
  inside it becomes an outcome, never an exception, so the other sections
  finish.
- `synthesize_proposal` is the final deliverable synthesis. It reads the
  stored sections back and builds one "for approval" entry per department
  (`ProposalHandoff`).
- `complete_drafting` queues it for Part 3 and sets the ticket's final
  Part 2 status in the same transaction.

Section subgraph (the generator-evaluator loop, one per department):

    START -> retrieve_knowledge -> generate_section
          -> check_readability | check_relevance | check_compliance   (in parallel)
          -> merge_evaluation -(route_after_evaluation)-> generate_section  (revise)
                                                        | accept_section    (accept)   -> END
                                                        | escalate_section  (escalate) -> END

Every node runs inside `steps.run_step`, which moves `current_stage`, writes an
`rfp_ticket_events` row with timing and tokens, and fails safely. Loop stages
carry the department and attempt: `generate_section:reverse#2`. The engine,
agents and settings come through the runtime context (`ProposalContext`), so
tests run these same graphs with scripted agents. No checkpointer of its own:
a redraft starts from scratch.

Part 3 reuses the section subgraph to revise a section an owner (or an
arbitration ruling) sent back. It passes `active_statuses = {waiting_for_approval}`
so the loop's writes accept the ticket's Part 3 status, and the assignment
carries a `revision_request` (the draft under review and what to change). Run
inside the checkpointed approval graph, its steps are checkpointed too.
"""

import operator
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Final, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.handoff import IntakeHandoff
from data.pipelines.rfp_intake.models import utcnow
from data.pipelines.rfp_intake.proposal import evaluation, handoff
from data.pipelines.rfp_intake.proposal import store as proposal_store
from data.pipelines.rfp_intake.proposal.agents import ProposalAgents
from data.pipelines.rfp_intake.proposal.assignments import build_assignments
from data.pipelines.rfp_intake.proposal.render import render_section
from data.pipelines.rfp_intake.proposal.results import (
    ComplianceResult,
    ReadabilityResult,
    RelevanceResult,
)
from data.pipelines.rfp_intake.steps import MODEL_RETRY, StageFailed, StepWork, run_step

GRAPH_NAME: Final = "rfp_proposal"
SECTION_GRAPH_NAME: Final = "rfp_proposal_section"

PLAN: Final = "plan_assignments"
DISPATCH: Final = "dispatch_sections"
DRAFT: Final = "draft_section"
SYNTHESIZE: Final = "synthesize_proposal"
COMPLETE: Final = "complete_drafting"

RETRIEVE: Final = "retrieve_knowledge"
GENERATE: Final = "generate_section"
READABILITY: Final = "check_readability"
RELEVANCE: Final = "check_relevance"
COMPLIANCE: Final = "check_compliance"
MERGE: Final = "merge_evaluation"
ROUTE: Final = "route_after_evaluation"
ACCEPT: Final = "accept_section"
ESCALATE: Final = "escalate_section"

# The order a section goes through, for progress displays.
SECTION_STAGES: Final = (RETRIEVE, GENERATE, READABILITY, RELEVANCE, COMPLIANCE, MERGE)
STAGES: Final = (PLAN, *SECTION_STAGES, ACCEPT, ESCALATE, SYNTHESIZE, COMPLETE)


class ProposalState(TypedDict, total=False):
    ticket_id: str
    run_id: str
    handoff: dict[str, Any]
    sla_department: str
    assignments: list[dict[str, Any]]
    sections: Annotated[list[dict[str, Any]], operator.add]
    proposal: dict[str, Any]
    outcome: str


class SectionTask(TypedDict):
    """What one department's loop receives: its own assignment, never the others'."""

    ticket_id: str
    run_id: str
    assignment: dict[str, Any]


class SectionState(TypedDict, total=False):
    ticket_id: str
    run_id: str
    assignment: dict[str, Any]
    knowledge: list[dict[str, Any]]
    knowledge_source: str
    iteration: int
    draft: dict[str, Any]
    markdown: str
    guard_notes: list[str]
    # One key per evaluator: parallel branches must never write the same key.
    readability: dict[str, Any]
    relevance: dict[str, Any]
    compliance: dict[str, Any]
    evaluation: dict[str, Any]
    decision: str
    decision_reason: str
    outcome: str


@dataclass(frozen=True)
class ProposalContext:
    engine: Engine
    agents: ProposalAgents
    settings: RfpIntakeSettings
    # The ticket statuses the loop may write under: Part 2's, or Part 3's
    # `waiting_for_approval` when it revises a section during approval.
    active_statuses: frozenset[str] = store.PART_2_ACTIVE


def _step(context: ProposalContext, ticket_id: str, stage: str, work: StepWork) -> dict[str, Any]:
    return run_step(context.engine, ticket_id, stage, work, statuses=context.active_statuses)


def _previous(state: SectionState, iteration: int) -> dict[str, Any] | None:
    """What the generator revises: its last attempt and the feedback, or the reviewed draft."""
    if iteration > 1:
        return {
            "iteration": iteration - 1,
            "draft": state.get("draft"),
            "feedback": (state.get("evaluation") or {}).get("feedback_for_generator", ""),
        }
    revision = state["assignment"].get("revision_request")
    if revision:
        # Part 3: the first attempt of a revision starts from the draft under review.
        return {
            "iteration": f"under review, round {revision['round']}",
            "draft": revision["draft"],
            "feedback": "Apply the changes requested in review (see CHANGES REQUESTED).",
        }
    return None


def _stage(node: str, state: SectionState, iteration: int | None = None) -> str:
    dept = state["assignment"]["department_id"]
    return f"{node}:{dept}" if iteration is None else f"{node}:{dept}#{iteration}"


# --- Section subgraph: the generator-evaluator loop ---------------------------------------


def retrieve_knowledge(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    assignment = state["assignment"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        passages, source = context.agents.retrieve_knowledge(
            assignment, context.settings.knowledge_top_k
        )
        documents = sorted({str(p.get("source_document")) for p in passages})
        detail = {"source": source, "passages": len(passages), "documents": documents}
        return {"knowledge": passages, "knowledge_source": source}, detail, 0

    return _step(context, state["ticket_id"], _stage(RETRIEVE, state), work)


def generate_section(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    assignment = state["assignment"]
    dept = assignment["department_id"]
    # The retry policy re-runs this node with the same input, so this stays consistent.
    iteration = state.get("iteration", 0) + 1

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        proposal_store.mark_drafting(
            context.engine,
            state["ticket_id"],
            assignment["section_id"],
            statuses=context.active_statuses,
        )
        previous = _previous(state, iteration)
        reply = context.agents.generators[dept](assignment, state.get("knowledge") or [], previous)
        draft = reply.result["draft"]
        notes = list(reply.result.get("guard_notes") or [])
        markdown = render_section(assignment, draft)
        proposal_store.save_draft(
            context.engine,
            state["ticket_id"],
            assignment["section_id"],
            run_id=state["run_id"],
            iteration=iteration,
            department_id=dept,
            draft=draft,
            markdown=markdown,
            guard_notes=notes,
            draft_data={
                "draft": draft,
                "guard_notes": notes,
                "knowledge_source": state.get("knowledge_source", "none"),
                "knowledge_documents": sorted(
                    {str(p.get("source_document")) for p in state.get("knowledge") or []}
                ),
                "is_sla_section": assignment["is_sla_section"],
                "max_iterations": assignment["max_iterations"],
                "rate_card_version": assignment["rate_card_version"],
                "run_id": state["run_id"],
                **(
                    {"revision_round": assignment["revision_request"]["round"]}
                    if assignment.get("revision_request")
                    else {}
                ),
            },
            tokens=reply.usage.total_tokens,
            statuses=context.active_statuses,
        )
        detail = {
            "department_id": dept,
            "iteration": iteration,
            "price_lines": len(draft.get("price_lines") or []),
            "guard_notes": notes,
            "revision": previous is not None,
        }
        update = {
            "iteration": iteration,
            "draft": draft,
            "markdown": markdown,
            "guard_notes": notes,
        }
        return update, detail, reply.usage.total_tokens

    return _step(context, state["ticket_id"], _stage(GENERATE, state, iteration), work)


def check_readability(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        result = evaluation.evaluate_readability(
            state["draft"],
            min_ease=context.settings.readability_min_ease,
            max_grade=context.settings.readability_max_grade,
        )
        detail = {
            "pass": result.passed,
            "score": result.score,
            "grade": result.details.flesch_kincaid_grade,
            "words": result.details.word_count,
        }
        return {"readability": result.as_json()}, detail, 0

    stage = _stage(READABILITY, state, state["iteration"])
    return _step(context, state["ticket_id"], stage, work)


def check_relevance(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.judge_relevance(
            state["assignment"], state["draft"], state["markdown"]
        )
        result = RelevanceResult.model_validate(reply.result)
        detail = {"pass": result.passed, "missing_aspects": result.missing_aspects}
        return {"relevance": result.as_json()}, detail, reply.usage.total_tokens

    stage = _stage(RELEVANCE, state, state["iteration"])
    return _step(context, state["ticket_id"], stage, work)


def check_compliance(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.review_compliance(
            state["assignment"],
            state["draft"],
            state["markdown"],
            context.settings.compliance_reviewer,
        )
        result = ComplianceResult.model_validate(reply.result)
        detail = {"pass": result.passed, "rule_ids": result.rule_ids, "reviewer": result.reviewer}
        return {"compliance": result.as_json()}, detail, reply.usage.total_tokens

    stage = _stage(COMPLIANCE, state, state["iteration"])
    return _step(context, state["ticket_id"], stage, work)


def merge_evaluation(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    assignment = state["assignment"]
    iteration = state["iteration"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        result = evaluation.compose(
            section_id=assignment["section_id"],
            department_id=assignment["department_id"],
            iteration=iteration,
            readability_result=ReadabilityResult.model_validate(state["readability"]),
            relevance=RelevanceResult.model_validate(state["relevance"]),
            compliance=ComplianceResult.model_validate(state["compliance"]),
            guard_notes=state.get("guard_notes") or [],
            currency=assignment.get("currency"),
            country=assignment.get("country"),
            evaluated_at=utcnow(),
        )
        decision, reason = evaluation.decide(
            result, iteration=iteration, max_iterations=assignment["max_iterations"]
        )
        proposal_store.save_evaluation(
            context.engine,
            state["ticket_id"],
            assignment["section_id"],
            run_id=state["run_id"],
            iteration=iteration,
            evaluation=result.as_json(),
            overall_pass=result.overall_pass,
            statuses=context.active_statuses,
        )
        detail = {
            "overall_pass": result.overall_pass,
            "failed": evaluation.failed_checks(result),
            "decision": decision,
            "reason": reason,
        }
        update = {"evaluation": result.as_json(), "decision": decision, "decision_reason": reason}
        return update, detail, 0

    return _step(context, state["ticket_id"], _stage(MERGE, state, iteration), work)


def route_after_evaluation(state: SectionState) -> str:
    return state["decision"]


def _finish(state: SectionState, runtime: Runtime[ProposalContext], node: str, status: str) -> Any:
    context = runtime.context
    assignment = state["assignment"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        proposal_store.finish_section(
            context.engine,
            state["ticket_id"],
            assignment["section_id"],
            status=status,
            statuses=context.active_statuses,
        )
        detail = {
            "department_id": assignment["department_id"],
            "iterations": state["iteration"],
            "reason": state.get("decision_reason"),
        }
        return {"outcome": status}, detail, 0

    update = _step(context, state["ticket_id"], _stage(node, state), work)
    if status == "needs_human_review":
        store.record_event(
            context.engine,
            state["ticket_id"],
            _stage(node, state),
            "warning",
            {"needs_human_review": True, "reason": state.get("decision_reason")},
        )
    return update


def accept_section(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    result: dict[str, Any] = _finish(state, runtime, ACCEPT, "passed")
    return result


def escalate_section(state: SectionState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    result: dict[str, Any] = _finish(state, runtime, ESCALATE, "needs_human_review")
    return result


SectionGraph = CompiledStateGraph[SectionState, ProposalContext, SectionState, SectionState]


def build_section_graph() -> SectionGraph:
    builder = StateGraph(SectionState, context_schema=ProposalContext)
    builder.add_node(RETRIEVE, retrieve_knowledge)
    builder.add_node(GENERATE, generate_section, retry_policy=MODEL_RETRY)
    builder.add_node(READABILITY, check_readability)
    builder.add_node(RELEVANCE, check_relevance, retry_policy=MODEL_RETRY)
    builder.add_node(COMPLIANCE, check_compliance)
    builder.add_node(MERGE, merge_evaluation)
    builder.add_node(ACCEPT, accept_section)
    builder.add_node(ESCALATE, escalate_section)

    builder.add_edge(START, RETRIEVE)
    builder.add_edge(RETRIEVE, GENERATE)
    # The three evaluators run in parallel over the same draft, then join.
    for evaluator in (READABILITY, RELEVANCE, COMPLIANCE):
        builder.add_edge(GENERATE, evaluator)
    builder.add_edge([READABILITY, RELEVANCE, COMPLIANCE], MERGE)
    builder.add_conditional_edges(
        MERGE, route_after_evaluation, {"revise": GENERATE, "accept": ACCEPT, "escalate": ESCALATE}
    )
    builder.add_edge(ACCEPT, END)
    builder.add_edge(ESCALATE, END)
    return builder.compile(name=SECTION_GRAPH_NAME)


@lru_cache(maxsize=1)
def get_section_graph() -> SectionGraph:
    return build_section_graph()


# --- Outer graph --------------------------------------------------------------------------


def plan_assignments(state: ProposalState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        intake = IntakeHandoff.model_validate(state["handoff"])
        departments = [dept.department_id for dept in intake.departments]
        section_ids = proposal_store.start_run(
            context.engine, ticket_id, run_id=state["run_id"], departments=list(departments)
        )
        sla, assignments = build_assignments(
            intake, section_ids, max_iterations=context.settings.max_draft_iterations
        )
        detail = {
            "departments": departments,
            "sla_department": sla,
            "currency": intake.client.currency,
            "rate_card_version": assignments[0]["rate_card_version"],
            "max_iterations": context.settings.max_draft_iterations,
        }
        update = {"assignments": [dict(item) for item in assignments], "sla_department": sla}
        return update, detail, 0

    return _step(context, ticket_id, PLAN, work)


def dispatch_sections(state: ProposalState) -> list[Send]:
    return [
        Send(
            DRAFT,
            SectionTask(ticket_id=state["ticket_id"], run_id=state["run_id"], assignment=item),
        )
        for item in state["assignments"]
    ]


def _after_failure(
    context: ProposalContext, task: SectionTask, failure: StageFailed
) -> dict[str, Any]:
    """A section whose loop broke: its last evaluated draft ships for review, if it has one."""
    assignment = task["assignment"]
    section_id = assignment["section_id"]
    outcome = {"department_id": assignment["department_id"], "section_id": section_id}
    attempt = proposal_store.latest_evaluated_attempt(context.engine, section_id, task["run_id"])
    if attempt is None:
        proposal_store.finish_section(
            context.engine, task["ticket_id"], section_id, status="failed"
        )
        return {
            **outcome,
            "outcome": "failed",
            "error_stage": failure.stage,
            "error_message": failure.message,
        }
    note = (
        f"Attempt {attempt.iteration + 1} could not be completed ({failure.stage}: "
        f"{failure.message}); this is attempt {attempt.iteration}, the last one evaluated."
    )
    draft_data = {
        "draft": attempt.draft_data,
        "guard_notes": list(attempt.guard_notes),
        "is_sla_section": assignment["is_sla_section"],
        "max_iterations": assignment["max_iterations"],
        "rate_card_version": assignment["rate_card_version"],
        "run_id": task["run_id"],
        "review_note": note,
    }
    proposal_store.restore_attempt(
        context.engine, task["ticket_id"], section_id, attempt, draft_data=draft_data
    )
    store.record_event(
        context.engine,
        task["ticket_id"],
        f"{ESCALATE}:{assignment['department_id']}",
        "warning",
        {"needs_human_review": True, "reason": note},
    )
    return {**outcome, "outcome": "needs_human_review", "iterations": attempt.iteration}


def draft_section(state: SectionTask, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    # `state` is the `SectionTask` that `dispatch_sections` sent, not the graph state.
    task = state
    context = runtime.context
    assignment = task["assignment"]
    try:
        final = get_section_graph().invoke(
            {"ticket_id": task["ticket_id"], "run_id": task["run_id"], "assignment": assignment},
            context=context,
        )
    except StageFailed as failure:
        return {"sections": [_after_failure(context, task, failure)]}
    return {
        "sections": [
            {
                "department_id": assignment["department_id"],
                "section_id": assignment["section_id"],
                "outcome": final["outcome"],
                "iterations": final["iteration"],
            }
        ]
    }


def synthesize_proposal(state: ProposalState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]
    failed = [section for section in state["sections"] if section["outcome"] == "failed"]
    if failed:
        # No draft of that section was ever evaluated: nothing can go to approval.
        raise StageFailed(failed[0]["error_stage"], failed[0]["error_message"])

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        intake = IntakeHandoff.model_validate(state["handoff"])
        proposal = handoff.build_proposal(
            intake,
            proposal_store.load_sections(context.engine, ticket_id),
            sla_department=state["sla_department"],
            max_iterations=context.settings.max_draft_iterations,
        )
        detail = {
            "status": proposal.status,
            "sections": {s.department_id: s.draft_status for s in proposal.sections},
            "average_iterations_per_section": proposal.metrics.average_iterations_per_section,
        }
        return {"proposal": proposal.model_dump(mode="json", by_alias=True)}, detail, 0

    return _step(context, ticket_id, SYNTHESIZE, work)


def complete_drafting(state: ProposalState, runtime: Runtime[ProposalContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        proposal = state["proposal"]
        status = proposal_store.complete_drafting(
            context.engine,
            ticket_id,
            payload=proposal,
            needs_human_review=proposal["status"] == "needs_human_review",
            contract_version=handoff.CONTRACT_VERSION,
            next_stage=handoff.NEXT_STAGE,
        )
        detail = {"handoff": handoff.CONTRACT_VERSION, "next_stage": handoff.NEXT_STAGE}
        return {"outcome": status}, {**detail, "status": status}, 0

    return _step(context, ticket_id, COMPLETE, work)


ProposalGraph = CompiledStateGraph[ProposalState, ProposalContext, ProposalState, ProposalState]


def build_proposal_graph() -> ProposalGraph:
    builder = StateGraph(ProposalState, context_schema=ProposalContext)
    builder.add_node(PLAN, plan_assignments)
    builder.add_node(DRAFT, draft_section)  # no retry: it would re-run a whole loop
    builder.add_node(SYNTHESIZE, synthesize_proposal)
    builder.add_node(COMPLETE, complete_drafting)

    builder.add_edge(START, PLAN)
    builder.add_conditional_edges(PLAN, dispatch_sections, [DRAFT])
    builder.add_edge(DRAFT, SYNTHESIZE)
    builder.add_edge(SYNTHESIZE, COMPLETE)
    builder.add_edge(COMPLETE, END)
    return builder.compile(name=GRAPH_NAME)


@lru_cache(maxsize=1)
def get_proposal_graph() -> ProposalGraph:
    return build_proposal_graph()


def draw_mermaid() -> dict[str, str]:
    """Both Part 2 graphs as Mermaid (the loop runs inside `draft_section`)."""
    drawers: dict[str, Callable[[], str]] = {
        GRAPH_NAME: lambda: get_proposal_graph().get_graph().draw_mermaid(),
        SECTION_GRAPH_NAME: lambda: get_section_graph().get_graph().draw_mermaid(),
    }
    return {name: draw() for name, draw in drawers.items()}
