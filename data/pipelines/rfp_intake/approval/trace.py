"""For any ticket, which agent did what, in what order, across Parts 1, 2 and 3.

Every node of the three parts writes an `rfp_ticket_events` row (`steps.run_step`
in Parts 1 and 2, `approval.graph._run` in Part 3). Part 3 rows name their
agent, input and output in the row's `detail`. Parts 1 and 2 name the node
(`generate_section:reverse#2`), so the agent is read from the node name here.
The Part 3 graph state also carries the same entries (`trace`), next to its
checkpoints.

`ticket_trace` returns one step per event, oldest first: the part, the node,
the department and attempt or round, the agent, the outcome, the duration and
the detail. Part 2's loop also runs inside Part 3 to revise a section: those
steps come after `open_approval`, and are shown as Part 3 revision steps.

It is what the CLI (`npm run rfp -- trace <id>`), the API
(`GET /rfp/tickets/{id}/trace`) and the backoffice "Run history" show.
"""

from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlmodel import Session, col

from data.pipelines.rfp_intake.models import RfpTicketEvent
from data.pipelines.rfp_intake.store import as_utc, as_uuid

# Node -> (part, agent). `{dept}` is filled with the department of the node name.
AGENTS: Final[dict[str, tuple[int, str]]] = {
    # Part 1: intake, classification, routing
    "convert_to_markdown": (1, "PDF converter (MarkItDown)"),
    "classify_document": (1, "classifier agent"),
    "discard_ticket": (1, "intake orchestrator"),
    "measure_readability": (1, "readability metrics"),
    "extract_metadata": (1, "metadata agent"),
    "plan_departments": (1, "orchestrator agent"),
    "analyze_department": (1, "{dept} worker agent"),
    "synthesize_brief": (1, "synthesizer agent"),
    "complete_intake": (1, "intake orchestrator"),
    "reprocess": (1, "operator (CLI)"),
    # Part 2: drafting and evaluation
    "plan_assignments": (2, "assignment orchestrator"),
    "retrieve_knowledge": (2, "{dept} knowledge retrieval"),
    "generate_section": (2, "{dept} generator agent"),
    "check_readability": (2, "readability evaluator"),
    "check_relevance": (2, "relevance evaluator agent"),
    "check_compliance": (2, "compliance evaluator"),
    "merge_evaluation": (2, "evaluation merge"),
    "accept_section": (2, "generator-evaluator loop"),
    "escalate_section": (2, "generator-evaluator loop"),
    "synthesize_proposal": (2, "proposal synthesizer"),
    "complete_drafting": (2, "drafting orchestrator"),
    "redraft": (2, "operator (CLI)"),
}


def split_node(node: str) -> tuple[str, str | None, int | None]:
    """`generate_section:reverse#2` -> (`generate_section`, `reverse`, 2)."""
    name, _, rest = node.partition(":")
    dept, _, number = rest.partition("#")
    return name, dept or None, int(number) if number.isdigit() else None


def describe(node: str, detail: dict[str, Any] | None) -> dict[str, Any]:
    """The part and the agent of one event."""
    name, dept, number = split_node(node)
    if detail and detail.get("part") == 3:
        return {
            "part": 3,
            "step": name,
            "department": detail.get("department") or dept,
            "number": number,
            "agent": detail.get("agent") or "approval orchestrator",
        }
    part, agent = AGENTS.get(name, (0, name))
    return {
        "part": part,
        "step": name,
        "department": dept,
        "number": number,
        "agent": agent.format(dept=dept or "department"),
    }


def ticket_trace(engine: Engine, ticket_id: UUID | str) -> list[dict[str, Any]]:
    """Every step of the ticket across the three parts, oldest first."""
    with Session(engine) as session:
        events = list(
            session.execute(
                select(RfpTicketEvent)
                .where(col(RfpTicketEvent.ticket_id) == as_uuid(ticket_id))
                .order_by(col(RfpTicketEvent.created_at), col(RfpTicketEvent.id))
            ).scalars()
        )
    steps: list[dict[str, Any]] = []
    in_approval = False
    for position, event in enumerate(events, 1):
        detail = event.detail or {}
        described = describe(event.node, detail)
        if described["step"] == "open_approval":
            in_approval = True
        elif in_approval and described["part"] == 2:
            described = {**described, "part": 3, "agent": f"{described['agent']} (revision)"}
        steps.append(
            {
                "position": position,
                "node": event.node,
                **described,
                "outcome": event.outcome,
                "duration_ms": event.duration_ms,
                "at": as_utc(event.created_at),
                "input": detail.get("input") if detail.get("part") == 3 else None,
                "output": detail.get("output") if detail.get("part") == 3 else None,
                "detail": None if detail.get("part") == 3 else (event.detail or None),
            }
        )
    return steps
