"""The orchestrator: one subtask per department the requested scope needs.

Orchestrator-worker-synthesizer (ticket: "Department orchestration"). The
orchestrator reads the whole converted RFP once and decomposes it: for each
department it writes an objective and copies the passages that department
needs. The workers then see only those passages plus the shared metadata.

Two agents look at the scope, and they must agree. The departments are checked
against the requested services the metadata extractor backed with quotes
(briefing §2.1: "Not every RFP needs all three departments"): a department the
client did not ask for is dropped, a requested one the orchestrator forgot is
added with the briefing's description. Every correction is recorded. An
extract that is not verbatim in the document is dropped, so a worker can never
be handed text the client did not write.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.briefing import (
    DEPARTMENT_BY_ID,
    DEPARTMENTS,
    SERVICE_LABELS,
    departments_for,
)
from data.pipelines.rfp_intake.text import appears_in, clean_quote, first_line

TOOL_NAME: Final = "plan_department_work"
MAX_EXTRACTS: Final = 8

_DEPARTMENT_LINES: Final = "\n".join(
    f"- {dept.id}: {dept.name} (owner {dept.owner}). Contributes: {dept.contributes}. "
    f"Needed when the client requests {SERVICE_LABELS[dept.service].lower()}."
    for dept in DEPARTMENTS
)

PROMPT: Final = f"""\
You are the orchestrator of TrackFlow's RFP intake. Split the analysis of this client \
RFP into one subtask per TrackFlow department that the requested scope needs, so each \
department analyses only its own part. Call plan_department_work once.

The departments - use exactly these ids:
{_DEPARTMENT_LINES}

Not every RFP needs all three departments. Include a department only if the client \
requests its service; a service the client explicitly excludes gets no subtask.

For each subtask:
- objective: one or two sentences, in English, on what this department must work out \
for the proposal.
- relevant_extracts: the passages of the DOCUMENT this department needs, copied exactly \
(at most about 40 words each, at most {MAX_EXTRACTS}). Include general context a \
department needs when it bears on its work, such as the client's location, volumes or \
deadline.

unassigned_requirements: requirements in the RFP that none of the three departments \
covers (for example, an IT integration), one short English sentence each.\
"""


class DepartmentSubtask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Checked against the briefing after the call, so one bad id drops one
    # subtask instead of invalidating the whole reply.
    department_id: str
    objective: str = ""
    relevant_extracts: list[str] = Field(default_factory=list)


class DepartmentPlan(BaseModel):
    """Split the RFP into one subtask per department its requested scope needs."""

    model_config = ConfigDict(extra="ignore")

    subtasks: list[DepartmentSubtask] = Field(default_factory=list)
    unassigned_requirements: list[str] = Field(default_factory=list)


def describe_metadata(metadata: dict[str, Any]) -> str:
    """The shared metadata as the orchestrator and the workers read it."""
    services = ", ".join(SERVICE_LABELS[s] for s in metadata.get("services_requested") or [])
    volume = metadata.get("monthly_volume")
    country = metadata.get("client_country")
    lines = [
        f"Client: {metadata.get('client_name') or 'not stated'}",
        f"Country: {country or 'not stated'}; currency: {metadata.get('currency') or 'not set'}",
        f"Services requested: {services or 'none stated'}",
        "Monthly order volume: "
        + (f"{volume} orders/month" if volume is not None else "not stated in the RFP"),
        "Deadline: "
        + (
            metadata.get("deadline")
            or (
                f'"{metadata["deadline_text"]}" (no full date)'
                if metadata.get("deadline_text")
                else "not stated"
            )
        ),
        f"Budget: {metadata.get('budget_range') or 'not stated'}",
    ]
    return "\n".join(lines)


def build_user_message(markdown: str, metadata: dict[str, Any]) -> str:
    return (
        f"METADATA (extracted by TrackFlow)\n{describe_metadata(metadata)}\n\n"
        f"DOCUMENT\n{llm.envelope('rfp_document', markdown)}"
    )


def default_objective(department_id: str) -> str:
    dept = DEPARTMENT_BY_ID[department_id]
    return f"Work out what {dept.name} must quote for this client: {dept.contributes.lower()}."


def reconcile(markdown: str, metadata: dict[str, Any], plan: DepartmentPlan) -> dict[str, Any]:
    """The validated plan, deterministic. JSON-mode, subtasks in the briefing's order."""
    notes: list[str] = []
    required = departments_for(metadata.get("services_requested") or [])
    proposed: dict[str, dict[str, Any]] = {}

    for subtask in plan.subtasks:
        dept_id = subtask.department_id.strip().lower()
        if dept_id not in DEPARTMENT_BY_ID:
            notes.append(f"subtask for unknown department {first_line(dept_id, 40)!r} dropped")
            continue
        if dept_id not in required:
            notes.append(
                f"{dept_id} dropped: the client did not request "
                f"{SERVICE_LABELS[DEPARTMENT_BY_ID[dept_id].service].lower()}"
            )
            continue
        entry = proposed.setdefault(dept_id, {"objective": "", "extracts": []})
        entry["objective"] = entry["objective"] or subtask.objective.strip()
        for raw in subtask.relevant_extracts:
            extract = clean_quote(raw)
            if not extract or extract in entry["extracts"]:
                continue
            if appears_in(raw, markdown):
                entry["extracts"].append(extract)
            else:
                notes.append(
                    f"{dept_id}: extract not found in the document dropped "
                    f"({first_line(extract, 60)!r})"
                )

    subtasks = []
    for dept_id in required:
        if dept_id not in proposed:
            notes.append(f"{dept_id} added: the client requested its service")
        entry = proposed.get(dept_id) or {"objective": "", "extracts": []}
        subtasks.append(
            {
                "department_id": dept_id,
                "objective": entry["objective"] or default_objective(dept_id),
                "extracts": entry["extracts"][:MAX_EXTRACTS],
            }
        )

    unassigned = [item.strip() for item in plan.unassigned_requirements if item.strip()][:6]
    return {"subtasks": subtasks, "unassigned_requirements": unassigned, "notes": notes}


def plan_departments(markdown: str, metadata: dict[str, Any]) -> AgentReply:
    plan, usage = llm.call_tool(
        system=PROMPT,
        user=build_user_message(markdown, metadata),
        tool_name=TOOL_NAME,
        output=DepartmentPlan,
    )
    return AgentReply(reconcile(markdown, metadata, plan), usage)
