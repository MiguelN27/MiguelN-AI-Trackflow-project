"""Merge the three evaluators into one `EvaluationResult`, and decide what happens next.

- `evaluate_readability`: the readability evaluator (no model):
  `py-readability-metrics` through `readability.measure`, on the section's
  own prose only. Tables, headings and fixed sentences would distort the
  sentence counts, and the generator cannot change them anyway.
- `compose`: the three results as the ticket's `EvaluationResult`, with
  `feedback_for_generator` written from them, deterministically.
- `decide`: accept, revise (back to the generator with the feedback) or
  escalate (`needs_human_review`: iteration limit reached, or a violation no
  rewrite can fix, such as an unknown client country).
"""

import re
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from data.pipelines.rfp_intake import readability
from data.pipelines.rfp_intake.proposal import guidelines
from data.pipelines.rfp_intake.proposal.render import prose_for_readability
from data.pipelines.rfp_intake.proposal.results import (
    ComplianceResult,
    EvaluationResult,
    ReadabilityDetails,
    ReadabilityResult,
    RelevanceResult,
)
from data.pipelines.rfp_intake.text import first_line

Decision = Literal["accept", "revise", "escalate"]

TARGET_WORDS: Final = 150
_SENTENCE: Final = re.compile(r"[^.!?]+[.!?]?")
_WORD: Final = re.compile(r"\b\w+\b", re.UNICODE)


def _sentences(prose: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.findall(prose) if _WORD.search(part)]


def evaluate_readability(
    draft: dict[str, Any], *, min_ease: float, max_grade: float
) -> ReadabilityResult:
    prose = prose_for_readability(draft)
    metrics = readability.measure(prose, language="en")
    sentences = _sentences(prose)
    longest = max(sentences, key=lambda s: len(_WORD.findall(s)), default=None)
    per_sentence = (
        round(metrics.word_count / metrics.sentence_count, 1) if metrics.sentence_count else None
    )
    scored = metrics.flesch_reading_ease is not None
    note = metrics.note
    if scored:
        passed = (
            metrics.flesch_reading_ease is not None
            and metrics.flesch_kincaid_grade is not None
            and metrics.flesch_reading_ease >= min_ease
            and metrics.flesch_kincaid_grade <= max_grade
        )
    elif metrics.word_count < readability.MIN_WORDS:
        passed = False
        note = (
            f"Too short to score: {metrics.word_count} words of prose; write at least "
            f"{TARGET_WORDS} words in the summary and narrative."
        )
    else:
        # Not the draft's fault (the NLTK data is missing): never trap it in the loop.
        passed = True
    return ReadabilityResult(
        passed=passed,
        score=metrics.flesch_reading_ease,
        details=ReadabilityDetails(
            scored=scored,
            flesch_reading_ease=metrics.flesch_reading_ease,
            flesch_kincaid_grade=metrics.flesch_kincaid_grade,
            gunning_fog=metrics.gunning_fog,
            word_count=metrics.word_count,
            sentence_count=metrics.sentence_count,
            words_per_sentence=per_sentence,
            longest_sentence=first_line(longest, 200) if longest else None,
            min_ease=min_ease,
            max_grade=max_grade,
            note=note,
        ),
    )


def feedback_for_generator(
    readability_result: ReadabilityResult,
    relevance: RelevanceResult,
    compliance: ComplianceResult,
    guard_notes: list[str],
    *,
    currency: str | None,
    country: str | None,
) -> str:
    """Concrete fixes, most important first. Empty when every check passed."""
    if readability_result.passed and relevance.passed and compliance.passed:
        return ""
    items: list[str] = []
    for violation in compliance.violations:
        fix = f"[{violation.rule_id}] {violation.message}"
        if violation.quote:
            fix += f' Rewrite or remove: "{first_line(violation.quote, 200)}".'
        items.append(f"{fix} {guidelines.remedy(violation.rule_id, currency, country)}")
    for aspect in relevance.missing_aspects:
        items.append(f"[relevance] Answer this, which the section does not: {aspect}")
    if not readability_result.passed:
        details = readability_result.details
        if not details.scored:
            items.append(f"[readability] {details.note}")
        else:
            items.append(
                f"[readability] Flesch reading ease {details.flesch_reading_ease} (needs "
                f"{details.min_ease:g} or more) and grade {details.flesch_kincaid_grade} (needs "
                f"{details.max_grade:g} or less), {details.words_per_sentence} words per sentence. "
                "Use sentences under 20 words and plain words"
                + (f'; split: "{details.longest_sentence}".' if details.longest_sentence else ".")
            )
    for note in guard_notes:
        items.append(f"[guard] {note}")
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, 1))


def compose(
    *,
    section_id: UUID | str,
    department_id: str,
    iteration: int,
    readability_result: ReadabilityResult,
    relevance: RelevanceResult,
    compliance: ComplianceResult,
    guard_notes: list[str],
    currency: str | None,
    country: str | None,
    evaluated_at: datetime,
) -> EvaluationResult:
    overall = readability_result.passed and relevance.passed and compliance.passed
    return EvaluationResult(
        section_id=UUID(str(section_id)),
        department_id=department_id,  # type: ignore[arg-type]
        iteration=iteration,
        readability=readability_result,
        relevance=relevance,
        compliance=compliance,
        overall_pass=overall,
        feedback_for_generator=feedback_for_generator(
            readability_result,
            relevance,
            compliance,
            guard_notes,
            currency=currency,
            country=country,
        ),
        evaluated_at=evaluated_at,
    )


def failed_checks(evaluation: EvaluationResult) -> list[str]:
    failed = []
    if not evaluation.readability.passed:
        failed.append("readability")
    if not evaluation.relevance.passed:
        failed.append("relevance")
    if not evaluation.compliance.passed:
        failed.append("compliance (" + ", ".join(evaluation.compliance.rule_ids) + ")")
    return failed


def decide(
    evaluation: EvaluationResult, *, iteration: int, max_iterations: int
) -> tuple[Decision, str]:
    if evaluation.overall_pass:
        return "accept", f"every check passed on attempt {iteration}"
    failed = ", ".join(failed_checks(evaluation))
    unfixable = [v.rule_id for v in evaluation.compliance.violations if not v.retryable]
    if unfixable:
        # The section can never pass (the client's country is unknown, so no currency
        # can be set): another attempt only spends tokens. A person must ask the client.
        return "escalate", (
            f"no rewrite can fix {', '.join(sorted(set(unfixable)))}; a person must decide "
            f"(fails: {failed})"
        )
    if iteration >= max_iterations:
        return "escalate", f"attempt {iteration} of {max_iterations} still fails: {failed}"
    return "revise", f"attempt {iteration} of {max_iterations} fails: {failed}"
