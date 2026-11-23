"""The dedicated `rfp_intake` LangGraph graph (not part of the CX / knowledge agent).

    START -> convert_to_markdown -> classify_document
        -(route_after_classification)-> discard_ticket -> END
                                      -> measure_readability -> extract_metadata
        -> plan_departments -(assign_workers)-> discard_ticket -> END   (no TrackFlow scope)
                                              -> analyze_department x N  (Send, in parallel)
        -> synthesize_brief -> complete_intake -> END

- Orchestrator-worker-synthesizer: `plan_departments` decomposes the RFP;
  `assign_workers` sends each department's worker only `WorkerTask` (the
  shared metadata and its own extracts); the `sections` reducer gathers their
  results for `synthesize_brief`.
- The database engine, the agents and the settings come through the runtime
  context (`IntakeContext`), so tests run this same graph with fakes.
- Every node runs inside `_step` (`steps.run_step`): it moves `current_stage`
  (live progress for the UI), writes an `rfp_ticket_events` row with its
  duration and tokens, and turns any failure into `StageFailed`, which the
  runner records as `failed`.
- No checkpointer: a reprocess starts from scratch (`store.reset_ticket`).
"""

import operator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import conversion, handoff, readability, store
from data.pipelines.rfp_intake.agents import AgentReply, IntakeAgents
from data.pipelines.rfp_intake.briefing import DEPARTMENT_IDS
from data.pipelines.rfp_intake.config import ROOT_DIR, RfpIntakeSettings
from data.pipelines.rfp_intake.steps import (  # noqa: F401 - re-exported for Part 1 callers
    MODEL_RETRY,
    StageFailed,
    StepWork,
    run_step,
    safe_message,
)

GRAPH_NAME: Final = "rfp_intake"

CONVERT: Final = "convert_to_markdown"
CLASSIFY: Final = "classify_document"
DISCARD: Final = "discard_ticket"
MEASURE: Final = "measure_readability"
EXTRACT: Final = "extract_metadata"
PLAN: Final = "plan_departments"
ANALYZE: Final = "analyze_department"
SYNTHESIZE: Final = "synthesize_brief"
COMPLETE: Final = "complete_intake"

ROUTE_AFTER_CLASSIFICATION: Final = "route_after_classification"
ASSIGN_WORKERS: Final = "assign_workers"

# The order a successful run goes through, for progress displays.
STAGES: Final = (CONVERT, CLASSIFY, MEASURE, EXTRACT, PLAN, ANALYZE, SYNTHESIZE, COMPLETE)

NO_SCOPE_REASON: Final = (
    "The document asks for a proposal but for none of the services TrackFlow sells "
    "(warehousing, last mile, returns), so no department can quote it."
)


class IntakeState(TypedDict, total=False):
    ticket_id: str
    markdown: str
    classification: dict[str, Any]
    readability: dict[str, Any]
    metadata: dict[str, Any]
    plan: dict[str, Any]
    sections: Annotated[list[dict[str, Any]], operator.add]
    brief: dict[str, Any]
    discard_reason: str
    outcome: str


class WorkerTask(TypedDict):
    """Everything one department worker receives: never the whole document."""

    ticket_id: str
    department_id: str
    objective: str
    extracts: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class IntakeContext:
    engine: Engine
    agents: IntakeAgents
    settings: RfpIntakeSettings


def _step(context: IntakeContext, ticket_id: str, stage: str, work: StepWork) -> dict[str, Any]:
    return run_step(context.engine, ticket_id, stage, work)


def _tokens(reply: AgentReply) -> int:
    return reply.usage.total_tokens


def resolve_pdf_path(raw_pdf_path: str) -> Path:
    path = Path(raw_pdf_path)
    return path if path.is_absolute() else ROOT_DIR / path


# --- Nodes ---------------------------------------------------------------------------


