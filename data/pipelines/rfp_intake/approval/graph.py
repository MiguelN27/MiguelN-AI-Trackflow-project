"""The Part 3 graphs: `rfp_approval` and its per-department branch `rfp_department_review`.

Outer graph, one checkpoint thread per ticket (`rfp-<ticket_id>`):

    START -> open_approval -> arbitrate
    arbitrate -(Send, one per department still to review)-> review_department -> arbitrate
    arbitrate -> compose_final_document -> complete_approval -> END
    arbitrate -> close_rejected -> END

- `open_approval` takes the claimed `rfp-proposal/1` handoff (from either of
  Part 2's end statuses) and moves the ticket to `waiting_for_approval`.
- `arbitrate` is the hub, and the briefing's §7 arbitration node. It runs
  first on Part 2's sections and again each time the department branches
  converge. It applies the fixed rules (`arbitration.py`) and decides where
  the run goes next: review for the sections still to approve (with a ruling's
  instruction for the conflicting ones), the final document once every
  section is approved and nothing conflicts, or `close_rejected`.
- `review_department` runs one department's branch (the subgraph below). The
  departments run in parallel through `Send`. An interrupt pauses only its own
  branch, so another department is approved, revised, or rejected meanwhile.
- `compose_final_document` is the "ultimate document synthesizer" of the
  visual reference: deterministic, the approved sections verbatim.
- `complete_approval` stores the document and sets `done` in one transaction.

Department branch:

    START -> open_review | start_revision -> revise_section -> open_review
    open_review -> await_decision -> validate_decision
        -> sign_off | record_rejection | withdraw -> END
        -> start_revision (request_changes)
        -> await_decision (a refused answer: asked again, with the reason)
    open_review -> withdraw (another section was rejected meanwhile)

`open_review` persists the request before the pause, so the checkpoint after it
holds everything. `await_decision` only calls `interrupt()`: LangGraph re-runs
the node holding an interrupt on every resume of the thread, so it has no side
effect. Every other node runs in `_run`, which moves `current_stage`, writes an
`rfp_ticket_events` row (agent, input, output, timing) and adds the same entry
to the graph state's `trace`. A failure becomes `StageFailed`, as in Parts 1-2.

`revise_section` runs Part 2's own generator-evaluator loop
(`rfp_proposal_section`) with the reviewer's request; nested in this graph, its
steps are checkpointed too.
"""

import contextlib
import hashlib
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Final, TypedDict
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt
from sqlalchemy.engine import Engine
from sqlmodel import Session

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import approvers, arbitration, decisions
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.approval.document import compose
from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.handoff import IntakeHandoff
from data.pipelines.rfp_intake.models import RfpDepartmentSection, RfpHandoff, utcnow
from data.pipelines.rfp_intake.proposal import store as proposal_store
from data.pipelines.rfp_intake.proposal.agents import ProposalAgents
from data.pipelines.rfp_intake.proposal.assignments import build_assignments
from data.pipelines.rfp_intake.proposal.graph import ProposalContext, get_section_graph
from data.pipelines.rfp_intake.proposal.handoff import ProposalHandoff
from data.pipelines.rfp_intake.steps import StageFailed, safe_message
from data.pipelines.rfp_intake.text import first_line

GRAPH_NAME: Final = "rfp_approval"
REVIEW_GRAPH_NAME: Final = "rfp_department_review"

OPEN: Final = "open_approval"
ARBITRATE: Final = "arbitrate"
REVIEW: Final = "review_department"
COMPOSE: Final = "compose_final_document"
COMPLETE: Final = "complete_approval"
CLOSE: Final = "close_rejected"

START_REVISION: Final = "start_revision"
REVISE: Final = "revise_section"
OPEN_REVIEW: Final = "open_review"
AWAIT: Final = "await_decision"
VALIDATE: Final = "validate_decision"
SIGN_OFF: Final = "sign_off"
REJECT: Final = "record_rejection"
WITHDRAW: Final = "withdraw"

ORCHESTRATOR: Final = "approval orchestrator"
VALIDATOR: Final = "decision validator"
SYNTHESIZER: Final = "final document synthesizer"


@dataclass(frozen=True)
class ApprovalContext:
    engine: Engine
    agents: ProposalAgents
    settings: RfpIntakeSettings


