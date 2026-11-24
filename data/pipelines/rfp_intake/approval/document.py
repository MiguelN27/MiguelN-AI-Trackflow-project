"""The final document: the approved sections consolidated, for Sales to send (briefing §2.3).

`FinalDocument` has the briefing's fields (`ticket_id`, `sections`, `currency`,
`generated_at`) plus what Sales needs to use it: the client, the Markdown
`content` and its SHA-256. It is composed deterministically, with no model:
every owner approved a precise text, so that text goes out as approved, word
for word. Only the section's own "For approval by ..." header line is
replaced by who approved it and when, and its headings move one level down
under the document's.

Layout:
- title and cover lines (client, country, currency, issue date, scope, volume,
  requested go-live);
- one numbered part per approved section, in the briefing's department order;
- "Approvals": department, owner, decision date;
- "Notes": the rate card version (indicative until Commercial confirms it),
  any arbitration that changed the proposal, and the questions for the client.
"""

import hashlib
import re
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from data.pipelines.rfp_intake.briefing import (
    COUNTRY_NAMES,
    DEPARTMENT_BY_ID,
    DEPARTMENT_IDS,
    SERVICE_LABELS,
    Currency,
    DepartmentId,
)
from data.pipelines.rfp_intake.proposal.handoff import ProposalHandoff

_FOR_APPROVAL: Final = re.compile(r"^For approval by [^.]+\.\s*")
_HEADING: Final = re.compile(r"^(#{1,5}) ")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FinalSection(_Model):
    department_id: DepartmentId
    department_name: str
    owner: str
    approved_by: str
    approved_at: datetime
    review_rounds: int
    is_sla_section: bool
    # The section as approved, under the document's headings.
    content: str


class FinalClient(_Model):
    name: str | None
    country: str | None
    currency: Currency


class FinalDocument(_Model):
    """Briefing §2.3: `ticket_id`, `sections`, `currency`, `generated_at` (plus what Sales uses)."""

    ticket_id: UUID
    rfp_id: UUID
    client: FinalClient
    currency: Currency
    sections: list[FinalSection]
    monthly_volume: int | None
    volume_capped_by_arbitration: bool
    rate_card_version: str
    rulings: list[dict[str, Any]]
    approval_thread_id: str
    generated_at: datetime
    content: str
    content_sha256: str


def _date(value: datetime) -> str:
    return value.strftime("%d %B %Y").lstrip("0")


def section_body(markdown: str, *, owner: str, approved_at: datetime) -> str:
    """The approved section under the document: header replaced, headings one level down."""
    lines = markdown.strip().splitlines()
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and _FOR_APPROVAL.match(line):
            rest = _FOR_APPROVAL.sub("", line)
            out.append(f"Approved by {owner} on {_date(approved_at)}. {rest}".strip())
            replaced = True
            continue
        out.append(_HEADING.sub(lambda m: "#" + m.group(1) + " ", line))
    return "\n".join(out).strip()


def _cover(proposal: ProposalHandoff, currency: str, volume: int | None, capped: bool) -> list[str]:
    client = proposal.client
    country = COUNTRY_NAMES.get(client.country or "", client.country or "not stated")
    services = ", ".join(SERVICE_LABELS.get(s, s) for s in proposal.services_requested)
    volume_text = f"{volume:,} orders a month" if volume is not None else "not stated in the RFP"
    if capped:
        volume_text += " (capped at warehouse capacity by arbitration)"
    deadline = proposal.deadline_text or (
        proposal.deadline.isoformat() if proposal.deadline else "not stated"
    )
    return [
        f"- **Client:** {client.name or 'not stated'} ({country})",
        f"- **Prices in:** {currency}",
        f"- **Services:** {services}",
        f"- **Monthly volume:** {volume_text}",
        f"- **Deadline (from the RFP):** {deadline}",
    ]


def compose(
    proposal: ProposalHandoff,
    sections: list[dict[str, Any]],
    *,
    thread_id: str,
    volume_cap: int | None,
    rulings: list[dict[str, Any]],
    generated_at: datetime,
) -> FinalDocument:
    """The final document from the approved sections (`sections`: the stored rows as dicts).

    Every active department must be approved; the caller (the graph) only
    composes once they are, and this checks it again.
    """
    currency = proposal.currency
    if currency is None:
        raise ValueError("a final document needs the client's currency")
    active = {section.department_id for section in proposal.sections}
    approved = {
        row["department_id"]: row
        for row in sections
        if row["department_id"] in active and row["approval_status"] == "approved"
    }
    if set(approved) != active:
        missing = sorted(active - set(approved))
        raise ValueError(f"not every section is approved: {', '.join(missing)}")

    volume = proposal.monthly_volume
    capped = volume_cap is not None and (volume is None or volume_cap < volume)
    if capped:
        volume = volume_cap
    title = f"# Pricing proposal for {proposal.client.name or 'the client'}"
    lines = [
        title,
        "",
        f"Prepared by TrackFlow on {_date(generated_at)}.",
        "",
        *_cover(proposal, currency, volume, capped),
        "",
    ]
    final_sections: list[FinalSection] = []
    sla_department = proposal.sla_department
    ordered = [dept for dept in DEPARTMENT_IDS if dept in approved]
    for number, dept in enumerate(ordered, 1):
        row = approved[dept]
        approved_at = row["approved_at"]
        body = section_body(row["draft_content"] or "", owner=row["owner"], approved_at=approved_at)
        name = DEPARTMENT_BY_ID[dept].name
        lines += [f"## {number}. {name}", "", body, ""]
        final_sections.append(
            FinalSection(
                department_id=dept,
                department_name=name,
                owner=row["owner"],
                approved_by=row["approved_by"] or row["owner"],
                approved_at=approved_at,
                review_rounds=row.get("review_rounds") or 0,
                is_sla_section=dept == sla_department,
                content=body,
            )
        )

    lines += ["## Approvals", "", "| Department | Approved by | Date |", "|---|---|---|"]
    for section in final_sections:
        lines.append(
            f"| {section.department_name} | {section.owner} | {_date(section.approved_at)} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        f"- Prices come from rate card {proposal.rate_card_version}. Its indicative prices are "
        "to be confirmed by Commercial before this proposal is sent.",
    ]
    for ruling in rulings:
        if ruling.get("action") == "request_changes":
            lines.append(
                f"- Arbitration ({ruling['trigger_id']}): {ruling['arbiter']} applied the rule "
                f'"{ruling["rule"]}" to {", ".join(ruling["departments"])}.'
            )
    if proposal.client_questions:
        lines += ["", "### Questions for the client", ""]
        lines += [f"- {question}" for question in proposal.client_questions]
    content = "\n".join(lines).rstrip() + "\n"
    return FinalDocument(
        ticket_id=proposal.ticket_id,
        rfp_id=proposal.rfp_id,
        client=FinalClient(
            name=proposal.client.name, country=proposal.client.country, currency=currency
        ),
        currency=currency,
        sections=final_sections,
        monthly_volume=volume,
        volume_capped_by_arbitration=capped,
        rate_card_version=proposal.rate_card_version,
        rulings=rulings,
        approval_thread_id=thread_id,
        generated_at=generated_at,
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