def convert_to_markdown(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        ticket = store.get_ticket(context.engine, ticket_id)
        if ticket is None:
            raise store.TicketNotFound("the ticket no longer exists")
        markdown = conversion.pdf_to_markdown(resolve_pdf_path(ticket.raw_pdf_path))
        markdown, truncated = conversion.truncate(markdown, context.settings.max_document_chars)
        store.save_markdown(context.engine, ticket_id, markdown, truncated)
        return {"markdown": markdown}, {"chars": len(markdown), "truncated": truncated}, 0

    return _step(context, ticket_id, CONVERT, work)


def classify_document(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.classify_document(state["markdown"])
        verdict = reply.result
        store.save_classification(context.engine, ticket_id, verdict)
        detail = {
            key: verdict.get(key)
            for key in ("decision", "document_kind", "confidence", "language", "flags")
        }
        return {"classification": verdict}, detail, _tokens(reply)

    return _step(context, ticket_id, CLASSIFY, work)


def route_after_classification(state: IntakeState) -> str:
    return "discard" if state["classification"]["decision"] == "discard" else "analyze"


def discard_ticket(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reason = (
            state.get("discard_reason")
            or state.get("classification", {}).get("discard_reason")
            or "The document was not accepted as a TrackFlow RFP."
        )
        store.discard(context.engine, ticket_id, reason)
        return {"outcome": "discarded"}, {"reason": reason}, 0

    return _step(context, ticket_id, DISCARD, work)


def measure_readability(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        language = state.get("classification", {}).get("language")
        metrics = readability.measure(state["markdown"], language=language).as_dict()
        return {"readability": metrics}, metrics, 0

    return _step(context, ticket_id, MEASURE, work)


def extract_metadata(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.extract_metadata(state["markdown"])
        metadata = reply.result
        store.save_metadata(
            context.engine,
            ticket_id,
            metadata,
            state["readability"],
            language=state.get("classification", {}).get("language"),
        )
        detail = {
            key: metadata.get(key)
            for key in (
                "client_name",
                "client_country",
                "services_requested",
                "departments_needed",
                "missing",
                "notes",
            )
        }
        return {"metadata": metadata}, detail, _tokens(reply)

    return _step(context, ticket_id, EXTRACT, work)


def plan_departments(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.plan_departments(state["markdown"], state["metadata"])
        plan = reply.result
        update: dict[str, Any] = {"plan": plan}
        if not plan["subtasks"]:
            update["discard_reason"] = NO_SCOPE_REASON
        detail = {
            "departments": [task["department_id"] for task in plan["subtasks"]],
            "extracts": {task["department_id"]: len(task["extracts"]) for task in plan["subtasks"]},
            "unassigned_requirements": plan.get("unassigned_requirements"),
            "notes": plan.get("notes"),
        }
        return update, detail, _tokens(reply)

    return _step(context, ticket_id, PLAN, work)


def assign_workers(state: IntakeState) -> list[Send] | str:
    subtasks = state["plan"]["subtasks"]
    if not subtasks:
        return DISCARD
    return [
        Send(
            ANALYZE,
            WorkerTask(
                ticket_id=state["ticket_id"],
                department_id=task["department_id"],
                objective=task["objective"],
                extracts=task["extracts"],
                metadata=state["metadata"],
            ),
        )
        for task in subtasks
    ]


def analyze_department(state: WorkerTask, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    # `state` is the `WorkerTask` that `assign_workers` sent, not the graph state.
    task = state
    context = runtime.context
    stage = f"{ANALYZE}:{task['department_id']}"

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.analyze_department(dict(task))
        section = reply.result
        detail = {
            "department_id": section["department_id"],
            "key_aspects": len(section["key_aspects"]),
            "open_questions": len(section["open_questions"]),
            "validation_notes": section["validation_notes"],
        }
        return {"sections": [section]}, detail, _tokens(reply)

    return _step(context, task["ticket_id"], stage, work)


def synthesize_brief(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        reply = context.agents.synthesize_brief(state["metadata"], state["sections"], state["plan"])
        brief = reply.result
        detail = {
            "departments": [item["department_id"] for item in brief["departments"]],
            "client_questions": len(brief["client_questions"]),
            "validation_notes": brief.get("validation_notes"),
        }
        return {"brief": brief}, detail, _tokens(reply)

    return _step(context, ticket_id, SYNTHESIZE, work)


def complete_intake(state: IntakeState, runtime: Runtime[IntakeContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any], int]:
        ticket = store.get_ticket(context.engine, ticket_id)
        if ticket is None:
            raise store.TicketNotFound("the ticket no longer exists")
        sections = sorted(
            state["sections"], key=lambda section: DEPARTMENT_IDS.index(section["department_id"])
        )
        payload = handoff.build_handoff(
            ticket=ticket,
            metadata=state["metadata"],
            readability=state["readability"],
            sections=sections,
            brief=state["brief"],
        )
        store.complete_intake(
            context.engine,
            ticket_id,
            sections=sections,
            sales_brief=state["brief"],
            handoff_payload=payload.model_dump(mode="json"),
            contract_version=handoff.CONTRACT_VERSION,
            next_stage=handoff.NEXT_STAGE,
        )
        detail = {"handoff": handoff.CONTRACT_VERSION, "next_stage": handoff.NEXT_STAGE}
        return {"outcome": "intake_complete"}, detail, 0

    return _step(context, ticket_id, COMPLETE, work)


# --- Assembly --------------------------------------------------------------------------


IntakeGraph = CompiledStateGraph[IntakeState, IntakeContext, IntakeState, IntakeState]


def build_intake_graph() -> IntakeGraph:
    builder = StateGraph(IntakeState, context_schema=IntakeContext)
    builder.add_node(CONVERT, convert_to_markdown)
    builder.add_node(CLASSIFY, classify_document, retry_policy=MODEL_RETRY)
    builder.add_node(DISCARD, discard_ticket)
    builder.add_node(MEASURE, measure_readability)
    builder.add_node(EXTRACT, extract_metadata, retry_policy=MODEL_RETRY)
    builder.add_node(PLAN, plan_departments, retry_policy=MODEL_RETRY)
    builder.add_node(ANALYZE, analyze_department, retry_policy=MODEL_RETRY)
    builder.add_node(SYNTHESIZE, synthesize_brief, retry_policy=MODEL_RETRY)
    builder.add_node(COMPLETE, complete_intake)

    builder.add_edge(START, CONVERT)
    builder.add_edge(CONVERT, CLASSIFY)
    builder.add_conditional_edges(
        CLASSIFY, route_after_classification, {"discard": DISCARD, "analyze": MEASURE}
    )
    builder.add_edge(MEASURE, EXTRACT)
    builder.add_edge(EXTRACT, PLAN)
    builder.add_conditional_edges(PLAN, assign_workers, [ANALYZE, DISCARD])
    builder.add_edge(ANALYZE, SYNTHESIZE)
    builder.add_edge(SYNTHESIZE, COMPLETE)
    builder.add_edge(COMPLETE, END)
    builder.add_edge(DISCARD, END)
    return builder.compile(name=GRAPH_NAME)


@lru_cache(maxsize=1)
def get_intake_graph() -> IntakeGraph:
    return build_intake_graph()


def draw_mermaid() -> str:
    return get_intake_graph().get_graph().draw_mermaid()
