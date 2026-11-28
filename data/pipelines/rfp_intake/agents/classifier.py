"""The classifier agent: is this document a TrackFlow RFP? (ticket: "Classifier agent").

The criteria are the briefing's (§1, §2.2, §4): a prospective client - an
e-commerce brand - asks TrackFlow for a proposal for warehousing, last mile
and/or returns, in the US and/or Spain. Formal and informal requests are both
RFPs. A carrier or vendor selling its services *to* TrackFlow is not one, even
though it talks about shipping, volumes and prices.

The model labels the document; `decide` turns the label into the routing
decision without a model. It is recall-first: a missed RFP costs a deal, while
a false alarm costs one department analysis that a person sees flagged. So a
ticket is discarded only on a consistent, confident "not an RFP".
"""

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.text import appears_in, clean_quote

TOOL_NAME: Final = "classify_document"
RFP_KINDS: Final = frozenset({"formal_rfp", "informal_rfp"})
MIN_CONFIDENCE: Final = 0.7

KIND_LABELS: Final[dict[str, str]] = {
    "formal_rfp": "formal RFP",
    "informal_rfp": "informal RFP",
    "carrier_or_vendor_offer": "a carrier or vendor offering its services to TrackFlow",
    "other": "not a request for a proposal",
}

PROMPT: Final = """\
You are the intake classifier of TrackFlow's Sales team. TrackFlow is a logistics \
provider (3PL): it sells warehousing, last-mile delivery with carrier management, and \
reverse logistics (returns handling) to e-commerce brands, with operations in the United \
States and Spain.

Decide whether the DOCUMENT is a TrackFlow RFP: a prospective client (typically an \
e-commerce brand in fashion, electronics or cosmetics) asking TrackFlow, or logistics \
providers in general, for a proposal or pricing for services TrackFlow sells - \
warehousing, last mile, reverse logistics, or a combination - in the United States, \
Spain or both. Call classify_document once.

document_kind:
- formal_rfp: a structured request for proposal (reference number, scope of work, \
evaluation criteria, timeline, submission instructions).
- informal_rfp: the same kind of request written informally, for example an email that \
describes the client's logistics needs and asks for a proposal or a quote. Informal RFPs \
are valid RFPs.
- carrier_or_vendor_offer: a carrier, supplier or partner offering its own services or \
rates TO TrackFlow. TrackFlow would be the buyer, so it is not an RFP, even when it \
mentions shipping, volumes or prices.
- other: anything else - an invoice, a newsletter, a job application, a support request \
from an existing client, a question that asks for no proposal, unrelated text.

is_rfp is true only for formal_rfp and informal_rfp. confidence: how sure you are of \
document_kind, from 0 to 1. language: en, es, mixed (both) or other. sender: the \
organisation that wrote the document, as written. reasons: up to 3 short sentences in \
English that justify the decision. evidence_quotes: up to 3 short passages copied \
exactly from the DOCUMENT that support it.\
"""


class DocumentClassification(BaseModel):
    """Classify the document: is it a client RFP for TrackFlow's logistics services?"""

    model_config = ConfigDict(extra="ignore")

    document_kind: Literal["formal_rfp", "informal_rfp", "carrier_or_vendor_offer", "other"]
    is_rfp: bool
    confidence: float = Field(ge=0.0, le=1.0)
    language: Literal["en", "es", "mixed", "other"] = "other"
    sender: str = Field(default="", max_length=300)
    # Lists are not length-capped here: an extra item is trimmed by `decide`,
    # not treated as an invalid reply.
    reasons: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)


def build_user_message(markdown: str) -> str:
    return f"DOCUMENT\n{llm.envelope('rfp_document', markdown)}"


def decide(markdown: str, label: DocumentClassification) -> dict[str, Any]:
    """The routing decision for `label`, deterministic. JSON-mode."""
    flags: list[str] = []
    quotes = [quote.strip() for quote in label.evidence_quotes if quote.strip()]
    verified = [clean_quote(quote) for quote in quotes if appears_in(quote, markdown)]
    if len(verified) < len(quotes):
        flags.append("unverified_evidence")

    says_rfp = label.is_rfp and label.document_kind in RFP_KINDS
    says_not_rfp = not label.is_rfp and label.document_kind not in RFP_KINDS
    confident = label.confidence >= MIN_CONFIDENCE

    if says_rfp:
        decision = "accept"
        if not confident:
            flags.append("low_confidence")
    elif says_not_rfp and confident:
        decision = "discard"
    else:
        # A hesitant or self-contradicting "not an RFP": analyse it anyway, flagged.
        decision = "accept"
        flags.append("low_confidence" if says_not_rfp else "inconsistent_verdict")

    reasons = [reason.strip() for reason in label.reasons if reason.strip()][:3]
    result: dict[str, Any] = {
        "decision": decision,
        "is_rfp": decision == "accept",
        "document_kind": label.document_kind,
        "model_is_rfp": label.is_rfp,
        "confidence": round(label.confidence, 3),
        "language": label.language,
        "sender": label.sender.strip() or None,
        "reasons": reasons,
        "evidence_quotes": verified[:3],
        "flags": flags,
    }
    if decision == "discard":
        because = " ".join(reasons) or "The classifier found no request for a proposal."
        result["discard_reason"] = (
            f"Not a client RFP: {KIND_LABELS[label.document_kind]}. {because}"
        )
    elif flags and set(flags) - {"unverified_evidence"}:
        result["review_note"] = (
            "Analysed although the classifier was not certain this is an RFP: "
            "a person should confirm it before Part 2."
        )
    return result


def classify_document(markdown: str) -> AgentReply:
    label, usage = llm.call_tool(
        system=PROMPT,
        user=build_user_message(markdown),
        tool_name=TOOL_NAME,
        output=DocumentClassification,
    )
    return AgentReply(decide(markdown, label), usage)
