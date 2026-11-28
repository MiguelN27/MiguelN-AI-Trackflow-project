"""The synthesizer: one Sales-facing brief of what to ask whom.

Acceptance criterion: "Sales should be able to look at the result of a
processed RFP and know, without reading the original document, what's needed
from each department and who to ask." The synthesizer reads the metadata and
the department sections (not the document) and writes the summary, one ask per
department, the questions for the client and the risks.

Who to ask is never the model's choice: the owners come from the briefing
(§2.1), and every active department gets an ask even if the model skips one.
Text with a figure that is in none of the sources is dropped (a summary with
one is replaced by a plain summary built from the metadata).
"""

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.agents.orchestrator import describe_metadata
from data.pipelines.rfp_intake.briefing import (
    COMMERCIAL_DIRECTOR,
    COUNTRY_NAMES,
    DEPARTMENT_BY_ID,
    DEPARTMENT_IDS,
    SERVICE_LABELS,
)
from data.pipelines.rfp_intake.text import first_line, squash, unsupported_figures

TOOL_NAME: Final = "write_sales_brief"
MAX_CLIENT_QUESTIONS: Final = 8
MAX_RISKS: Final = 5

PROMPT: Final = f"""\
You write the intake brief of a client RFP for TrackFlow's Sales team (Commercial \
Director {COMMERCIAL_DIRECTOR}), so Sales knows - without reading the RFP - what is \
needed from each department and who to ask. Use only the METADATA and the DEPARTMENT \
ANALYSES below; never add figures, prices, dates or commitments that are not in them. \
Call write_sales_brief once.

- summary: two to four sentences in English: who the client is, what they ask for, \
where, and by when.
- department_asks: one for each department listed in the analyses: what Sales should \
ask that department's owner to provide for this client, in one or two sentences.
- client_questions: what Sales must ask the client before the proposal can be priced \
(information the RFP does not give), at most six. Do not repeat a question listed under \
ALREADY ASKED, even in other words.
- risks: up to four short points that put the proposal at risk, such as a tight \
deadline, missing volumes or requirements no department covers.\
"""


class DepartmentAsk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    department_id: str
    ask: str


class SalesBrief(BaseModel):
    """Write the Sales-facing brief: what to ask each department's owner, and the client."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    department_asks: list[DepartmentAsk] = Field(default_factory=list)
    client_questions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def _ordered(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(sections, key=lambda section: DEPARTMENT_IDS.index(section["department_id"]))


def describe_section(section: dict[str, Any]) -> str:
    lines = [f"{section['department_name']} ({section['department_id']}), owner {section['owner']}"]
    lines += [
        f"- Key aspect: {aspect['aspect']}: {aspect['detail']}"
        for aspect in section.get("key_aspects", [])
    ]
    lines += [f"- Needed: {need}" for need in section.get("needed_from_department", [])]
    lines += [f"- Open question: {question}" for question in section.get("open_questions", [])]
    return "\n".join(lines)


def build_user_message(
    metadata: dict[str, Any], sections: list[dict[str, Any]], plan: dict[str, Any]
) -> str:
    analyses = "\n\n".join(describe_section(section) for section in _ordered(sections))
    unassigned = plan.get("unassigned_requirements") or []
    extra = (
        "\n\nREQUIREMENTS NO DEPARTMENT COVERS\n" + "\n".join(f"- {item}" for item in unassigned)
        if unassigned
        else ""
    )
    asked = metadata.get("client_questions") or []
    already = (
        "\n\nALREADY ASKED (questions for the client TrackFlow already has)\n"
        + "\n".join(f"- {question}" for question in asked)
        if asked
        else ""
    )
    return (
        f"METADATA\n{describe_metadata(metadata)}\n\n"
        f"DEPARTMENT ANALYSES\n{llm.envelope('department_analyses', analyses)}{extra}{already}"
    )


def plain_summary(metadata: dict[str, Any]) -> str:
    """A summary built from the metadata alone, used when the model's has to go."""
    client = metadata.get("client_name") or "An unnamed client"
    country = COUNTRY_NAMES.get(metadata.get("client_country") or "", "an unstated country")
    services = [SERVICE_LABELS[s].lower() for s in metadata.get("services_requested") or []]
    asked = ", ".join(services) if services else "logistics services"
    deadline = metadata.get("deadline") or metadata.get("deadline_text")
    when = f" Deadline: {deadline}." if deadline else ""
    return f"{client} requests {asked} in {country}.{when}"


