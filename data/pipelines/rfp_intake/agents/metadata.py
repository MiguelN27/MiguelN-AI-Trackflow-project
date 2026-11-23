"""The metadata extractor (ticket: "Extract metadata from the converted document").

Briefing §2.3 fields: client name and country, requested services, monthly
volume, deadline and budget, plus `departments_needed` and the currency, which
are derived here from the services and the country, never by the model.

Nothing is guessed. Each value the model reads must come with a quote that is
really in the document, and a figure must be in its own quote; otherwise the
value is left empty and becomes a question for the client. A deadline without
a year ("around August 24th") keeps its wording but gets no date.
"""

import re
from datetime import date
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from data.pipelines.rfp_intake import llm
from data.pipelines.rfp_intake.agents import AgentReply
from data.pipelines.rfp_intake.briefing import (
    CURRENCY_BY_COUNTRY,
    SERVICES,
    departments_for,
)
from data.pipelines.rfp_intake.text import (
    appears_in,
    canonical_figures,
    clean_quote,
    first_line,
    squash,
)

TOOL_NAME: Final = "record_rfp_metadata"

# A monthly volume's quote must talk about orders (or shipments) per month, in
# English or Spanish. "15.000 SKUs" passes the figure check but is a catalogue
# size, not a volume (ModaViva's RFP).
_ORDER_WORDS: Final = re.compile(
    r"\b(orders?|shipments?|parcels?|packages?|deliveries|pedidos?|env[ií]os?|"
    r"paquetes?|entregas?)\b",
    re.IGNORECASE,
)
_MONTH_WORDS: Final = re.compile(r"\b(month|monthly|mo|mes|meses|mensual(?:es)?)\b", re.IGNORECASE)

PROMPT: Final = """\
You extract the metadata of a client RFP for TrackFlow's Sales team. TrackFlow sells \
warehousing, last-mile delivery with carrier management, and reverse logistics to \
e-commerce brands in the United States and Spain. Call record_rfp_metadata once. Copy \
values only from the DOCUMENT. When the document does not state a value, leave it null: \
never estimate, convert, round or invent one.

- client_name: the client organisation asking for the proposal (not TrackFlow, not a \
person). client_name_quote: the passage that names it.
- client_country: where the client needs TrackFlow to operate, "US" (United States) or \
"ES" (Spain), read from the client's location and the operations it requests; null if \
neither applies or it is unclear. client_country_quote: the passage that shows it.
- services_requested: the TrackFlow services the client asks for. "warehousing": \
storage, fulfilment, pick and pack, inventory. "last_mile": shipping and delivery to end \
customers, carrier management or carrier selection. "reverse_logistics": returns \
handling. A service the document explicitly excludes (for example, the client keeps its \
own carrier) is not requested. services_quotes: the passages that ask for each service.
- monthly_volume: orders or shipments per month, as a whole number, only if the \
document states a monthly order volume. A number of SKUs, pallets or stored items is not \
a monthly volume. monthly_volume_quote: the passage with that figure.
- deadline: the date the proposal is due; if no due date is given, the date the client \
needs the service to start. As YYYY-MM-DD only when the document gives the day, the \
month and the year; otherwise null. deadline_text: that deadline exactly as the document \
words it, also when deadline is null. deadline_kind: proposal_due, service_start or other.
- budget_range: the reference budget exactly as written, with its currency; null if \
there is none. budget_quote: the passage with it.

Every quote is copied exactly from the DOCUMENT, in its language, at most about 30 words.\
"""

ServiceName = Literal["warehousing", "last_mile", "reverse_logistics"]


class ExtractedMetadata(BaseModel):
    """Record the RFP's metadata exactly as the document states it."""

    model_config = ConfigDict(extra="ignore")

    client_name: str | None = None
    client_name_quote: str | None = None
    client_country: Literal["US", "ES"] | None = None
    client_country_quote: str | None = None
    services_requested: list[ServiceName] = Field(default_factory=list)
    services_quotes: list[str] = Field(default_factory=list)
    monthly_volume: int | None = Field(default=None, ge=0)
    monthly_volume_quote: str | None = None
    deadline: str | None = Field(default=None, description="YYYY-MM-DD, or null")
    deadline_text: str | None = None
    deadline_kind: Literal["proposal_due", "service_start", "other"] | None = None
    budget_range: str | None = None
    budget_quote: str | None = None


# What Sales asks the client when a field is missing. Order = display order.
_MISSING_QUESTIONS: Final[dict[str, str]] = {
    "client_name": "Confirm the client's legal name: the RFP does not state it clearly.",
    "client_country": (
        "Confirm whether the operation is in the United States or Spain: it sets the "
        "proposal currency (USD or EUR)."
    ),
    "services_requested": "Confirm which services the client needs: none is stated clearly.",
    "monthly_volume": "Ask for the expected monthly order volume: the RFP does not state it.",
    "deadline": "Ask when the proposal is due and when the service should start.",
    "budget_range": "Ask whether there is a reference budget: the RFP gives none.",
}


