"""The department workers: what each department needs to know and provide.

One worker per active department, run in parallel by the graph (`Send`). A
worker receives the shared metadata and *its own* extracts - never the whole
document, never another department's extracts (briefing §2.3). Its
`key_aspects` are stored per department in PostgreSQL.

"Never invent pallet/SKU/order volumes not present in the RFP" (§2.3) is
enforced after the call: a key aspect must quote one of the worker's sources,
and any figure in an aspect, a need or a question must appear in them. What
fails is dropped and noted. When the monthly volume is missing, the
departments whose cost depends on it always get the open question.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.agents.orchestrator import describe_metadata
from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID
from data.pipelines.rfp_intake.text import (
    appears_in,
    clean_quote,
    first_line,
    squash,
    unsupported_figures,
)

TOOL_NAME: Final = "record_department_analysis"
MAX_ITEMS: Final = 8

# Departments whose quote cannot be priced without the monthly order volume.
VOLUME_DEPENDENT: Final = frozenset({"warehouse", "lastmile"})
MISSING_VOLUME_QUESTION: Final = (
    "What monthly order volume should we plan for? The RFP does not state it."
)

PROMPT_TEMPLATE: Final = """\
You are the {name} analyst at TrackFlow (department owner: {owner}). For a client \
proposal your department contributes: {contributes}. Call record_department_analysis once.

From the shared METADATA and your department's EXTRACTS of the client's RFP - never \
from anything else - record what your department needs to know and provide:
- key_aspects: the client's requirements that matter to your department. aspect: a \
short label. detail: the requirement in English, with any figure exactly as the extracts \
or metadata state it. source_quote: the passage of the extracts or metadata it comes \
from, copied exactly.
- needed_from_department: what {owner}'s team must provide for this proposal, specific \
to this client (for example: {contributes_lower}).
- open_questions: information your department needs that the RFP does not give.

Never invent or estimate volumes, pallet or SKU counts, prices, dates or any other \
figure that is not in the extracts or metadata: when one is missing, ask for it in \
open_questions. Write in English.\
"""


class KeyAspect(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aspect: str
    detail: str
    source_quote: str = ""


class DepartmentAnalysis(BaseModel):
    """Record what this department needs to know and provide for the proposal."""

    model_config = ConfigDict(extra="ignore")

    key_aspects: list[KeyAspect] = Field(default_factory=list)
    needed_from_department: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def system_prompt(department_id: str) -> str:
    dept = DEPARTMENT_BY_ID[department_id]
    return PROMPT_TEMPLATE.format(
        name=dept.name,
        owner=dept.owner,
        contributes=dept.contributes,
        contributes_lower=dept.contributes.lower(),
    )


def build_user_message(task: dict[str, Any]) -> str:
    extracts = task.get("extracts") or []
    numbered = "\n".join(f"{n}. {extract}" for n, extract in enumerate(extracts, 1))
    return (
        f"METADATA (extracted by TrackFlow)\n{describe_metadata(task['metadata'])}\n\n"
        f"OBJECTIVE\n{task.get('objective') or ''}\n\n"
        "EXTRACTS\n"
        + llm.envelope(
            "rfp_extracts", numbered or "(no passage of the RFP concerns this department)"
        )
    )


def _items(values: list[str], sources: list[str], label: str, notes: list[str]) -> list[str]:
    kept: list[str] = []
    for value in values:
        value = squash(value)
        if not value or value in kept:
            continue
        invented = unsupported_figures(value, sources)
        if invented:
            notes.append(
                f"{label} dropped: {', '.join(invented)} is not in the RFP "
                f"({first_line(value, 60)!r})"
            )
            continue
        kept.append(value)
    return kept[:MAX_ITEMS]


def check(task: dict[str, Any], analysis: DepartmentAnalysis) -> dict[str, Any]:
    """The stored section for `analysis`, deterministic. JSON-mode."""
    dept = DEPARTMENT_BY_ID[task["department_id"]]
    metadata = task["metadata"]
    extracts: list[str] = task.get("extracts") or []
    sources = [*extracts, describe_metadata(metadata)]
    quotable = "\n".join(sources)
    notes: list[str] = []

    key_aspects: list[dict[str, str]] = []
    for item in analysis.key_aspects:
        label = first_line(item.aspect, 60)
        if not appears_in(item.source_quote, quotable):
            notes.append(f"key aspect {label!r} dropped: its source quote is not in the extracts")
            continue
        invented = unsupported_figures(f"{item.aspect} {item.detail}", sources)
        if invented:
            notes.append(f"key aspect {label!r} dropped: {', '.join(invented)} is not in the RFP")
            continue
        key_aspects.append(
            {
                "aspect": squash(item.aspect),
                "detail": squash(item.detail),
                "source_quote": clean_quote(item.source_quote),
            }
        )

    needed = _items(analysis.needed_from_department, sources, "need", notes)
    questions = _items(analysis.open_questions, sources, "open question", notes)
    if (
        dept.id in VOLUME_DEPENDENT
        and metadata.get("monthly_volume") is None
        and not any("volume" in question.lower() for question in questions)
    ):
        questions.insert(0, MISSING_VOLUME_QUESTION)

    return {
        "department_id": dept.id,
        "department_name": dept.name,
        "owner": dept.owner,
        "objective": task.get("objective") or "",
        "extracts": extracts,
        "key_aspects": key_aspects[: MAX_ITEMS + 2],
        "needed_from_department": needed,
        "open_questions": questions[:MAX_ITEMS],
        "validation_notes": notes,
    }


def analyze_department(task: dict[str, Any]) -> AgentReply:
    analysis, usage = llm.call_tool(
        system=system_prompt(task["department_id"]),
        user=build_user_message(task),
        tool_name=TOOL_NAME,
        output=DepartmentAnalysis,
    )
    return AgentReply(check(task, analysis), usage)
