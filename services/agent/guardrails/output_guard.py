"""The output guard: a generated answer is checked before anyone reads it.

Deterministic. An answer fails, and is replaced with a fixed text, when:

- structural: it is empty, longer than `MAX_ANSWER_CHARS`, or carries what an
  answer never should - the raw `respond` tool arguments (JSON), tool-call
  markup, or our envelope and chat-role markers;
- security: it leaks the agent's instructions - the canary marker hidden in
  the system prompt, a run of `SHINGLE_WORDS` consecutive words copied from
  the confidential part of the prompt, or a sentence about its own prompt;
- content: it reveals what the briefing (SEC-114, section 3) says the agent
  must never reveal:
  - an order or tracking number the signed-in customer does not own;
  - an email address (customers' emails are in the order records);
  - a carrier, or a negotiated / preferential / contract rate, in the same
    sentence as a price, a percentage or a per-kg figure, or a percentage
    discount: negotiated rates and B2B commercial terms;
  - an exact location (street address, postcode, coordinates) or an internal
    warehouse route (aisle, rack, dock number, access code), with the
    regexes the memory policy already enforces (`policy.location_findings`).

The knowledge base never pairs a carrier with a figure and holds no address,
so a faithful answer passes; `tests/pipelines/harness/test_output_guard.py`
replays every answer recorded from the live model to prove it.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from services.agent.guardrails.input_guard import extract_order_references
from services.agent.guardrails.normalize import fold
from services.agent.memory import policy

MAX_ANSWER_CHARS: Final = 4000
SHINGLE_WORDS: Final = 10

# Hidden in the system prompt, described as a marker never to be written. If it
# shows up in an answer, the prompt is leaking, whatever else the answer says.
GUARD_CANARY: Final = "TF-GUARD-7Q2X-91LM"

_TOOL_RESIDUE: Final = re.compile(
    r"\"(?:answer|memory_check|memory_check_reason|memory_proposal|scope)\"\s*:|<tool_call>|"
    r"</?function(?:_call)?>|\"tool_calls\"|\bfunctions\.respond\b",
    re.IGNORECASE,
)
_MARKERS: Final = re.compile(
    r"<<\s*/?\s*untrusted|‹‹\s*/?\s*untrusted|<\|\s*im_(?:start|end)|\[/?\s*INST\s*\]",
    re.IGNORECASE,
)
_SELF_DISCLOSURE: Final = re.compile(
    r"\b(?:my|the) (?:system prompt|system message|hidden instructions|initial instructions|"
    r"internal instructions|developer message)\b|\bi(?:'ve| have| was| am)? (?:been )?"
    r"(?:instructed|programmed|prompted) (?:to|not to)\b|\bmy instructions (?:say|are|state|"
    r"tell|include)\b",
    re.IGNORECASE,
)

_EMAIL: Final = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_OWN_DOMAINS: Final = frozenset({"trackflow.com"})

_CARRIER: Final = re.compile(
    r"\b(?:ups|fedex|fed ex|dhl|mrw|seur|estafeta|local (?:last-mile )?carriers?)\b", re.I
)
_RATE_WORDS: Final = re.compile(
    r"\b(?:negotiated|preferential|special|discounted|contract(?:ed)?|confidential|"
    r"agreed|private)\s+(?:rates?|prices?|pricing|tariffs?|fees?|discounts?|terms)\b",
    re.IGNORECASE,
)
_FIGURE: Final = re.compile(
    r"(?:[$€£]\s?\d|\d[\d.,]*\s?(?:usd|eur|euros?|dollars?|mxn|pesos?|[$€£])\b|"
    r"\d+(?:[.,]\d+)?\s?%|\bper (?:kg|kilo|kilogram|lb|pound|parcel|package|shipment|delivery)\b)",
    re.IGNORECASE,
)
_PERCENT_DISCOUNT: Final = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?%\s+(?:\w+\s+)?(?:discount|rebate|off)\b", re.IGNORECASE
)
# A sentence; a dot between digits ("4.20 USD") does not end it.
_SENTENCE: Final = re.compile(r"(?:[^.!?\n]|\.(?=\d))+(?:[.!?]|\n|$)")


@dataclass(frozen=True)
class OutputFinding:
    rule: str
    failure_type: str


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", fold(text, leet=False))


def shingles(text: str, size: int = SHINGLE_WORDS) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def _structural(answer: str) -> list[OutputFinding]:
    if not answer.strip():
        return [OutputFinding("output.empty", "structural")]
    found: list[OutputFinding] = []
    if len(answer) > MAX_ANSWER_CHARS:
        found.append(OutputFinding("output.too_long", "structural"))
    if _TOOL_RESIDUE.search(answer) or re.fullmatch(r"\s*\{[\s\S]*\}\s*", answer):
        found.append(OutputFinding("output.tool_residue", "structural"))
    if _MARKERS.search(answer):
        found.append(OutputFinding("output.envelope_marker", "structural"))
    return found


def _leaks(answer: str, confidential: str) -> list[OutputFinding]:
    found: list[OutputFinding] = []
    if GUARD_CANARY.lower() in answer.lower():
        found.append(OutputFinding("leak.canary", "security"))
    if confidential and shingles(answer) & shingles(confidential):
        found.append(OutputFinding("leak.prompt_text", "security"))
    if _SELF_DISCLOSURE.search(answer):
        found.append(OutputFinding("leak.self_disclosure", "security"))
    return found


def _sensitive(answer: str, *, audience: str, owned_orders: Collection[str]) -> list[OutputFinding]:
    found: list[OutputFinding] = []
    if audience == "customer":
        owned = {order.upper() for order in owned_orders}
        if any(ref not in owned for ref in extract_order_references(answer)):
            found.append(OutputFinding("sensitive.other_customer_order", "content"))
    if any(match.group(1).lower() not in _OWN_DOMAINS for match in _EMAIL.finditer(answer)):
        found.append(OutputFinding("sensitive.email_address", "content"))
    for sentence in _SENTENCE.findall(answer):
        if _FIGURE.search(sentence) and (_CARRIER.search(sentence) or _RATE_WORDS.search(sentence)):
            found.append(OutputFinding("sensitive.negotiated_rate", "content"))
            break
    if _PERCENT_DISCOUNT.search(answer):
        found.append(OutputFinding("sensitive.commercial_terms", "content"))
    for reason in policy.location_findings(answer):
        found.append(OutputFinding(f"sensitive.{reason}", "content"))
    return found


def review_answer(
    answer: str,
    *,
    audience: str = "staff",
    owned_orders: Collection[str] = (),
    confidential: str = "",
) -> list[OutputFinding]:
    """Everything wrong with `answer`, structural first; empty when it may be shown."""
    structural = _structural(answer)
    if any(finding.rule == "output.empty" for finding in structural):
        return structural
    return [
        *structural,
        *_leaks(answer, confidential),
        *_sensitive(answer, audience=audience, owned_orders=owned_orders),
    ]


def events(findings: list[OutputFinding]) -> list[dict[str, Any]]:
    """One `withhold` event per failure type found (its first rule)."""
    first: dict[str, OutputFinding] = {}
    for finding in findings:
        first.setdefault(finding.failure_type, finding)
    return [
        {
            "guardrail": "output_validation",
            "action": "withhold",
            "failure_type": failure_type,
            "rule": finding.rule,
            "source": "model_answer",
        }
        for failure_type, finding in first.items()
    ]
