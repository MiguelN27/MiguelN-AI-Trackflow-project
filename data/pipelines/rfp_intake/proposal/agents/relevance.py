"""The relevance evaluator: does the section answer what the RFP (and §2.1) ask of it?

Two parts:

- Deterministic: the department's required elements from briefing §2.1
  (`sections.REQUIRED_ELEMENTS`), checked on the structured draft. A
  warehouse section without onboarding time fails whatever a model thinks.
- A model judge, one forced call (`judge_section_relevance`), goes through
  this department's requirements from the Part 1 handoff: its key aspects
  (A1, A2, ...) and what it must provide (N1, N2, ...). A requirement counts
  as covered only when the judge quotes the passage of the section that
  answers it and the quote really is in the section (layout ignored). A
  requirement the judge leaves out is asked for once more, then counts as
  missing (fail-closed).

`missing_aspects` lists what is missing, each with what to add.
"""

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID, departments_for
from data.pipelines.rfp_intake.proposal.results import RelevanceResult
from data.pipelines.rfp_intake.proposal.sections import missing_required
from data.pipelines.rfp_intake.text import appears_in, first_line, squash

TOOL_NAME: Final = "judge_section_relevance"

SYSTEM_TEMPLATE: Final = """\
You review one section of TrackFlow's pricing proposal for {client}: the {department_name} \
section. It covers: {contributes}. {others}For each numbered requirement, decide whether the \
SECTION answers it. Call judge_section_relevance once, with one verdict for every requirement id.

- covered: the section answers it. evidence_quote: the sentence or table row of the SECTION \
that answers it, copied exactly.
- not_covered: the section does not answer it. gap: what is missing, in one short sentence \
the writer can act on.
- not_applicable: this section cannot answer it: a proposal-wide matter (such as the \
proposal's due date) or another section's service. gap: why, briefly.

Judge only what the SECTION says, and judge it as a first draft for the department owner:
- A requirement that needs information the client or the department owner must still give is \
covered when the section says what it can and lists the rest under "Pending information".
- When TrackFlow's policy prices something differently from how the RFP asks (for example \
storage per cubic meter rather than per pallet or SKU), the requirement is covered if the \
section explains how it is priced instead.\
"""

# Table pipes, list markers and emphasis are layout, not words: a quote that
# drops them still quotes the section.
_LAYOUT: Final = re.compile(r"[|*`#]+")
_LIST_MARKER: Final = re.compile(r"(?m)^\s*(?:[-•]|\d{1,2}[.)])\s+")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?;])\s+")
# A quote's sentences shorter than this prove nothing on their own.
MIN_SENTENCE_CHARS: Final = 12


class AspectVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aspect_id: str
    verdict: Literal["covered", "not_covered", "not_applicable"]
    evidence_quote: str = ""
    gap: str = ""


class RelevanceJudgement(BaseModel):
    """Record, for every requirement id, whether the section answers it."""

    model_config = ConfigDict(extra="ignore")

    verdicts: list[AspectVerdict] = Field(default_factory=list)


def requirements(assignment: dict[str, Any]) -> list[dict[str, str]]:
    """This department's requirements from the handoff: key aspects, then needs."""
    found: list[dict[str, str]] = []
    for number, aspect in enumerate(assignment.get("key_aspects") or [], 1):
        found.append(
            {
                "id": f"A{number}",
                "label": squash(f"{aspect.get('aspect')}: {aspect.get('detail')}"),
            }
        )
    for number, need in enumerate(assignment.get("needed_from_department") or [], 1):
        found.append({"id": f"N{number}", "label": squash(need)})
    return found


def build_user_message(assignment: dict[str, Any], markdown: str) -> str:
    listed = "\n".join(f"{item['id']}. {item['label']}" for item in requirements(assignment))
    return (
        "REQUIREMENTS (from the client's RFP, as analysed in Part 1)\n"
        + llm.envelope("rfp_requirements", listed)
        + "\n\nSECTION\n"
        + llm.envelope("proposal_section", markdown)
    )


def _plain(text: str) -> str:
    return squash(_LAYOUT.sub(" ", _LIST_MARKER.sub("", text)))


