"""The structured evaluation of a proposal section (the ticket's `EvaluationResult`).

    EvaluationResult:
      section_id / department_id
      readability: { pass, score, details }
      relevance:   { pass, missing_aspects[] }
      compliance:  { pass, rule_ids[], violations[] }
      overall_pass: bool
      feedback_for_generator: string

`pass` is a Python keyword, so the field is `passed` with the alias `pass`:
the stored and served JSON uses the ticket's names. `compliance.rule_ids`
are the rules the section *violates*; `checked_rule_ids` are the ones that
applied to it. Extra fields add detail without changing that shape.
"""

from datetime import datetime
from typing import Any, Final, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake.briefing import DepartmentId

RuleId = Literal[
    "currency-by-country",
    "on-time-sla-stated",
    "returns-min-48h",
    "volume-discount-table",
    "no-carrier-rates",
]
RULE_IDS: Final[tuple[str, ...]] = get_args(RuleId)

# The ticket's field name is `pass`, a Python keyword. Separate validation and
# serialization aliases keep `passed=` as the constructor's name for type checkers.
PASS: Final = Field(validation_alias="pass", serialization_alias="pass")


class _Result(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ReadabilityDetails(_Result):
    scored: bool = Field(description="False when the scores could not be computed")
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None
    gunning_fog: float | None
    word_count: int
    sentence_count: int
    words_per_sentence: float | None
    longest_sentence: str | None
    min_ease: float
    max_grade: float
    note: str | None = None


class ReadabilityResult(_Result):
    passed: bool = PASS
    score: float | None = Field(description="Flesch reading ease of the section's prose")
    details: ReadabilityDetails


class RelevanceResult(_Result):
    passed: bool = PASS
    missing_aspects: list[str] = Field(
        description="What the RFP or the briefing asks this section for and it does not answer"
    )
    covered_aspects: list[str] = Field(default_factory=list)
    not_applicable: list[str] = Field(default_factory=list)


class Violation(_Result):
    rule_id: RuleId
    message: str
    quote: str | None = Field(default=None, description="The offending passage, verbatim")
    source: Literal["check", "reviewer"] = "check"
    # False when no rewrite can fix it (the client's country is unknown).
    retryable: bool = True


class ComplianceResult(_Result):
    passed: bool = PASS
    rule_ids: list[RuleId] = Field(description="The rules this section violates")
    violations: list[Violation]
    checked_rule_ids: list[RuleId] = Field(default_factory=list)
    reviewer: Literal["used", "off", "unavailable"] = "off"


class EvaluationResult(_Result):
    section_id: UUID
    department_id: DepartmentId
    iteration: int = Field(ge=1)
    readability: ReadabilityResult
    relevance: RelevanceResult
    compliance: ComplianceResult
    overall_pass: bool
    feedback_for_generator: str = Field(
        description="Concrete, actionable fixes for the next draft; empty when it passed"
    )
    evaluated_at: datetime
