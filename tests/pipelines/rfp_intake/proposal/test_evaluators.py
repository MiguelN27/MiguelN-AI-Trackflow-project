"""The evaluator agents (readability, relevance, compliance) and how their results merge."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from data.pipelines.rfp_intake import handoff, readability
from data.pipelines.rfp_intake.proposal import evaluation
from data.pipelines.rfp_intake.proposal.agents import compliance, generators, relevance
from data.pipelines.rfp_intake.proposal.render import render_section
from data.pipelines.rfp_intake.proposal.results import (
    ComplianceResult,
    RelevanceResult,
    Violation,
)
from tests.pipelines.rfp_intake.proposal import support

MIN_EASE, MAX_GRADE = 30.0, 14.0

CONVOLUTED = (
    "Notwithstanding the aforementioned considerations regarding the multidimensional "
    "operationalization of comprehensive logistical infrastructure, TrackFlow's "
    "organizational capabilities encompass sophisticated warehousing methodologies, "
    "incorporating synchronized inventory-management functionalities, comprehensive "
    "fulfillment orchestration, and administratively harmonized replenishment "
    "documentation, thereby facilitating considerably accelerated commercialization "
    "opportunities for internationally distributed e-commerce organizations."
)


@pytest.fixture
def luna(engine, tmp_path):
    return handoff.load_handoff(engine, support.intake_complete(engine, tmp_path, "informal"))


def guarded(assignment, draft_model):
    draft = generators.check(assignment, support.kb_for(assignment), draft_model)["draft"]
    return draft, render_section(assignment, draft)


# --- Readability --------------------------------------------------------------------------


def test_plain_prose_passes_readability():
    draft = support.warehouse_draft("USD", "US", sla=False).model_dump()

    result = evaluation.evaluate_readability(draft, min_ease=MIN_EASE, max_grade=MAX_GRADE)

    assert result.passed is True
    assert result.score is not None and result.score >= MIN_EASE
    assert result.details.word_count >= readability.MIN_WORDS
    assert result.as_json()["pass"] is True


def test_convoluted_prose_fails_readability_with_the_sentence_to_split():
    draft = support.warehouse_draft("USD", "US", sla=False).model_dump()
    draft["narrative"] = [CONVOLUTED] * 3

    result = evaluation.evaluate_readability(draft, min_ease=MIN_EASE, max_grade=MAX_GRADE)

    assert result.passed is False
    assert result.details.flesch_kincaid_grade is not None
    assert result.details.flesch_kincaid_grade > MAX_GRADE
    assert result.details.longest_sentence.startswith("Notwithstanding")


def test_a_section_too_short_to_score_fails_and_says_how_long_to_write():
    result = evaluation.evaluate_readability(
        {"summary": "We store your stock.", "narrative": ["We ship it fast."]},
        min_ease=MIN_EASE,
        max_grade=MAX_GRADE,
    )

    assert result.passed is False
    assert result.details.scored is False
    assert "at least 150 words" in result.details.note


def test_missing_tokenizer_data_never_traps_a_draft(monkeypatch):
    real = readability.measure

    def without_scores(text, *, language=None):
        metrics = real(text, language=language)
        return readability.ReadabilityMetrics(
            **{
                **metrics.as_dict(),
                "flesch_reading_ease": None,
                "flesch_kincaid_grade": None,
                "gunning_fog": None,
                "note": "Scores unavailable: NLTK tokenizer data is not installed.",
            }
        )

    monkeypatch.setattr(readability, "measure", without_scores)
    draft = support.warehouse_draft("USD", "US", sla=False).model_dump()

    result = evaluation.evaluate_readability(draft, min_ease=MIN_EASE, max_grade=MAX_GRADE)

    assert result.passed is True and result.details.scored is False


# --- Relevance ----------------------------------------------------------------------------


def verdicts(assignment, verdict="covered", quote=""):
    return {
        "verdicts": [
            {"aspect_id": item["id"], "verdict": verdict, "evidence_quote": quote}
            for item in relevance.requirements(assignment)
        ]
    }


def test_a_section_that_answers_its_requirements_is_relevant(model, luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft, markdown = guarded(assignment, support.lastmile_draft("USD", "US", volume=5000))
    fake = model(verdicts(assignment, quote="We choose the carrier for each parcel"))

    result = RelevanceResult.model_validate(relevance.judge(assignment, draft, markdown).result)

    assert result.passed is True and result.missing_aspects == []
    assert "<<untrusted kind=proposal_section>>" in fake.user
    assert "A1." in fake.user and "N1." in fake.user


def test_a_section_without_its_cost_per_shipment_fails_relevance(model, luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft_model = support.lastmile_draft("USD", "US", volume=5000)
    draft_model.price_lines = []
    draft, markdown = guarded(assignment, draft_model)
    model(verdicts(assignment, quote="We choose the carrier for each parcel"))

    result = RelevanceResult.model_validate(relevance.judge(assignment, draft, markdown).result)

    assert result.passed is False
    assert any(item.startswith("Cost per shipment") for item in result.missing_aspects)


def test_a_covered_verdict_without_a_real_quote_counts_as_missing(model, luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft, markdown = guarded(assignment, support.lastmile_draft("USD", "US", volume=5000))
    model(verdicts(assignment, quote="We deliver everything overnight for free."))

    result = RelevanceResult.model_validate(relevance.judge(assignment, draft, markdown).result)

    assert result.passed is False
    assert len(result.missing_aspects) == len(relevance.requirements(assignment))


def test_requirements_the_judge_leaves_out_are_missing_and_not_applicable_needs_a_reason(luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft, markdown = guarded(assignment, support.lastmile_draft("USD", "US", volume=5000))
    judgement = relevance.RelevanceJudgement(
        verdicts=[relevance.AspectVerdict(aspect_id="A1", verdict="not_applicable", gap="dates")]
    )

    result = relevance.finalize(assignment, draft, markdown, judgement)

    assert result.not_applicable and result.not_applicable[0].endswith(": dates")
    assert any("no verdict" in item for item in result.missing_aspects)


# --- Compliance: the reviewer can only add ------------------------------------------------


@pytest.fixture
def reverse_case(engine, tmp_path):
    intake = handoff.load_handoff(engine, support.intake_complete(engine, tmp_path, "formal"))
    assignment = support.assignment_for(intake, "reverse")
    draft, markdown = guarded(assignment, support.reverse_draft("EUR"))
    return assignment, draft, markdown


def test_the_reviewer_adds_a_paraphrased_violation_with_a_real_quote(model, reverse_case):
    assignment, draft, markdown = reverse_case
    model(
        {
            "violations": [
                {
                    "rule_id": "returns-min-48h",
                    "quote": "Items fit for resale go back on the shelf.",
                    "explanation": "Implies an immediate restock.",
                },
                {"rule_id": "returns-min-48h", "quote": "Returns in one hour, guaranteed."},
                {"rule_id": "made-up-rule", "quote": "Items fit for resale go back on the shelf."},
                {
                    "rule_id": "on-time-sla-stated",
                    "quote": "Items fit for resale go back on the shelf.",
                },
            ]
        }
    )

    result = ComplianceResult.model_validate(
        compliance.evaluate(assignment, draft, markdown, True).result
    )

    assert result.reviewer == "used"
    assert result.rule_ids == ["returns-min-48h"]
    assert [v.source for v in result.violations] == ["reviewer"]


def test_an_unavailable_reviewer_leaves_the_rules_result(model, reverse_case):
    assignment, draft, markdown = reverse_case
    model(None)  # a reply without a tool call

    result = ComplianceResult.model_validate(
        compliance.evaluate(assignment, draft, markdown, True).result
    )

    assert result.reviewer == "unavailable" and result.passed is True


def test_the_reviewer_is_not_called_when_switched_off(reverse_case):
    # `isolated_rfp_intake` makes any model call fail the test.
    assignment, draft, markdown = reverse_case

    result = compliance.evaluate(assignment, draft, markdown, False).result

    assert result["reviewer"] == "off" and result["pass"] is True


# --- Merging and deciding -----------------------------------------------------------------


def results(*, readable=True, relevant=True, violations=()):
    draft = support.warehouse_draft("USD", "US", sla=False).model_dump()
    if not readable:
        draft["narrative"] = [CONVOLUTED] * 3
    readability_result = evaluation.evaluate_readability(
        draft, min_ease=MIN_EASE, max_grade=MAX_GRADE
    )
    relevance_result = RelevanceResult(
        passed=relevant, missing_aspects=[] if relevant else ["Onboarding time: not stated"]
    )
    compliance_result = ComplianceResult(
        passed=not violations,
        rule_ids=sorted({v.rule_id for v in violations}),
        violations=list(violations),
        checked_rule_ids=["currency-by-country", "returns-min-48h"],
    )
    return readability_result, relevance_result, compliance_result


def compose(*, readable=True, relevant=True, violations=(), notes=(), iteration=1):
    readability_result, relevance_result, compliance_result = results(
        readable=readable, relevant=relevant, violations=violations
    )
    return evaluation.compose(
        section_id=uuid4(),
        department_id="warehouse",
        iteration=iteration,
        readability_result=readability_result,
        relevance=relevance_result,
        compliance=compliance_result,
        guard_notes=list(notes),
        currency="USD",
        country="US",
        evaluated_at=datetime.now(UTC),
    )


RETURNS_24H = Violation(
    rule_id="returns-min-48h",
    message="A returns promise under 48 hours (24 hours).",
    quote="Returns are processed within 24 hours of arrival.",
)


def test_the_evaluation_result_has_the_ticket_shape():
    payload = compose().as_json()

    assert set(payload) >= {
        "section_id",
        "department_id",
        "readability",
        "relevance",
        "compliance",
        "overall_pass",
        "feedback_for_generator",
    }
    assert set(payload["readability"]) >= {"pass", "score", "details"}
    assert set(payload["relevance"]) >= {"pass", "missing_aspects"}
    assert set(payload["compliance"]) >= {"pass", "rule_ids", "violations"}
    assert payload["overall_pass"] is True and payload["feedback_for_generator"] == ""


def test_feedback_is_concrete_and_ordered_compliance_relevance_readability_guard():
    result = compose(
        readable=False,
        relevant=False,
        violations=[RETURNS_24H],
        notes=["price line 'x' dropped: not an item of this rate card"],
    )

    lines = result.feedback_for_generator.splitlines()
    assert result.overall_pass is False
    assert [line.split("] ")[0].split("[")[1] for line in lines] == [
        "returns-min-48h",
        "relevance",
        "readability",
        "guard",
    ]
    assert 'Rewrite or remove: "Returns are processed within 24 hours of arrival."' in lines[0]
    assert "72 hours" in lines[0]  # the rate card's turnaround
    assert "split:" in lines[2]


@pytest.mark.parametrize(
    ("kwargs", "iteration", "expected"),
    [
        ({}, 1, "accept"),
        ({"violations": [RETURNS_24H]}, 1, "revise"),
        ({"violations": [RETURNS_24H]}, 3, "escalate"),
        ({"relevant": False}, 2, "revise"),
        (
            {
                "violations": [
                    Violation(rule_id="currency-by-country", message="?", retryable=False)
                ]
            },
            1,
            "escalate",
        ),
        (  # unfixable, even with another check failing too: no attempt can pass
            {
                "relevant": False,
                "violations": [
                    Violation(rule_id="currency-by-country", message="?", retryable=False)
                ],
            },
            1,
            "escalate",
        ),
    ],
)
def test_the_loop_decision(kwargs, iteration, expected):
    result = compose(iteration=iteration, **kwargs)

    decision, reason = evaluation.decide(result, iteration=iteration, max_iterations=3)

    assert decision == expected
    assert reason


def test_a_quoted_table_row_counts_without_its_pipes(luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft, markdown = guarded(assignment, support.lastmile_draft("USD", "US", volume=5000))
    row = "Standard delivery, parcel up to 2 kg (3 to 5 business days) 6.90 USD per shipment"
    judgement = relevance.RelevanceJudgement(
        verdicts=[
            relevance.AspectVerdict(aspect_id=item["id"], verdict="covered", evidence_quote=row)
            for item in relevance.requirements(assignment)
        ]
    )

    assert relevance.finalize(assignment, draft, markdown, judgement).passed is True


def test_the_judge_knows_what_this_section_and_the_others_cover(luna):
    prompt = relevance.system_prompt(support.assignment_for(luna, "lastmile"))

    assert "It covers: cost per shipment, available carriers by destination, delivery sla" in prompt
    assert "Other sections of the same proposal cover: Warehouse Operations" in prompt
    assert "priced differently" not in prompt and "per cubic meter rather than" in prompt


def test_a_quote_joining_two_sentences_or_bullets_of_the_section_counts():
    markdown = "## T\n\nWe store your stock in Los Angeles.\n\n- Onboarding: 10 business days.\n"

    assert relevance.quoted_in(
        "We store your stock in Los Angeles. Onboarding: 10 business days.", markdown
    )
    assert relevance.quoted_in("- Onboarding: 10 business days.", markdown)
    assert not relevance.quoted_in("We store your stock in Los Angeles and Miami.", markdown)
    assert not relevance.quoted_in(
        "We store your stock in Los Angeles. We ship overnight.", markdown
    )


def test_the_judge_is_asked_again_for_requirements_it_left_out(model, luna):
    assignment = support.assignment_for(luna, "lastmile")
    draft, markdown = guarded(assignment, support.lastmile_draft("USD", "US", volume=5000))
    complete = verdicts(assignment, quote="We choose the carrier for each parcel")
    fake = model(
        {"verdicts": complete["verdicts"][1:]},  # A1 left out
        {"verdicts": complete["verdicts"][:1]},
    )

    reply = relevance.judge(assignment, draft, markdown)

    assert RelevanceResult.model_validate(reply.result).passed is True
    assert "these requirement ids only: A1" in fake.user
    assert reply.usage.total_tokens == 2 * (120 + 30)