def _sources(
    metadata: dict[str, Any], sections: list[dict[str, Any]], plan: dict[str, Any]
) -> list[str]:
    sources = [describe_metadata(metadata), *(plan.get("unassigned_requirements") or [])]
    for section in sections:
        sources.append(describe_section(section))
        sources += [aspect.get("source_quote", "") for aspect in section.get("key_aspects", [])]
        sources += section.get("extracts", [])
    return sources


def _department_of(label: str) -> str | None:
    """The department id a model wrote as an id, a department name or an owner."""
    wanted = label.strip().casefold()
    for dept in DEPARTMENT_BY_ID.values():
        if wanted == dept.id or dept.name.casefold() in wanted or dept.owner.casefold() in wanted:
            return dept.id
    for dept in DEPARTMENT_BY_ID.values():
        if re.search(rf"\b{dept.id}\b", wanted):
            return dept.id
    return None


def finalize(
    metadata: dict[str, Any],
    sections: list[dict[str, Any]],
    plan: dict[str, Any],
    brief: SalesBrief,
) -> dict[str, Any]:
    """The stored brief, deterministic. JSON-mode."""
    sources = _sources(metadata, sections, plan)
    notes: list[str] = []

    def supported(text: str, label: str) -> bool:
        invented = unsupported_figures(text, sources)
        if invented:
            notes.append(f"{label} dropped: {', '.join(invented)} is not in the RFP")
        return not invented

    summary = brief.summary.strip()
    if not summary or not supported(summary, "summary"):
        summary = plain_summary(metadata)

    asks = {
        _department_of(ask.department_id): squash(ask.ask)
        for ask in brief.department_asks
        if ask.ask.strip() and _department_of(ask.department_id)
    }
    departments = []
    for section in _ordered(sections):
        dept = DEPARTMENT_BY_ID[section["department_id"]]
        ask = asks.get(dept.id, "")
        if not ask or not supported(ask, f"{dept.id} ask"):
            needs = section.get("needed_from_department") or [dept.contributes]
            ask = "Please provide: " + "; ".join(need.rstrip(" .") for need in needs) + "."
        departments.append(
            {
                "department_id": dept.id,
                "department_name": dept.name,
                "owner": dept.owner,
                "ask": ask,
                "needed_from_department": section.get("needed_from_department", []),
                "open_questions": section.get("open_questions", []),
            }
        )

    client_questions: list[str] = []
    for question in [*(metadata.get("client_questions") or []), *brief.client_questions]:
        question = squash(question)
        if question and question not in client_questions and supported(question, "question"):
            client_questions.append(question)

    risks = [
        risk.strip()
        for risk in brief.risks
        if risk.strip() and supported(risk, f"risk {first_line(risk, 40)!r}")
    ]

    return {
        "summary": summary,
        "client_name": metadata.get("client_name"),
        "client_country": metadata.get("client_country"),
        "currency": metadata.get("currency"),
        "commercial_owner": COMMERCIAL_DIRECTOR,
        "departments": departments,
        "client_questions": client_questions[:MAX_CLIENT_QUESTIONS],
        "risks": risks[:MAX_RISKS],
        "unassigned_requirements": plan.get("unassigned_requirements") or [],
        "validation_notes": notes,
    }


def synthesize_brief(
    metadata: dict[str, Any], sections: list[dict[str, Any]], plan: dict[str, Any]
) -> AgentReply:
    brief, usage = llm.call_tool(
        system=PROMPT,
        user=build_user_message(metadata, sections, plan),
        tool_name=TOOL_NAME,
        output=SalesBrief,
    )
    return AgentReply(finalize(metadata, sections, plan, brief), usage)
