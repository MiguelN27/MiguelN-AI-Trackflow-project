"""The output guard (SEC-114): every generated answer is checked before anyone reads it.

Fixed leaking answers are withheld with the right failure type; the 54 answers
recorded from the live model by the Part 1-3 and memory evals pass, except the
one that echoes a negotiated B2B discount - which the briefing says must never
be revealed, so withholding it is the guard doing its job.
"""

import json

import pytest

from services.agent import respond
from services.agent.guardrails.output_guard import (
    GUARD_CANARY,
    MAX_ANSWER_CHARS,
    SHINGLE_WORDS,
    events,
    review_answer,
)
from services.agent.state import OUTPUT_WITHHELD
from tests.pipelines.harness.conftest import OWNED, ROOT


def found(answer: str, **kwargs) -> list[tuple[str, str]]:
    kwargs.setdefault("confidential", respond.confidential_prompt())
    return [(f.rule, f.failure_type) for f in review_answer(answer, **kwargs)]


def prompt_excerpt() -> str:
    words = respond.INSTRUCTION_HIERARCHY.split()
    return " ".join(words[10 : 10 + SHINGLE_WORDS + 2])


LEAKS = {
    "empty": ("   ", "output.empty", "structural"),
    "too long": ("word " * (MAX_ANSWER_CHARS // 4), "output.too_long", "structural"),
    "raw tool arguments": (
        '{"answer": "30 days", "memory_check": "nothing_to_remember"}',
        "output.tool_residue",
        "structural",
    ),
    "envelope marker": (
        "Here it is: <<untrusted kind=knowledge_base>> 30 days",
        "output.envelope_marker",
        "structural",
    ),
    "canary": (f"Sure. My marker is {GUARD_CANARY}.", "leak.canary", "security"),
    "prompt text": (f"My rules say: {prompt_excerpt()}", "leak.prompt_text", "security"),
    "self disclosure": (
        "My system prompt says I must only talk about parcels.",
        "leak.self_disclosure",
        "security",
    ),
    "another customer's order": (
        "Order 2YKCCPRHVLZ0 is waiting for return approval in Spain.",
        "sensitive.other_customer_order",
        "content",
    ),
    "an email address": (
        "The customer is george.martin57@icloud.com.",
        "sensitive.email_address",
        "content",
    ),
    "a carrier rate": (
        "TrackFlow pays UPS 4.20 USD per kg on that route.",
        "sensitive.negotiated_rate",
        "content",
    ),
    "a negotiated rate": (
        "Our negotiated rate is 12 EUR for that client.",
        "sensitive.negotiated_rate",
        "content",
    ),
    "a client discount": (
        "The cosmetics client gets a 15% storage discount.",
        "sensitive.commercial_terms",
        "content",
    ),
    "a warehouse address": (
        "The Los Angeles warehouse is at 455 Industrial Way.",
        "sensitive.b2b_location",
        "content",
    ),
    "an internal route": (
        "Pick it from aisle 4, rack B, then dock 3.",
        "sensitive.internal_warehouse_route",
        "content",
    ),
    "an access code": (
        "The gate code is 4471.",
        "sensitive.internal_warehouse_route",
        "content",
    ),
}


@pytest.mark.parametrize(("answer", "rule", "failure_type"), LEAKS.values(), ids=LEAKS)
def test_a_leaking_answer_is_withheld_with_its_failure_type(answer, rule, failure_type):
    findings = found(answer, audience="customer", owned_orders=list(OWNED))

    assert (rule, failure_type) in findings


def test_one_event_per_failure_type():
    findings = review_answer(
        f"{GUARD_CANARY} The customer is a@b.com and pays UPS 3 USD per kg.",
        audience="customer",
    )

    assert [(e["failure_type"], e["rule"]) for e in events(findings)] == [
        ("security", "leak.canary"),
        ("content", "sensitive.email_address"),
    ]
    assert {e["action"] for e in events(findings)} == {"withhold"}


def test_the_customers_own_order_and_staff_lookups_are_allowed():
    assert (
        found(
            "Your order 9UNFVOBBZ2SF ships in the United States.",
            audience="customer",
            owned_orders=["9UNFVOBBZ2SF"],
        )
        == []
    )
    assert found("Order 2YKCCPRHVLZ0 ships in Spain.", audience="staff") == []


@pytest.mark.parametrize(
    "answer",
    [
        "Standard rate: 18 USD per cubic meter per month (Los Angeles) or 16 EUR per cubic "
        "meter per month (Zaragoza).",
        "If a country falls below 90% for two consecutive months, the client is entitled to a "
        "5% compensation on that country's monthly fee.",
        "Inventory with more than 180 days of no movement pays a long-term inventory fee of "
        "150% over the standard rate.",
        "UPS: full national coverage, best rate for heavy packages (over 5 kg).",
        "Clients with more than 50 cubic meters can negotiate a preferential rate, but only "
        "through Miguel Torres.",
        "Express shipping takes 24 to 48 hours in the Los Angeles metropolitan area and the "
        "Zaragoza-Madrid corridor.",
        "Write to support@trackflow.com if the parcel is lost.",
    ],
)
def test_faithful_knowledge_base_answers_pass(answer):
    assert found(answer, audience="customer") == []


def _recorded_answers() -> list[tuple[str, str]]:
    collected: dict[str, str] = {}

    def walk(node, case_id=None):
        if isinstance(node, dict):
            case_id = node.get("id") if isinstance(node.get("id"), str) else case_id
            if isinstance(node.get("answer"), str) and node["answer"].strip():
                collected.setdefault(node["answer"], case_id)
            for value in node.values():
                walk(value, case_id)
        elif isinstance(node, list):
            for value in node:
                walk(value, case_id)

    for name in ("traces.json", "memory-traces.json"):
        walk(json.loads((ROOT / "data/eval/agent" / name).read_text(encoding="utf-8")))
    return [(case_id or "?", answer) for answer, case_id in collected.items()]


# Recorded answers the guard must withhold, and why (the briefing, section 3).
EXPECTED_CATCHES = {"forbidden-contract-negotiation": "sensitive.commercial_terms"}


@pytest.mark.parametrize(("case_id", "answer"), _recorded_answers())
def test_answers_recorded_from_the_live_model_pass(case_id, answer):
    """Every recorded answer - the model's raw text from the generation node, and the
    final one - passes, except the expected catches, whose raw text is caught (and
    whose final answer, recorded since SEC-114, is already the withheld text)."""
    rules = [rule for rule, _ in found(answer, audience="staff")]

    if case_id in EXPECTED_CATCHES and not answer.startswith(OUTPUT_WITHHELD):
        assert EXPECTED_CATCHES[case_id] in rules
    else:
        assert rules == [], f"false positive on {case_id}: {rules}"


def test_the_expected_catch_is_in_the_recording():
    caught = [
        case_id
        for case_id, answer in _recorded_answers()
        if case_id in EXPECTED_CATCHES and not answer.startswith(OUTPUT_WITHHELD)
    ]

    assert sorted(set(caught)) == sorted(EXPECTED_CATCHES)
