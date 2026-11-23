"""The compliance evaluator: the section against the company guidelines (briefing §5).

The deterministic checks in `guidelines.py` are the authority. An optional
model reviewer (`report_guideline_violations`, `RFP_COMPLIANCE_REVIEWER`)
reads the section for what patterns miss, such as a paraphrased fast-returns
promise. It can only *add* a violation, and only:
- of a rule that applies to this section;
- quoting a passage that is really in the section;
- that the checks have not already reported.

If the reviewer is unavailable, the deterministic result stands and records
`reviewer: unavailable`.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.briefing import COUNTRY_NAMES
from data.pipelines.rfp_intake.proposal import guidelines
from data.pipelines.rfp_intake.proposal.results import RULE_IDS, ComplianceResult, Violation
from data.pipelines.rfp_intake.text import appears_in, clean_quote, comparable, squash

TOOL_NAME: Final = "report_guideline_violations"

SYSTEM_TEMPLATE: Final = """\
You check one section of TrackFlow's pricing proposal against the company guidelines below. \
Call report_guideline_violations once. Report only clear violations: for each, the rule_id \
and the exact passage of the SECTION that breaks it, copied exactly. Report an empty list \
when the section complies.

Guidelines:
{rules}

Facts for this section: {facts}
Naming carriers by destination is allowed; pairing a carrier with a rate, a discount or \
what TrackFlow pays it is not. A return window (days a customer has to request a return) \
is not a processing time.\
"""


class ReportedViolation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_id: str
    quote: str
    explanation: str = ""


class ComplianceReview(BaseModel):
    """Report the guideline violations in the section; an empty list when there are none."""

    model_config = ConfigDict(extra="ignore")

    violations: list[ReportedViolation] = Field(default_factory=list)


def system_prompt(assignment: dict[str, Any], checked: list[str]) -> str:
    rules = "\n".join(
        f"- [{rule.rule_id}] {rule.briefing_text}"
        for rule in guidelines.GUIDELINES
        if rule.rule_id in checked
    )
    country = assignment.get("country")
    facts = (
        f"the client is in {COUNTRY_NAMES.get(country, country)}, so every price must be in "
        f"{assignment.get('currency')}."
        if country
        else "the client's country is unknown."
    )
    if assignment.get("is_sla_section"):
        facts += " This section must state the on-time delivery SLA (%)."
    return SYSTEM_TEMPLATE.format(rules=rules, facts=facts)


def evaluate_rules(
    assignment: dict[str, Any], draft: dict[str, Any], markdown: str
) -> ComplianceResult:
    """The deterministic result: every applicable §5 rule checked on the rendered section."""
    violations, checked = guidelines.check_section(
        draft,
        markdown,
        currency=assignment.get("currency"),
        is_sla_section=bool(assignment.get("is_sla_section")),
    )
    return _result(violations, checked, "off")


def _result(violations: list[Violation], checked: list[Any], reviewer: str) -> ComplianceResult:
    violated = {violation.rule_id for violation in violations}
    return ComplianceResult(
        passed=not violations,
        rule_ids=[rule_id for rule_id in RULE_IDS if rule_id in violated],  # type: ignore[misc]
        violations=violations,
        checked_rule_ids=checked,
        reviewer=reviewer,  # type: ignore[arg-type]
    )


def merge_review(
    base: ComplianceResult, markdown: str, review: ComplianceReview
) -> list[Violation]:
    """The reviewer's violations that may be added to `base`."""
    known = {(v.rule_id, comparable(v.quote or "")) for v in base.violations}
    added: list[Violation] = []
    for reported in review.violations:
        rule_id = reported.rule_id.strip()
        quote = clean_quote(reported.quote)
        if rule_id not in base.checked_rule_ids or not appears_in(quote, markdown):
            continue
        key = (rule_id, comparable(quote))
        if key in known or any(rid == rule_id and comparable(quote) in q for rid, q in known):
            continue
        known.add(key)
        added.append(
            Violation(
                rule_id=rule_id,  # type: ignore[arg-type]
                message=squash(reported.explanation)
                or "The reviewer found this passage breaks the guideline.",
                quote=quote,
                source="reviewer",
            )
        )
    return added


def evaluate(
    assignment: dict[str, Any],
    draft: dict[str, Any],
    markdown: str,
    use_reviewer: bool = True,
) -> AgentReply:
    base = evaluate_rules(assignment, draft, markdown)
    if not use_reviewer:
        return AgentReply(base.as_json(), llm.NO_USAGE)
    try:
        review, usage = llm.call_tool(
            system=system_prompt(assignment, list(base.checked_rule_ids)),
            user="SECTION\n" + llm.envelope("proposal_section", markdown),
            tool_name=TOOL_NAME,
            output=ComplianceReview,
        )
    except llm.IntakeModelError:
        return AgentReply(
            _result(base.violations, base.checked_rule_ids, "unavailable").as_json(), llm.NO_USAGE
        )
    added = merge_review(base, markdown, review)
    return AgentReply(
        _result([*base.violations, *added], base.checked_rule_ids, "used").as_json(), usage
    )