def build_user_message(markdown: str) -> str:
    return f"DOCUMENT\n{llm.envelope('rfp_document', markdown)}"


def is_monthly_order_quote(quote: str | None) -> bool:
    return bool(quote and _ORDER_WORDS.search(quote) and _MONTH_WORDS.search(quote))


def _clean(value: str | None) -> str | None:
    return squash(value) if value and value.strip() else None


def _full_date(raw: str | None, wording: str | None) -> date | None:
    """`raw` as a date, only when its day and year are written in `wording`."""
    if not raw or not wording:
        return None
    try:
        parsed = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    written = canonical_figures(wording)
    return parsed if {str(parsed.year), str(parsed.day)} <= written else None


def finalize(markdown: str, extracted: ExtractedMetadata) -> dict[str, Any]:
    """The stored metadata for `extracted`, deterministic. JSON-mode."""
    notes: list[str] = []
    quotes: dict[str, Any] = {}

    def keep(field: str, value: Any, quote: str | None, *, value_is_text: bool = False) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if quote and appears_in(quote, markdown):
            quotes[field] = clean_quote(quote)
            return value
        if value_is_text and appears_in(str(value), markdown):
            quotes[field] = squash(str(value))
            return value
        notes.append(f"{field} dropped: its quote is not in the document")
        return None

    client_name = keep(
        "client_name",
        _clean(extracted.client_name),
        extracted.client_name_quote,
        value_is_text=True,
    )
    client_country = keep(
        "client_country", extracted.client_country, extracted.client_country_quote
    )

    services = [s for s in dict.fromkeys(extracted.services_requested) if s in SERVICES]
    service_quotes = [clean_quote(q) for q in extracted.services_quotes if appears_in(q, markdown)]
    if services and not service_quotes:
        notes.append("services_requested: no service quote was found verbatim in the document")
    quotes["services_requested"] = service_quotes

    monthly_volume = extracted.monthly_volume
    if monthly_volume is not None:
        quote = extracted.monthly_volume_quote
        if not (appears_in(quote, markdown) and str(monthly_volume) in canonical_figures(quote)):
            notes.append(
                f"monthly_volume {monthly_volume} dropped: the figure is not in a quote "
                "from the document"
            )
            monthly_volume = None
        elif not is_monthly_order_quote(quote):
            notes.append(
                f"monthly_volume {monthly_volume} dropped: its quote does not state orders "
                "per month"
            )
            monthly_volume = None
        else:
            quotes["monthly_volume"] = clean_quote(quote) if quote else None

    deadline_text = _clean(extracted.deadline_text)
    deadline_text = clean_quote(deadline_text) if deadline_text else None
    if deadline_text and not appears_in(deadline_text, markdown):
        notes.append("deadline_text dropped: it is not in the document")
        deadline_text = None
    deadline = _full_date(extracted.deadline, deadline_text)
    if extracted.deadline and deadline is None:
        notes.append(
            "deadline left without a date: the document does not give its full date"
            + (f' ("{first_line(deadline_text, 80)}")' if deadline_text else "")
        )
    if deadline_text:
        quotes["deadline"] = deadline_text

    budget_range = keep(
        "budget_range", _clean(extracted.budget_range), extracted.budget_quote, value_is_text=True
    )

    values = {
        "client_name": client_name,
        "client_country": client_country,
        "services_requested": services or None,
        "monthly_volume": monthly_volume,
        "deadline": deadline,
        "budget_range": budget_range,
    }
    missing = [field for field, value in values.items() if value is None]
    client_questions = []
    for field in missing:
        if field == "deadline" and deadline_text:
            client_questions.append(
                f'Confirm the exact date behind "{first_line(deadline_text, 80)}": '
                "the RFP gives no full date."
            )
        else:
            client_questions.append(_MISSING_QUESTIONS[field])

    return {
        "client_name": client_name,
        "client_country": client_country,
        "currency": CURRENCY_BY_COUNTRY.get(client_country) if client_country else None,
        "services_requested": services,
        "monthly_volume": monthly_volume,
        "deadline": deadline.isoformat() if deadline else None,
        "deadline_text": deadline_text,
        "deadline_kind": extracted.deadline_kind if deadline_text or deadline else None,
        "budget_range": budget_range,
        "departments_needed": departments_for(services),
        "evidence": quotes,
        "missing": missing,
        "client_questions": client_questions,
        "notes": notes,
    }


def extract_metadata(markdown: str) -> AgentReply:
    extracted, usage = llm.call_tool(
        system=PROMPT,
        user=build_user_message(markdown),
        tool_name=TOOL_NAME,
        output=ExtractedMetadata,
    )
    return AgentReply(finalize(markdown, extracted), usage)