def quoted_in(quote: str, markdown: str) -> bool:
    """True when `quote` is a passage of the section: whole, or sentence by sentence.

    Judges quote across a list's bullets or join two sentences of the section;
    every sentence of the quote must still be in it, word for word.
    """
    section = _plain(markdown)
    whole = _plain(quote)
    if appears_in(whole, section):
        return True
    parts = [part for part in _SENTENCE_END.split(whole) if len(part) >= MIN_SENTENCE_CHARS]
    return bool(parts) and all(appears_in(part, section) for part in parts)


def system_prompt(assignment: dict[str, Any]) -> str:
    dept = assignment["department_id"]
    others = [
        DEPARTMENT_BY_ID[other]
        for other in departments_for(assignment.get("services_requested") or [])
        if other != dept
    ]
    listed = "; ".join(f"{other.name} ({other.contributes.lower()})" for other in others)
    return SYSTEM_TEMPLATE.format(
        client=assignment.get("client_name") or "a prospective client",
        department_name=assignment["department_name"],
        contributes=DEPARTMENT_BY_ID[dept].contributes.lower(),
        others=f"Other sections of the same proposal cover: {listed}. " if listed else "",
    )


def finalize(
    assignment: dict[str, Any],
    draft: dict[str, Any],
    markdown: str,
    judgement: RelevanceJudgement | None,
) -> RelevanceResult:
    """The relevance result, deterministic once the judge has answered."""
    missing: list[str] = []
    covered: list[str] = []
    not_applicable: list[str] = []
    for element in missing_required(assignment["department_id"], draft):
        missing.append(f"{element.label} (briefing §2.1 requires it): {element.fix}")

    verdicts = {v.aspect_id.strip().upper(): v for v in (judgement.verdicts if judgement else [])}
    for item in requirements(assignment):
        label = first_line(item["label"], 140)
        verdict = verdicts.get(item["id"])
        if verdict is None:
            missing.append(f"{label} (the judge gave no verdict)")
        elif verdict.verdict == "covered":
            if quoted_in(verdict.evidence_quote, markdown):
                covered.append(label)
            else:
                missing.append(f"{label}: no passage of the section answers it")
        elif verdict.verdict == "not_applicable" and squash(verdict.gap):
            not_applicable.append(f"{label}: {squash(verdict.gap)}")
        else:
            gap = squash(verdict.gap) or "the section does not answer it"
            missing.append(f"{label}: {gap}")
    return RelevanceResult(
        passed=not missing,
        missing_aspects=missing,
        covered_aspects=covered,
        not_applicable=not_applicable,
    )


def _omitted(assignment: dict[str, Any], judgement: RelevanceJudgement) -> list[str]:
    given = {verdict.aspect_id.strip().upper() for verdict in judgement.verdicts}
    return [item["id"] for item in requirements(assignment) if item["id"] not in given]


def judge(assignment: dict[str, Any], draft: dict[str, Any], markdown: str) -> AgentReply:
    if not requirements(assignment):
        return AgentReply(finalize(assignment, draft, markdown, None).as_json(), llm.NO_USAGE)
    system = system_prompt(assignment)
    user = build_user_message(assignment, markdown)
    judgement, usage = llm.call_tool(
        system=system, user=user, tool_name=TOOL_NAME, output=RelevanceJudgement
    )
    omitted = _omitted(assignment, judgement)
    if omitted:
        # An incomplete reply is the judge's slip, not the draft's: ask once more for
        # what it left out. Whatever is still missing then counts as not answered.
        again, more = llm.call_tool(
            system=system,
            user=f"{user}\n\nGive a verdict for these requirement ids only: {', '.join(omitted)}.",
            tool_name=TOOL_NAME,
            output=RelevanceJudgement,
        )
        judgement = RelevanceJudgement(verdicts=[*judgement.verdicts, *again.verdicts])
        usage = llm.Usage(
            prompt_tokens=usage.prompt_tokens + more.prompt_tokens,
            completion_tokens=usage.completion_tokens + more.completion_tokens,
        )
    return AgentReply(finalize(assignment, draft, markdown, judgement).as_json(), usage)