def _merge(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    return {**(left or {}), **(right or {})}


class ApprovalState(TypedDict, total=False):
    ticket_id: str
    thread_id: str
    proposal: dict[str, Any]
    departments: list[str]
    # Each department branch's latest outcome (`approved`, `rejected`, `withdrawn`).
    sections: Annotated[dict[str, dict[str, Any]], _merge]
    arbitration_rounds: int
    rulings: Annotated[list[dict[str, Any]], operator.add]
    # Set by a `volume-vs-capacity` ruling; kept for every later revision of lastmile.
    volume_cap: int | None
    document: dict[str, Any]
    outcome: str
    # Every node execution: node, agent, department, input, output, timestamps.
    trace: Annotated[list[dict[str, Any]], operator.add]


class ReviewTask(TypedDict):
    """What one department's branch receives from `arbitrate`."""

    ticket_id: str
    thread_id: str
    department_id: str
    # An arbitration ruling's request, when the section must change before review.
    revision: dict[str, Any] | None
    volume_cap: int | None


class ReviewState(TypedDict, total=False):
    ticket_id: str
    thread_id: str
    department_id: str
    revision: dict[str, Any] | None
    volume_cap: int | None
    request: dict[str, Any]
    response: Any
    decision: dict[str, Any] | None
    refusal: str | None
    withdraw_reason: str | None
    outcome: str
    trace: Annotated[list[dict[str, Any]], operator.add]


# --- Tracing --------------------------------------------------------------------------


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _entry(
    *,
    node: str,
    agent: str,
    department: str | None,
    inputs: dict[str, Any],
    output: dict[str, Any],
    outcome: str,
    started_at: Any,
    finished_at: Any,
) -> dict[str, Any]:
    return {
        "part": 3,
        "node": node,
        "agent": agent,
        "department": department,
        "input": inputs,
        "output": output,
        "outcome": outcome,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
    }


Work = Callable[[], tuple[dict[str, Any], dict[str, Any]]]


def _run(
    context: ApprovalContext,
    ticket_id: str,
    stage: str,
    work: Work,
    *,
    agent: str,
    department: str | None,
    inputs: dict[str, Any],
    thread: str,
    statuses: frozenset[str] = approval_store.PART_3_ACTIVE,
) -> dict[str, Any]:
    """Run one node's `work`: its state update plus a trace entry, also stored as an event."""
    store.set_stage(context.engine, ticket_id, stage, statuses=statuses)
    started_at = utcnow()
    started = time.perf_counter()
    detail = {"part": 3, "agent": agent, "department": department, "input": inputs}
    try:
        update, output = work()
    except (GraphBubbleUp, StageFailed, store.TicketStateError):
        raise
    except Exception as error:
        message = safe_message(error)
        store.record_event(
            context.engine,
            ticket_id,
            stage,
            "failed",
            {**detail, "thread_id": thread, "error": type(error).__name__, "message": message},
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise StageFailed(stage, message) from error
    finished_at = utcnow()
    store.record_event(
        context.engine,
        ticket_id,
        stage,
        "completed",
        {**detail, "output": output, "thread_id": thread},
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    entry = _entry(
        node=stage,
        agent=agent,
        department=department,
        inputs=inputs,
        output=output,
        outcome="completed",
        started_at=started_at,
        finished_at=finished_at,
    )
    return {**update, "trace": [entry]}


def _owner_agent(department_id: str) -> str:
    return f"{approvers.owner_of(department_id)} (owner of {DEPARTMENT_BY_ID[department_id].name})"


# --- Outer graph --------------------------------------------------------------------------


def open_approval(state: ApprovalState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    proposal = ProposalHandoff.model_validate(state["proposal"])
    departments = [section.department_id for section in proposal.sections]

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        previous = approval_store.open_approval(context.engine, state["ticket_id"], departments)
        update = {
            "departments": departments,
            "arbitration_rounds": 0,
            "volume_cap": None,
            "sections": {dept: {"approval_status": "pending"} for dept in departments},
        }
        output = {
            "previous_status": previous,
            "status": approval_store.WAITING,
            "owners": {dept: approvers.owner_of(dept) for dept in departments},
            "needs_human_review": [
                s.department_id for s in proposal.sections if s.needs_human_review
            ],
        }
        return update, output

    return _run(
        context,
        state["ticket_id"],
        OPEN,
        work,
        agent=ORCHESTRATOR,
        department=None,
        inputs={
            "contract": proposal.contract_version,
            "proposal_status": proposal.status,
            "departments": departments,
        },
        thread=state["thread_id"],
        statuses=approval_store.OPENABLE,
    )


def _views(
    rows: list[RfpDepartmentSection], departments: list[str]
) -> list[arbitration.SectionView]:
    return [
        arbitration.SectionView(
            department_id=row.department_id,
            draft=(row.draft_data or {}).get("draft") or {},
            markdown=row.draft_content or "",
            approval_status=row.approval_status,
            review_rounds=row.review_rounds or 0,
        )
        for row in rows
        if row.department_id in departments
    ]


def _arbiter_revision(rulings: list[arbitration.Ruling], decision_ids: list[str]) -> dict[str, Any]:
    arbiters = sorted({f"{r.arbiter} ({r.arbiter_basis})" for r in rulings})
    return {
        "source": "arbiter",
        "requested_by": "; ".join(arbiters),
        "instruction": "\n".join(r.instruction for r in rulings),
        "comment": None,
        "trigger_ids": [r.trigger_id for r in rulings],
        "decision_ids": decision_ids,
    }


def arbitrate(state: ApprovalState, runtime: Runtime[ApprovalContext]) -> Command[Any]:
    """§7: detect contradictions, apply the fixed rule, and route the run."""
    context = runtime.context
    settings = context.settings
    ticket_id = state["ticket_id"]
    proposal = ProposalHandoff.model_validate(state["proposal"])
    departments = state["departments"]
    rounds_used = state.get("arbitration_rounds", 0)
    route: dict[str, Any] = {}

    def task(dept: str, revision: dict[str, Any] | None, volume_cap: int | None) -> Send:
        return Send(
            REVIEW,
            ReviewTask(
                ticket_id=ticket_id,
                thread_id=state["thread_id"],
                department_id=dept,
                revision=revision,
                volume_cap=volume_cap,
            ),
        )

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        rows = approval_store.load_sections(context.engine, ticket_id)
        views = _views(rows, departments)
        by_dept = {row.department_id: row for row in rows}
        rejected = [v["department_id"] for v in views if v["approval_status"] == "rejected"]
        if rejected:
            route["goto"] = CLOSE
            return {}, {"decision": "close", "rejected": rejected}

        conflicts = arbitration.detect(
            views, currency=proposal.currency, country=proposal.client.country
        )
        rulings = arbitration.rule_on(
            conflicts,
            rounds_used=rounds_used,
            max_rounds=settings.max_arbitration_rounds,
            revisions_left={
                v["department_id"]: settings.max_revision_rounds - v["review_rounds"] for v in views
            },
            currency=proposal.currency,
            country=proposal.client.country,
            now=utcnow(),
        )
        as_json = [ruling.model_dump(mode="json") for ruling in rulings]
        output: dict[str, Any] = {
            "conflicts": [c.trigger_id for c in conflicts],
            "rulings": [
                {
                    "trigger_id": r.trigger_id,
                    "arbiter": r.arbiter,
                    "action": r.action,
                    "departments": r.departments,
                }
                for r in rulings
            ],
        }
        rejects = [ruling for ruling in rulings if ruling.action == "reject"]
        if rejects:
            for ruling in rejects:
                for rejected_dept in ruling.departments:
                    row = by_dept[rejected_dept]
                    approval_store.record_decision(
                        context.engine,
                        ticket_id=ticket_id,
                        section_id=row.id,
                        department_id=rejected_dept,
                        review_round=(row.review_rounds or 0) + 1,
                        draft_sha256=decisions.draft_sha256(row.draft_content),
                        action="reject",
                        comment=ruling.instruction,
                        source="arbiter",
                        decided_by=ruling.arbiter,
                        decided_by_name=ruling.arbiter,
                        trigger_id=ruling.trigger_id,
                        status="applied",
                    )
                    approval_store.set_section_status(context.engine, ticket_id, row.id, "rejected")
            approval_store.add_rulings(context.engine, ticket_id, as_json, rounds=rounds_used)
            route["goto"] = CLOSE
            return {"rulings": as_json}, {**output, "decision": "close"}

        volume_cap = state.get("volume_cap")
        if rulings:
            rounds = rounds_used + 1
            by_department: dict[str, list[arbitration.Ruling]] = {}
            for ruling in rulings:
                for ruled in ruling.departments:
                    by_department.setdefault(ruled, []).append(ruling)
                if ruling.volume_cap is not None:
                    volume_cap = ruling.volume_cap
            sends: list[Send] = []
            for dept in departments:
                if dept in by_department:
                    row = by_dept[dept]
                    ids = [
                        str(
                            approval_store.record_decision(
                                context.engine,
                                ticket_id=ticket_id,
                                section_id=row.id,
                                department_id=dept,
                                review_round=(row.review_rounds or 0) + 1,
                                draft_sha256=decisions.draft_sha256(row.draft_content),
                                action="request_changes",
                                comment=ruling.instruction,
                                source="arbiter",
                                decided_by=ruling.arbiter,
                                decided_by_name=ruling.arbiter,
                                trigger_id=ruling.trigger_id,
                                status="applied",
                            ).id
                        )
                        for ruling in by_department[dept]
                    ]
                    sends.append(
                        task(dept, _arbiter_revision(by_department[dept], ids), volume_cap)
                    )
                elif by_dept[dept].approval_status != "approved":
                    sends.append(task(dept, None, volume_cap))
            approval_store.add_rulings(context.engine, ticket_id, as_json, rounds=rounds)
            route["goto"] = sends
            update = {"rulings": as_json, "arbitration_rounds": rounds, "volume_cap": volume_cap}
            return update, {**output, "decision": "revise", "arbitration_round": rounds}

        pending = [v["department_id"] for v in views if v["approval_status"] != "approved"]
        if pending:
            route["goto"] = [task(dept, None, volume_cap) for dept in pending]
            return {}, {**output, "decision": "review", "departments": pending}
        route["goto"] = COMPOSE
        return {}, {**output, "decision": "compose"}

    update = _run(
        context,
        ticket_id,
        ARBITRATE,
        work,
        agent=f"arbitration node (fixed §7 rules; arbiters {arbitration.REVERSE_OWNER} and "
        "Miguel Torres)",
        department=None,
        inputs={"departments": departments, "arbitration_rounds_used": rounds_used},
        thread=state["thread_id"],
    )
    return Command(update=update, goto=route["goto"])


def review_department(state: ReviewTask, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    # `state` is the `ReviewTask` that `arbitrate` sent, not the graph state.
    final = get_review_graph().invoke(
        ReviewState(
            ticket_id=state["ticket_id"],
            thread_id=state["thread_id"],
            department_id=state["department_id"],
            revision=state["revision"],
            volume_cap=state["volume_cap"],
        ),
        context=runtime.context,
    )
    return {
        "sections": {state["department_id"]: {"approval_status": final["outcome"]}},
        "trace": final.get("trace") or [],
    }


def _section_rows(engine: Engine, ticket_id: str, departments: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "department_id": row.department_id,
            "approval_status": row.approval_status,
            "approved_at": store.as_utc(row.approved_at),
            "approved_by": row.approved_by,
            "owner": row.owner,
            "draft_content": row.draft_content,
            "review_rounds": row.review_rounds or 0,
        }
        for row in approval_store.load_sections(engine, ticket_id)
        if row.department_id in departments
    ]


def compose_final_document(
    state: ApprovalState, runtime: Runtime[ApprovalContext]
) -> dict[str, Any]:
    context = runtime.context
    proposal = ProposalHandoff.model_validate(state["proposal"])

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        document = compose(
            proposal,
            _section_rows(context.engine, state["ticket_id"], state["departments"]),
            thread_id=state["thread_id"],
            volume_cap=state.get("volume_cap"),
            rulings=list(state.get("rulings") or []),
            generated_at=utcnow(),
        )
        output = {
            "sections": [s.department_id for s in document.sections],
            "currency": document.currency,
            "content_sha256": document.content_sha256[:16],
            "characters": len(document.content),
        }
        return {"document": document.model_dump(mode="json")}, output

    return _run(
        context,
        state["ticket_id"],
        COMPOSE,
        work,
        agent=SYNTHESIZER,
        department=None,
        inputs={"approved": state["departments"], "rulings": len(state.get("rulings") or [])},
        thread=state["thread_id"],
    )


def complete_approval(state: ApprovalState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    document = state["document"]

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        approval_store.complete_approval(context.engine, state["ticket_id"], document)
        return {"outcome": "done"}, {"status": "done", "delivered_to": "Sales (the ticket page)"}

    return _run(
        context,
        state["ticket_id"],
        COMPLETE,
        work,
        agent=ORCHESTRATOR,
        department=None,
        inputs={"content_sha256": document["content_sha256"][:16]},
        thread=state["thread_id"],
    )


def close_rejected(state: ApprovalState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    ticket_id = state["ticket_id"]

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        rejections = [
            d
            for d in approval_store.decisions_for(context.engine, ticket_id)
            if d.action == "reject" and d.status == "applied"
        ]
        withdrawn = approval_store.close_rejected(context.engine, ticket_id)
        output = {
            "status": "rejected",
            "rejected_by": [
                {
                    "department": d.department_id,
                    "by": d.decided_by_name,
                    "source": d.source,
                    "reason": first_line(d.comment or "", 200),
                }
                for d in rejections
            ],
            "withdrawn": withdrawn,
        }
        return {"outcome": "rejected"}, output

    return _run(
        context,
        ticket_id,
        CLOSE,
        work,
        agent=ORCHESTRATOR,
        department=None,
        inputs={"departments": state["departments"]},
        thread=state["thread_id"],
    )


# --- Department branch -----------------------------------------------------------------------


def _stage(node: str, state: ReviewState, round_: int | None = None) -> str:
    dept = state["department_id"]
    return f"{node}:{dept}" if round_ is None else f"{node}:{dept}#{round_}"


def route_start(state: ReviewState) -> str:
    return "revise" if state.get("revision") else "review"


def start_revision(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    dept = state["department_id"]
    revision = dict(state["revision"] or {})
    section = approval_store.load_section(context.engine, state["ticket_id"], dept)

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        rounds = approval_store.start_revision(context.engine, state["ticket_id"], section.id)
        # The run id is fixed here, before the loop: a resumed `revise_section` reuses it.
        revision.update(round=rounds, run_id=str(uuid4()))
        output = {"revision_round": rounds, "approval_status": "changes_requested"}
        return {"revision": revision}, output

    return _run(
        context,
        state["ticket_id"],
        _stage(START_REVISION, state, (section.review_rounds or 0) + 1),
        work,
        agent=revision.get("requested_by") or ORCHESTRATOR,
        department=dept,
        inputs={
            "source": revision.get("source"),
            "instruction": first_line(revision.get("instruction") or "", 300),
            "comment": revision.get("comment"),
        },
        thread=state["thread_id"],
    )


def _intake_handoff(engine: Engine, ticket_id: str) -> IntakeHandoff:
    with Session(engine) as session:
        row = session.get(RfpHandoff, UUID(ticket_id))
        if row is None:
            raise store.TicketNotFound(f"intake handoff of {ticket_id}")
        return IntakeHandoff.model_validate(row.payload)


def _assignment(
    context: ApprovalContext, state: ReviewState, section: RfpDepartmentSection
) -> dict[str, Any]:
    """The department's Part 2 assignment, plus this revision's request (and volume cap)."""
    revision = state["revision"] or {}
    rows = approval_store.load_sections(context.engine, state["ticket_id"])
    _, assignments = build_assignments(
        _intake_handoff(context.engine, state["ticket_id"]),
        {row.department_id: row.id for row in rows},
        max_iterations=context.settings.max_draft_iterations,
    )
    assignment: dict[str, Any] = dict(
        next(a for a in assignments if a["department_id"] == section.department_id)
    )
    cap = state.get("volume_cap")
    volume = assignment.get("monthly_volume")
    if cap is not None and section.department_id == "lastmile" and (volume is None or cap < volume):
        # Briefing §7: the proposal volume is capped; the guard then accepts the capped figure.
        assignment["monthly_volume"] = cap
    assignment["revision_request"] = {
        "round": revision["round"],
        "requested_by": revision.get("requested_by") or "the reviewer",
        "instruction": revision.get("instruction") or "",
        "comment": revision.get("comment"),
        "draft": (section.draft_data or {}).get("draft") or {},
    }
    return assignment


def revise_section(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    """Part 2's generator-evaluator loop again, on this section, with the reviewer's request."""
    context = runtime.context
    dept = state["department_id"]
    revision = state["revision"] or {}
    ticket_id = state["ticket_id"]
    section = approval_store.load_section(context.engine, ticket_id, dept)
    saved = approval_store.snapshot(section)
    run_id = revision["run_id"]
    loop_context = ProposalContext(
        engine=context.engine,
        agents=context.agents,
        settings=context.settings,
        active_statuses=approval_store.PART_3_ACTIVE,
    )

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        assignment = _assignment(context, state, section)
        try:
            final = get_section_graph().invoke(
                {"ticket_id": ticket_id, "run_id": run_id, "assignment": assignment},
                context=loop_context,
            )
        except StageFailed as failure:
            return {}, _after_failed_revision(context, state, section, saved, assignment, failure)
        return {}, {
            "draft_status": final["outcome"],
            "attempts": final["iteration"],
            "overall_pass": (final.get("evaluation") or {}).get("overall_pass"),
            "revision_round": revision["round"],
        }

    return _run(
        context,
        ticket_id,
        _stage(REVISE, state, revision["round"]),
        work,
        agent=f"{dept} generator and the readability, relevance and compliance evaluators",
        department=dept,
        inputs={
            "revision_round": revision["round"],
            "requested_by": revision.get("requested_by"),
            "volume_cap": state.get("volume_cap") if dept == "lastmile" else None,
        },
        thread=state["thread_id"],
    )


def _after_failed_revision(
    context: ApprovalContext,
    state: ReviewState,
    section: RfpDepartmentSection,
    saved: dict[str, Any],
    assignment: dict[str, Any],
    failure: StageFailed,
) -> dict[str, Any]:
    """A broken revision goes back to the owner: its last evaluated attempt, or the old draft."""
    revision = state["revision"] or {}
    attempt = proposal_store.latest_evaluated_attempt(
        context.engine, section.id, revision["run_id"]
    )
    if attempt is not None:
        note = (
            f"Revision {revision['round']}: attempt {attempt.iteration + 1} could not be completed "
            f"({failure.stage}: {failure.message}); this is attempt {attempt.iteration}, the last "
            "one evaluated."
        )
        proposal_store.restore_attempt(
            context.engine,
            state["ticket_id"],
            section.id,
            attempt,
            draft_data={
                "draft": attempt.draft_data,
                "guard_notes": list(attempt.guard_notes),
                "is_sla_section": assignment["is_sla_section"],
                "max_iterations": assignment["max_iterations"],
                "rate_card_version": assignment["rate_card_version"],
                "run_id": revision["run_id"],
                "revision_round": revision["round"],
                "review_note": note,
            },
            statuses=approval_store.PART_3_ACTIVE,
        )
        return {"draft_status": "needs_human_review", "failed_stage": failure.stage, "note": note}
    note = (
        f"Revision {revision['round']} could not be completed ({failure.stage}: "
        f"{failure.message}). This is the draft from before the revision."
    )
    approval_store.restore_section(context.engine, state["ticket_id"], section.id, saved, note=note)
    return {"draft_status": saved["draft_status"], "failed_stage": failure.stage, "note": note}


def open_review(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    """Persist the request, so the checkpoint before the pause holds it."""
    context = runtime.context
    settings = context.settings
    dept = state["department_id"]
    section = approval_store.load_section(context.engine, state["ticket_id"], dept)
    review_round = (section.review_rounds or 0) + 1

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        rejected = [
            d
            for d in approval_store.rejected_departments(context.engine, state["ticket_id"])
            if d != dept
        ]
        if rejected:
            reason = f"{', '.join(rejected)} was rejected, so this section needs no decision."
            return {"withdraw_reason": reason}, {"withdrawn": True, "reason": reason}
        revised = bool(state.get("revision"))
        approval_store.request_review(
            context.engine, state["ticket_id"], section.id, new_round=revised
        )
        current = approval_store.load_section(context.engine, state["ticket_id"], dept)
        request = decisions.ApprovalRequest(
            ticket_id=UUID(state["ticket_id"]),
            section_id=current.id,
            department_id=dept,  # type: ignore[arg-type]
            department_name=DEPARTMENT_BY_ID[dept].name,
            owner=current.owner,
            approver_configured=approvers.approver_email(settings, dept) is not None,
            review_round=review_round,
            revisions_used=current.review_rounds or 0,
            max_revision_rounds=settings.max_revision_rounds,
            allowed_actions=decisions.allowed_actions(
                current.review_rounds or 0, settings.max_revision_rounds
            ),
            draft_sha256=decisions.draft_sha256(current.draft_content),
            draft_status=current.draft_status,
            needs_human_review=current.draft_status == "needs_human_review",
            reason=(state.get("revision") or {}).get("instruction"),
            requested_at=store.as_utc(current.ready_at) or utcnow(),
        )
        update = {"request": request.model_dump(mode="json"), "revision": None, "refusal": None}
        output = {
            "asked": current.owner,
            "review_round": review_round,
            "allowed_actions": request.allowed_actions,
            "draft_sha256": request.draft_sha256[:16],
            "needs_human_review": request.needs_human_review,
        }
        return update, output

    return _run(
        context,
        state["ticket_id"],
        _stage(OPEN_REVIEW, state, review_round),
        work,
        agent=ORCHESTRATOR,
        department=dept,
        inputs={"revised": bool(state.get("revision")), "draft_status": section.draft_status},
        thread=state["thread_id"],
    )


def route_after_open(state: ReviewState) -> str:
    return "withdraw" if state.get("withdraw_reason") else "await"


def _response_summary(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"value": type(response).__name__}
    decider = response.get("decided_by") or {}
    comment = response.get("comment")
    return {
        "action": response.get("action"),
        "decided_by": decider.get("email") or decider.get("name")
        if isinstance(decider, dict)
        else None,
        "comment": first_line(comment, 200) if isinstance(comment, str) else None,
        "review_round": response.get("review_round"),
    }


def await_decision(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    """The human-in-the-loop pause. Only `interrupt()`: re-run on every resume, no side effect."""
    request = dict(state["request"])
    if state.get("refusal"):
        request["error"] = state["refusal"]
    response = interrupt(request)
    dept = state["department_id"]
    entry = _entry(
        node=_stage(AWAIT, state, request["review_round"]),
        agent=_owner_agent(dept),
        department=dept,
        inputs={
            "review_round": request["review_round"],
            "allowed_actions": request["allowed_actions"],
            "draft_sha256": str(request["draft_sha256"])[:16],
        },
        output=_response_summary(response),
        outcome="completed",
        started_at=request["requested_at"],
        finished_at=utcnow(),
    )
    return {"response": response, "trace": [entry]}


def validate_decision(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    """The human answer is checked again before it changes anything (`decisions.check_decision`)."""
    context = runtime.context
    dept = state["department_id"]
    request = decisions.ApprovalRequest.model_validate(state["request"])
    response = state.get("response")

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            decision = decisions.parse_decision(response)
            if decision.department_id != dept:
                raise decisions.DecisionRefused(
                    "invalid", f"This decision is for {decision.department_id}, not {dept}."
                )
            decisions.check_decision(
                settings=context.settings,
                department_id=dept,
                review_round=decision.review_round,
                draft_sha=decision.draft_sha256,
                revisions_used=request.revisions_used,
                action=decision.action,
                comment=decision.comment,
                source=decision.decided_by.source,
                decided_by_email=decision.decided_by.email,
                expected_round=request.review_round,
                expected_sha=request.draft_sha256,
            )
        except decisions.DecisionRefused as refused:
            decision_id = response.get("decision_id") if isinstance(response, dict) else None
            if decision_id:
                # A value that is not a UUID was never stored: nothing to mark.
                with contextlib.suppress(ValueError):
                    approval_store.mark_decision(
                        context.engine, str(decision_id), "refused", refused.message
                    )
            refused_update = {"refusal": refused.message, "decision": None}
            output = {"valid": False, "code": refused.code, "message": refused.message}
            return refused_update, output

        if (
            not approval_store.mark_decision(context.engine, decision.decision_id, "applied")
            and approval_store.get_decision(context.engine, decision.decision_id) is None
        ):
            # A resume from code or the CLI: the decision is logged here.
            approval_store.record_decision(
                context.engine,
                ticket_id=state["ticket_id"],
                section_id=request.section_id,
                department_id=dept,
                review_round=decision.review_round,
                draft_sha256=decision.draft_sha256,
                action=decision.action,
                comment=decisions.clean_comment(decision.comment),
                source=decision.decided_by.source,
                decided_by=decision.decided_by.email or decision.decided_by.name,
                decided_by_name=decision.decided_by.name,
                status="applied",
            )
        update: dict[str, Any] = {"decision": decision.model_dump(mode="json"), "refusal": None}
        if decision.action == "request_changes":
            update["revision"] = {
                "source": "owner",
                "requested_by": f"{request.owner} (owner of {request.department_name})",
                "instruction": (
                    f"{request.owner}, the owner of {request.department_name}, reviewed this "
                    "section and asked for changes. Apply them."
                ),
                "comment": decisions.clean_comment(decision.comment),
                "decision_ids": [str(decision.decision_id)],
            }
        output = {
            "valid": True,
            "action": decision.action,
            "decided_by": decision.decided_by.email or decision.decided_by.name,
            "comment": first_line(decision.comment or "", 200) or None,
        }
        return update, output

    return _run(
        context,
        state["ticket_id"],
        _stage(VALIDATE, state, request.review_round),
        work,
        agent=VALIDATOR,
        department=dept,
        inputs=_response_summary(response),
        thread=state["thread_id"],
    )


def route_after_validate(state: ReviewState) -> str:
    if state.get("refusal") or not state.get("decision"):
        return "refused"
    return str((state["decision"] or {})["action"])


def sign_off(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    dept = state["department_id"]
    decision = decisions.ApprovalDecision.model_validate(state["decision"])
    request = decisions.ApprovalRequest.model_validate(state["request"])

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        approval_store.sign_off(
            context.engine,
            state["ticket_id"],
            request.section_id,
            approver=request.owner,
            approved_by=decision.decided_by.email or decision.decided_by.name,
            approved_at=decision.decided_at,
        )
        output = {
            "approval_status": "approved",
            "approver": request.owner,
            "approved_at": decision.decided_at.isoformat(),
        }
        return {"outcome": "approved"}, output

    return _run(
        context,
        state["ticket_id"],
        _stage(SIGN_OFF, state, request.review_round),
        work,
        agent=_owner_agent(dept),
        department=dept,
        inputs={"decision_id": str(decision.decision_id), "review_round": request.review_round},
        thread=state["thread_id"],
    )


def record_rejection(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    dept = state["department_id"]
    decision = decisions.ApprovalDecision.model_validate(state["decision"])
    request = decisions.ApprovalRequest.model_validate(state["request"])

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        approval_store.set_section_status(
            context.engine, state["ticket_id"], request.section_id, "rejected"
        )
        output = {"approval_status": "rejected", "reason": first_line(decision.comment or "", 200)}
        return {"outcome": "rejected"}, output

    return _run(
        context,
        state["ticket_id"],
        _stage(REJECT, state, request.review_round),
        work,
        agent=_owner_agent(dept),
        department=dept,
        inputs={"decision_id": str(decision.decision_id), "review_round": request.review_round},
        thread=state["thread_id"],
    )


def withdraw(state: ReviewState, runtime: Runtime[ApprovalContext]) -> dict[str, Any]:
    context = runtime.context
    dept = state["department_id"]
    section = approval_store.load_section(context.engine, state["ticket_id"], dept)
    reason = state.get("withdraw_reason") or (state.get("decision") or {}).get("comment")

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        if state.get("withdraw_reason"):
            # Withdrawn before its owner was asked: logged here (no interrupt was answered).
            approval_store.record_decision(
                context.engine,
                ticket_id=state["ticket_id"],
                section_id=section.id,
                department_id=dept,
                review_round=(section.review_rounds or 0) + 1,
                draft_sha256=decisions.draft_sha256(section.draft_content),
                action="withdrawn",
                comment=reason,
                source="system",
                decided_by="system",
                decided_by_name="system",
                status="applied",
            )
        approval_store.set_section_status(
            context.engine, state["ticket_id"], section.id, "withdrawn"
        )
        return {"outcome": "withdrawn"}, {"approval_status": "withdrawn", "reason": reason}

    return _run(
        context,
        state["ticket_id"],
        _stage(WITHDRAW, state),
        work,
        agent="system",
        department=dept,
        inputs={"reason": reason},
        thread=state["thread_id"],
    )


# --- Building --------------------------------------------------------------------------------


ReviewGraph = CompiledStateGraph[ReviewState, ApprovalContext, ReviewState, ReviewState]
ApprovalGraph = CompiledStateGraph[ApprovalState, ApprovalContext, ApprovalState, ApprovalState]


def build_review_graph() -> ReviewGraph:
    builder = StateGraph(ReviewState, context_schema=ApprovalContext)
    builder.add_node(START_REVISION, start_revision)
    builder.add_node(REVISE, revise_section)  # no retry: it would re-run a whole loop
    builder.add_node(OPEN_REVIEW, open_review)
    builder.add_node(AWAIT, await_decision)
    builder.add_node(VALIDATE, validate_decision)
    builder.add_node(SIGN_OFF, sign_off)
    builder.add_node(REJECT, record_rejection)
    builder.add_node(WITHDRAW, withdraw)

    builder.add_conditional_edges(
        START, route_start, {"revise": START_REVISION, "review": OPEN_REVIEW}
    )
    builder.add_edge(START_REVISION, REVISE)
    builder.add_edge(REVISE, OPEN_REVIEW)
    builder.add_conditional_edges(
        OPEN_REVIEW, route_after_open, {"await": AWAIT, "withdraw": WITHDRAW}
    )
    builder.add_edge(AWAIT, VALIDATE)
    builder.add_conditional_edges(
        VALIDATE,
        route_after_validate,
        {
            "refused": AWAIT,
            "approve": SIGN_OFF,
            "request_changes": START_REVISION,
            "reject": REJECT,
            "withdrawn": WITHDRAW,
        },
    )
    for node in (SIGN_OFF, REJECT, WITHDRAW):
        builder.add_edge(node, END)
    # No checkpointer of its own: run inside `review_department`, it uses the
    # approval graph's, under the branch's own checkpoint namespace.
    return builder.compile(name=REVIEW_GRAPH_NAME)


@lru_cache(maxsize=1)
def get_review_graph() -> ReviewGraph:
    return build_review_graph()


def build_approval_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> ApprovalGraph:
    builder = StateGraph(ApprovalState, context_schema=ApprovalContext)
    builder.add_node(OPEN, open_approval)
    builder.add_node(ARBITRATE, arbitrate, destinations=(REVIEW, COMPOSE, CLOSE))
    builder.add_node(REVIEW, review_department)  # no retry: a branch holds human decisions
    builder.add_node(COMPOSE, compose_final_document)
    builder.add_node(COMPLETE, complete_approval)
    builder.add_node(CLOSE, close_rejected)

    builder.add_edge(START, OPEN)
    builder.add_edge(OPEN, ARBITRATE)
    builder.add_edge(REVIEW, ARBITRATE)
    builder.add_edge(COMPOSE, COMPLETE)
    builder.add_edge(COMPLETE, END)
    builder.add_edge(CLOSE, END)
    return builder.compile(checkpointer=checkpointer, name=GRAPH_NAME)


_graphs: dict[int, tuple[BaseCheckpointSaver[Any], ApprovalGraph]] = {}


def get_approval_graph(checkpointer: BaseCheckpointSaver[Any]) -> ApprovalGraph:
    """The approval graph compiled with this checkpointer (once per checkpointer)."""
    cached = _graphs.get(id(checkpointer))
    if cached is None or cached[0] is not checkpointer:
        cached = (checkpointer, build_approval_graph(checkpointer))
        _graphs[id(checkpointer)] = cached
    return cached[1]


def clear_graphs() -> None:
    _graphs.clear()


def draw_mermaid() -> dict[str, str]:
    """Both Part 3 graphs as Mermaid (each department's branch runs inside `review_department`)."""
    return {
        GRAPH_NAME: build_approval_graph().get_graph().draw_mermaid(),
        REVIEW_GRAPH_NAME: get_review_graph().get_graph().draw_mermaid(),
    }


def graph_version() -> str:
    """Both graphs' shape as a fingerprint, stored on each run (a later mismatch is logged)."""
    drawings = draw_mermaid()
    text = "\n".join(drawings[name] for name in sorted(drawings))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
