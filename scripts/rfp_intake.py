"""The RFP workflow from the command line (Milestone 9 Parts 1-3).

Not a second HTTP API.

    npm run rfp -- run PDF-examples/trackflow-request-1.pdf   # intake + drafting + open approvals
    npm run rfp -- list
    npm run rfp -- show <ticket_id> [--json]                  # the Sales view, or everything
    npm run rfp -- reprocess <ticket_id>                      # a failed or discarded ticket
    npm run rfp -- handoff <ticket_id>                        # the Part 2 input (JSON)
    npm run rfp -- draft [<ticket_id>]                        # draft one ticket, or the queue
    npm run rfp -- redraft <ticket_id>                        # Part 2 again, from the handoff
    npm run rfp -- proposal <ticket_id>                       # the Part 3 payload (JSON)
    npm run rfp -- request-approval [--limit N]               # open approvals for the queue
    npm run rfp -- approvals <ticket_id>                      # open requests, decisions, rulings
    npm run rfp -- decide <ticket_id> <department> approve|request-changes|reject \
                          --as <owner email> [--comment TEXT]  # an owner's decision, applied
    npm run rfp -- resume <ticket_id>                         # continue a stopped approval run
    npm run rfp -- document <ticket_id> [--output FILE]       # the final document (Markdown)
    npm run rfp -- trace <ticket_id> [--json] [--state]       # which agent did what, in order
    npm run rfp -- kpi                                        # iterations, approval and build time
    npm run rfp -- graph [--part 1|2|3]                       # the graphs as Mermaid
    npm run rfp -- handoff-schema [--write]                   # docs/rfp/handoff-contract.md
    npm run rfp -- proposal-schema [--write]                  # the Part 3 contract doc
    LLM_TIMEOUT_SECONDS=120 npm run rfp:smoke                 # the three briefing PDFs, live

All commands but `smoke`, `graph` and the schemas use the API's database
(`DATABASE_URL`) and create the `rfp_*` tables when missing, like the API's
start-up. `smoke` uses the real models but a temporary SQLite database, so it
leaves no ticket behind. It runs intake, drafting and approval on each PDF:
the owners are simulated (programmatic resumes), one of them asks for a
change, which the live generator makes. It writes
`data/eval/rfp_intake/smoke.json` with the briefing's KPIs (§3): correct
classification rate and average iterations per section.

Exit codes: 0 done, 1 a smoke expectation failed or a run did not complete,
2 configuration (models or database), 3 unknown ticket or refused
reprocess/draft/redraft/decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlmodel import create_engine

from data.pipelines.rfp_intake import handoff, runner, store
from data.pipelines.rfp_intake.approval import approvers, decisions
from data.pipelines.rfp_intake.approval import detail as approval_detail
from data.pipelines.rfp_intake.approval import graph as approval_graph
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.approval import trace as approval_trace
from data.pipelines.rfp_intake.config import ROOT_DIR, RfpIntakeSettings, get_rfp_settings
from data.pipelines.rfp_intake.graph import draw_mermaid
from data.pipelines.rfp_intake.proposal import graph as proposal_graph
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from data.pipelines.rfp_intake.proposal import store as proposal_store
from data.process.knowledge_base import KnowledgeBaseError, get_rag_settings

DEFAULT_PDF_DIR = ROOT_DIR / "PDF-examples"
DEFAULT_SMOKE_OUTPUT = ROOT_DIR / "data" / "eval" / "rfp_intake" / "smoke.json"
CONTRACT_DOC = ROOT_DIR / "docs" / "rfp" / "handoff-contract.md"
SCHEMA_BEGIN = "<!-- handoff-schema:begin -->"
SCHEMA_END = "<!-- handoff-schema:end -->"
PROPOSAL_DOC = ROOT_DIR / "docs" / "rfp" / "proposal-handoff-contract.md"
PROPOSAL_SCHEMA_BEGIN = "<!-- proposal-schema:begin -->"
PROPOSAL_SCHEMA_END = "<!-- proposal-schema:end -->"

# Where intake + drafting may end for a document the intake accepted.
DRAFTED: frozenset[str] = frozenset({"waiting_for_approval", "needs_human_review"})
COMPLETED: frozenset[str] = DRAFTED | {"intake_complete", "discarded"}
# The smoke run's simulated owners (Part 3), one account per department.
SMOKE_OWNERS: dict[str, str] = {
    "warehouse": "ana.whitfield@smoke.trackflow.test",
    "lastmile": "carlos.vega@smoke.trackflow.test",
    "reverse": "sofia.ramos@smoke.trackflow.test",
}
SMOKE_CHANGE_REQUEST = (
    "Please add one sentence that says which person at TrackFlow the client contacts for "
    "this service, and keep everything else as it is."
)
# The order a ticket's status must follow through the three parts.
FORWARD = (
    "analyzing",
    "intake_complete",
    "drafting",
    "under_evaluation",
    "needs_human_review",
    "waiting_for_approval",
    "done",
)

# Briefing §4: what each sample must produce.
SMOKE_CASES: tuple[dict[str, Any], ...] = (
    {
        "file": "trackflow-request-1.pdf",
        "label": "formal RFP - ModaViva (Spain), warehousing + returns, own carrier",
        "expect": {
            "status": "intake_complete",
            "departments": ["warehouse", "reverse"],
            "client_country": "ES",
            "currency": "EUR",
            # The RFP gives a SKU count, not orders per month: never invented.
            "monthly_volume": None,
        },
        # Part 2: prices in EUR; no last mile, so the warehouse section states the SLA.
        "proposal": {"currency": "EUR", "sla_department": "warehouse"},
    },
    {
        "file": "trackflow-request-2.pdf",
        "label": "informal RFP - Luna Cosmetics (LA), warehousing + last mile",
        "expect": {
            "status": "intake_complete",
            "departments": ["warehouse", "lastmile"],
            "client_country": "US",
            "currency": "USD",
            "monthly_volume": 5000,
        },
        "proposal": {"currency": "USD", "sla_department": "lastmile"},
    },
    {
        "file": "trackflow-request-3.pdf",
        "label": "invalid - RapidShip Carriers' rate pitch to TrackFlow",
        "expect": {"status": "discarded"},
    },
)


class CliError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --- Shared ------------------------------------------------------------------------------


def api_engine() -> Engine:
    """The API's own engine (`DATABASE_URL`), with its schema initialized."""
    from services.core.config import ConfigurationError
    from services.inventory.database import get_engine

    try:
        engine = get_engine()
        store.initialize_schema(engine)
    except (ConfigurationError, ValueError) as error:
        raise CliError(f"database not configured: {error}", 2) from None
    except Exception as error:  # noqa: BLE001 - name the class only, never the URL
        raise CliError(f"database unavailable ({type(error).__name__})", 2) from None
    return engine


def require_models() -> None:
    try:
        get_rag_settings().generation_endpoint()
    except KnowledgeBaseError as error:
        raise CliError(f"the model is not configured: {error}", 2) from None


def create_from_pdf(
    engine: Engine, pdf: Path, settings: RfpIntakeSettings, *, uploaded_by: str
) -> UUID:
    """What the upload route does, from a local file: store the PDF, create the ticket."""
    data = pdf.read_bytes()
    if b"%PDF-" not in data[:1024]:
        raise CliError(f"{pdf} is not a PDF", 2)
    ticket_id = uuid4()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / f"{ticket_id}.pdf"
    shutil.copyfile(pdf, target)
    try:
        stored = target.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        stored = str(target)
    store.create_ticket(
        engine,
        ticket_id=ticket_id,
        original_filename=pdf.name,
        raw_pdf_path=stored,
        pdf_sha256=hashlib.sha256(data).hexdigest(),
        pdf_bytes=len(data),
        uploaded_by=uploaded_by,
    )
    return ticket_id


def detail_or_exit(engine: Engine, ticket_id: str) -> dict[str, Any]:
    try:
        detail = approval_detail.ticket_detail(
            engine, ticket_id, viewer_email=None, settings=get_rfp_settings()
        )
    except ValueError:
        raise CliError(f"not a ticket id: {ticket_id}", 3) from None
    if detail is None:
        raise CliError(f"no ticket {ticket_id}", 3)
    return detail


def render(detail: dict[str, Any]) -> str:
    """The Sales view as text: what is needed from each department and who to ask."""
    lines = [
        f"Ticket {detail['ticket_id']}  ({detail['original_filename']})",
        f"Status: {detail['status']}   tokens used: {detail['llm_tokens_used']}",
    ]
    if detail["status"] == "discarded":
        lines.append(f"Discarded: {detail['discard_reason']}")
    if detail["status"] == "failed":
        lines.append(f"Failed at {detail['error_stage']}: {detail['error_message']}")
    metadata = detail.get("metadata")
    if metadata:
        volume = metadata["monthly_volume"]
        lines += [
            "",
            f"Client: {metadata['client_name'] or '?'} ({metadata['client_country'] or '?'}, "
            f"{metadata['currency'] or 'currency not set'})",
            f"Services: {', '.join(metadata['services_requested']) or 'none stated'}",
            f"Monthly volume: {volume if volume is not None else 'not stated'}",
            f"Deadline: {metadata['deadline'] or metadata['deadline_text'] or 'not stated'}",
            f"Budget: {metadata['budget_range'] or 'not stated'}",
            "Readability: {word_count} words, ~{estimated_tokens} tokens, "
            "grade {flesch_kincaid_grade}, cost {processing_cost}".format(
                **metadata["readability"]
            ),
        ]
    brief = detail.get("sales_brief")
    if brief:
        lines += ["", f"Summary: {brief['summary']}"]
        for dept in brief["departments"]:
            lines += ["", f"{dept['department_name']} - ask {dept['owner']}:", f"  {dept['ask']}"]
            lines += [f"  ? {question}" for question in dept["open_questions"]]
        if brief["client_questions"]:
            lines += ["", "Ask the client:"] + [f"  - {q}" for q in brief["client_questions"]]
        if brief["unassigned_requirements"]:
            lines += ["", "Not covered by a department:"]
            lines += [f"  - {item}" for item in brief["unassigned_requirements"]]
    lines += render_drafts(detail)
    lines += render_approval(detail)
    return "\n".join(lines)


def render_approval(detail: dict[str, Any]) -> list[str]:
    """Part 3: each owner's decision, the arbitration rulings and the final document."""
    approval = detail.get("approval")
    if not approval:
        return []
    lines = [
        "",
        f"Approval (Part 3): {approval['status']}, {approval['approved']} of "
        f"{approval['sections']} section(s) approved; thread {approval['thread_id']}",
    ]
    if approval.get("error_stage"):
        lines.append(
            f"  stopped at {approval['error_stage']}: {approval['error_message']} "
            f"(npm run rfp -- resume {detail['ticket_id']})"
        )
    for ruling in approval.get("rulings") or []:
        lines.append(
            f"  arbitration {ruling['trigger_id']} by {ruling['arbiter']}: {ruling['action']} "
            f"{', '.join(ruling['departments'])}"
        )
    for section in detail["sections"]:
        if not section.get("approval_status"):
            continue
        state = section["approval_status"]
        if state == "approved":
            state = f"approved by {section['approver']} ({section['approved_by']})"
        elif section.get("awaiting_decision"):
            state = f"waiting for {section['owner']}, round {section['review_round']}"
        lines.append(f"  {section['department_name']}: {state}")
        for decision in section.get("decisions") or []:
            comment = f": {decision['comment']}" if decision.get("comment") else ""
            lines.append(
                f"    round {decision['review_round']} {decision['decided_by_name']} "
                f"({decision['source']}) {decision['action']} [{decision['status']}]{comment}"
            )
    if detail.get("final_document"):
        lines.append(
            f"  Final document: {detail['final_document']['generated_at']} "
            f"(npm run rfp -- document {detail['ticket_id']})"
        )
    return lines


def _check(result: dict[str, Any] | None) -> str:
    if not result:
        return "-"
    return "pass" if result.get("pass") else "FAIL"


def render_drafts(detail: dict[str, Any]) -> list[str]:
    """Part 2: each department's draft, its three checks and what to fix."""
    sections = [s for s in detail["sections"] if s.get("draft_status")]
    if not sections:
        return []
    lines = ["", "Proposal draft (Part 2):"]
    proposal = detail.get("proposal")
    if proposal:
        lines.append(
            f"  {proposal['status']}; average attempts per section "
            f"{proposal['average_iterations_per_section']} (target: fewer than 2); "
            f"rate card {proposal['rate_card_version']} (indicative)"
        )
    for section in sections:
        evaluation = section.get("evaluation") or {}
        compliance = evaluation.get("compliance") or {}
        sla = " [states the SLA]" if section.get("is_sla_section") else ""
        lines += [
            "",
            f"  {section['department_name']} (for approval by {section['owner']}){sla}: "
            f"{section['draft_status']}, attempt {section['draft_iterations']} of "
            f"{section.get('max_iterations') or '?'}",
            f"    readability {_check(evaluation.get('readability'))}, relevance "
            f"{_check(evaluation.get('relevance'))}, compliance {_check(compliance)}"
            + (
                f" ({', '.join(compliance.get('rule_ids') or [])})"
                if compliance.get("rule_ids")
                else ""
            ),
        ]
        if section.get("review_note"):
            lines.append(f"    note: {section['review_note']}")
        if evaluation.get("feedback_for_generator") and not evaluation.get("overall_pass"):
            lines += ["    to fix:"] + [
                f"      {line}" for line in evaluation["feedback_for_generator"].splitlines()
            ]
    return lines


# --- Commands --------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    require_models()
    engine = api_engine()
    ticket_id = create_from_pdf(engine, Path(args.pdf), get_rfp_settings(), uploaded_by="cli")
    print(f"ticket {ticket_id}: analyzing...", file=sys.stderr)
    status = runner.run_intake(ticket_id, engine)
    if status == "intake_complete" and not args.no_draft:
        print(f"ticket {ticket_id}: drafting the proposal...", file=sys.stderr)
        status = proposal_runner.draft_ticket(ticket_id, engine)
        if status in DRAFTED and not args.no_approval:
            print(f"ticket {ticket_id}: asking each department owner...", file=sys.stderr)
            approval_runner.start_pending(engine)
            status = store.get_ticket(engine, ticket_id).status  # type: ignore[union-attr]
    print(render(detail_or_exit(engine, str(ticket_id))))
    return 0 if status in COMPLETED else 1


def cmd_reprocess(args: argparse.Namespace) -> int:
    require_models()
    engine = api_engine()
    try:
        status = runner.reprocess(args.ticket_id, engine)
    except store.TicketNotFound:
        raise CliError(f"no ticket {args.ticket_id}", 3) from None
    except store.TicketStateError as error:
        raise CliError(str(error), 3) from None
    print(render(detail_or_exit(engine, args.ticket_id)))
    return 0 if status in {"intake_complete", "discarded"} else 1


def cmd_list(args: argparse.Namespace) -> int:
    engine = api_engine()
    for row in store.list_tickets(engine):
        print(
            f"{row['ticket_id']}  {row['status']:<16} {row['created_at']:%Y-%m-%d %H:%M}  "
            f"{row['client_name'] or '-':<24} {row['original_filename']}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail = detail_or_exit(engine, args.ticket_id)
    print(json.dumps(detail, indent=2, default=str) if args.json else render(detail))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    engine = api_engine()
    payload = handoff.load_handoff(engine, args.ticket_id)
    if payload is None:
        raise CliError(f"ticket {args.ticket_id} has no handoff (not intake_complete)", 3)
    print(payload.model_dump_json(indent=2))
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    require_models()
    engine = api_engine()
    if args.ticket_id:
        detail_or_exit(engine, args.ticket_id)
        try:
            status = proposal_runner.draft_ticket(args.ticket_id, engine)
        except store.TicketStateError as error:
            raise CliError(str(error), 3) from None
        print(render(detail_or_exit(engine, args.ticket_id)))
        return 0 if status in DRAFTED else 1
    done = proposal_runner.run_pending(engine, limit=args.limit)
    for ticket_id, status in done:
        print(f"{ticket_id}  {status}")
    if not done:
        print("no ticket is waiting for drafting", file=sys.stderr)
    return 0 if all(status in DRAFTED for _, status in done) else 1


def cmd_redraft(args: argparse.Namespace) -> int:
    require_models()
    engine = api_engine()
    detail_or_exit(engine, args.ticket_id)
    try:
        status = proposal_runner.redraft(args.ticket_id, engine)
    except (store.TicketNotFound, store.TicketStateError) as error:
        raise CliError(str(error), 3) from None
    print(render(detail_or_exit(engine, args.ticket_id)))
    return 0 if status in DRAFTED else 1


def cmd_proposal(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail_or_exit(engine, args.ticket_id)
    payload = proposal_handoff.load_proposal_handoff(engine, args.ticket_id)
    if payload is None:
        raise CliError(f"ticket {args.ticket_id} has no proposal yet (Part 2 not finished)", 3)
    print(payload.model_dump_json(indent=2, by_alias=True))
    return 0


def cmd_kpi(args: argparse.Namespace) -> int:
    engine = api_engine()
    report = {
        "iterations": proposal_store.iteration_kpi(engine),
        "approval": approval_store.approval_kpi(engine),
    }
    print(json.dumps(report, indent=2))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    if args.part == "1":
        print(draw_mermaid())
        return 0
    drawings = proposal_graph.draw_mermaid() if args.part == "2" else approval_graph.draw_mermaid()
    for name, mermaid in drawings.items():
        print(f"%% {name}\n{mermaid}")
    return 0


def cmd_request_approval(args: argparse.Namespace) -> int:
    engine = api_engine()
    started = approval_runner.start_pending(engine, limit=args.limit)
    for ticket_id, status in started:
        print(f"{ticket_id}  {status}")
    if not started:
        print("no proposal is waiting for its approvals to open", file=sys.stderr)
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail = detail_or_exit(engine, args.ticket_id)
    requests = approval_runner.open_requests(engine, args.ticket_id)
    if not requests:
        print("no department is waiting for a decision", file=sys.stderr)
    for _, request in requests:
        note = " [needs human review]" if request["needs_human_review"] else ""
        account = approvers.approver_email(get_rfp_settings(), request["department_id"])
        print(
            f"{request['department_id']:<10} ask {request['owner']} "
            f"({account or 'no account configured'}), "
            f"round {request['review_round']}, may: {', '.join(request['allowed_actions'])}{note}"
        )
        if request.get("reason"):
            print(f"           because: {request['reason']}")
        if request.get("error"):
            print(f"           last answer refused: {request['error']}")
    print("\n".join(render_approval(detail)))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail = detail_or_exit(engine, args.ticket_id)
    section = next((s for s in detail["sections"] if s["department_id"] == args.department), None)
    review_round = args.round or (section["review_round"] if section else 1)
    action = args.action.replace("-", "_")
    if action == "request_changes":
        require_models()  # the section is redrafted by the live generator
    try:
        approval_runner.submit_decision(
            engine,
            args.ticket_id,
            args.department,
            action=action,
            comment=args.comment,
            review_round=review_round,
            decided_by_email=args.decided_by,
        )
    except decisions.DecisionRefused as refused:
        raise CliError(refused.message, 3) from None
    status = approval_runner.apply_decisions(args.ticket_id, engine)
    print(f"ticket {args.ticket_id}: {status}", file=sys.stderr)
    print("\n".join(render_approval(detail_or_exit(engine, args.ticket_id))))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail_or_exit(engine, args.ticket_id)
    try:
        status = approval_runner.resume(args.ticket_id, engine)
    except store.TicketStateError as error:
        raise CliError(str(error), 3) from None
    print(f"ticket {args.ticket_id}: {status}")
    return 0


def cmd_document(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail_or_exit(engine, args.ticket_id)
    document = approval_detail.document_fields(engine, args.ticket_id)
    if document is None:
        raise CliError(
            f"ticket {args.ticket_id} has no final document yet (every owner must approve)", 3
        )
    if args.output:
        Path(args.output).write_text(document["content"], encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(document["content"])
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    engine = api_engine()
    detail_or_exit(engine, args.ticket_id)
    if args.state:
        entries = approval_runner.state_trace(engine, args.ticket_id)
        if args.json:
            print(json.dumps(entries, indent=2, default=str))
            return 0
        for entry in entries:
            print(
                f"{entry['started_at'][:19]}  {entry['node']:<32} {entry['agent']}\n"
                f"    in:  {json.dumps(entry['input'], default=str)[:160]}\n"
                f"    out: {json.dumps(entry['output'], default=str)[:160]}"
            )
        return 0
    steps = approval_trace.ticket_trace(engine, args.ticket_id)
    if args.json:
        print(json.dumps(steps, indent=2, default=str))
        return 0
    for step in steps:
        took = f"{step['duration_ms']} ms" if step["duration_ms"] is not None else ""
        print(
            f"{step['position']:>3}. Part {step['part']}  {step['at']:%H:%M:%S}  "
            f"{step['node']:<34} {step['outcome']:<9} {took:>9}  {step['agent']}"
        )
    return 0


def schema_block() -> str:
    schema = json.dumps(handoff.json_schema(), indent=2, ensure_ascii=False)
    return f"{SCHEMA_BEGIN}\n```json\n{schema}\n```\n{SCHEMA_END}"


def _write_block(document: Path, begin: str, end: str, block: str) -> None:
    text = document.read_text(encoding="utf-8")
    start, stop = text.index(begin), text.index(end) + len(end)
    document.write_text(text[:start] + block + text[stop:], encoding="utf-8")
    print(f"wrote the schema into {document.relative_to(ROOT_DIR)}", file=sys.stderr)


def cmd_handoff_schema(args: argparse.Namespace) -> int:
    block = schema_block()
    if not args.write:
        print(block)
        return 0
    _write_block(CONTRACT_DOC, SCHEMA_BEGIN, SCHEMA_END, block)
    return 0


def proposal_schema_block() -> str:
    schema = json.dumps(proposal_handoff.json_schema(), indent=2, ensure_ascii=False)
    return f"{PROPOSAL_SCHEMA_BEGIN}\n```json\n{schema}\n```\n{PROPOSAL_SCHEMA_END}"


def cmd_proposal_schema(args: argparse.Namespace) -> int:
    block = proposal_schema_block()
    if not args.write:
        print(block)
        return 0
    _write_block(PROPOSAL_DOC, PROPOSAL_SCHEMA_BEGIN, PROPOSAL_SCHEMA_END, block)
    return 0


def check_case(expect: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    metadata = detail.get("metadata") or {}
    actual = {
        "status": detail["status"],
        "departments": [section["department_id"] for section in detail["sections"]],
        "client_country": metadata.get("client_country"),
        "currency": metadata.get("currency"),
        "monthly_volume": metadata.get("monthly_volume"),
    }
    checks = {key: actual[key] == value for key, value in expect.items()}
    return {"actual": actual, "checks": checks, "passed": all(checks.values())}


def check_proposal(expect: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Part 2 on a live run: briefing §5's currency and SLA, and the handoff's completeness."""
    sections = detail["sections"]
    currencies = {
        line["currency"]
        for section in sections
        for line in ((section.get("draft") or {}).get("price_lines") or [])
    }
    sla_sections = [s["department_id"] for s in sections if s.get("is_sla_section")]
    sla_stated = all(
        (s.get("draft") or {}).get("on_time_sla_pct") == 90
        for s in sections
        if s.get("is_sla_section")
    )
    turnarounds = [
        hours
        for s in sections
        if (hours := (s.get("draft") or {}).get("returns_turnaround_hours")) is not None
    ]
    checks = {
        "status": detail["status"] in DRAFTED,
        "handoff_has_content_and_evaluation": bool(detail.get("proposal"))
        and all(s.get("draft_content") and s.get("evaluation") for s in sections),
        "currency": currencies == {expect["currency"]},
        "sla_department": sla_sections == [expect["sla_department"]],
        "sla_stated": sla_stated,
        "returns_at_least_48h": all(hours >= 48 for hours in turnarounds),
    }
    per_section = {
        s["department_id"]: {
            "draft_status": s.get("draft_status"),
            "iterations": s.get("draft_iterations"),
            "readability": (s.get("evaluation") or {}).get("readability", {}).get("score"),
            "grade": ((s.get("evaluation") or {}).get("readability", {}).get("details") or {}).get(
                "flesch_kincaid_grade"
            ),
            "relevance_missing": (s.get("evaluation") or {})
            .get("relevance", {})
            .get("missing_aspects"),
            "compliance_rule_ids": (s.get("evaluation") or {})
            .get("compliance", {})
            .get("rule_ids"),
            "knowledge_source": s.get("knowledge_source"),
        }
        for s in sections
    }
    attempts = [
        {
            "stage": event["node"],
            "failed": (event["detail"] or {}).get("failed"),
            "decision": (event["detail"] or {}).get("decision"),
        }
        for event in detail.get("events") or []
        if event["node"].startswith("merge_evaluation:") and event["outcome"] == "completed"
    ]
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "status": detail["status"],
        "sections": per_section,
        "attempts": attempts,
        "average_iterations_per_section": (detail.get("proposal") or {}).get(
            "average_iterations_per_section"
        ),
    }


def _forward(statuses: list[str]) -> bool:
    """The statuses only move forward through the three parts (repeats allowed)."""
    positions = [FORWARD.index(status) for status in statuses if status in FORWARD]
    return len(positions) == len(statuses) and positions == sorted(positions)


def simulated_approval(
    engine: Engine,
    ticket_id: UUID,
    settings: RfpIntakeSettings,
    *,
    currency: str,
    change_request: str | None,
    statuses: list[str],
) -> dict[str, Any]:
    """Part 3 with simulated owners: programmatic decisions, the graph resumed with each.

    Every owner approves; with `change_request`, the first owner asks for that
    change instead, and the live generator revises the section first.
    """

    def status() -> str:
        ticket = store.get_ticket(engine, ticket_id)
        return ticket.status if ticket else "missing"

    approval_runner.start_pending(engine, settings=settings, limit=1)
    statuses.append(status())
    made: list[dict[str, Any]] = []
    asked = change_request is None
    for _ in range(12):
        requests = approval_runner.open_requests(engine, ticket_id, settings)
        if not requests:
            break
        for _, request in requests:
            dept = request["department_id"]
            action, comment = "approve", None
            if not asked and "request_changes" in request["allowed_actions"]:
                action, comment, asked = "request_changes", change_request, True
            approval_runner.submit_decision(
                engine,
                ticket_id,
                dept,
                action=action,
                comment=comment,
                review_round=request["review_round"],
                decided_by_email=SMOKE_OWNERS[dept],
                settings=settings,
            )
            made.append({"department": dept, "round": request["review_round"], "action": action})
        approval_runner.apply_decisions(ticket_id, engine, settings=settings)
        statuses.append(status())

    detail = detail_or_exit(engine, str(ticket_id))
    document = approval_detail.document_fields(engine, ticket_id)
    active = [s for s in detail["sections"] if s.get("approval_status")]
    content = (document or {}).get("content") or ""
    checks = {
        "status_done": detail["status"] == "done",
        "every_section_approved_by_its_owner": bool(active)
        and all(s["approval_status"] == "approved" and s["approver"] == s["owner"] for s in active),
        "document_stored": document is not None,
        "document_sections": bool(document)
        and [s["department_id"] for s in document["sections"]]  # type: ignore[index]
        == [s["department_id"] for s in active],
        "document_currency": bool(document) and document["currency"] == currency,  # type: ignore[index]
        "approved_text_in_document": bool(document)
        and all(((s.get("draft") or {}).get("summary") or "") in content for s in active),
        "statuses_forward": _forward(statuses),
    }
    if change_request is not None:
        checks["change_requested_and_revised"] = any(
            (s.get("review_rounds") or 0) >= 1 for s in active
        )
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "status": detail["status"],
        "statuses": statuses,
        "decisions": made,
        "rulings": (detail.get("approval") or {}).get("rulings"),
        "sections": {
            s["department_id"]: {
                "approval_status": s["approval_status"],
                "approver": s.get("approver"),
                "review_rounds": s.get("review_rounds"),
                "draft_status": s.get("draft_status"),
            }
            for s in active
        },
        "document": (
            {
                "currency": document["currency"],
                "sections": [section["department_id"] for section in document["sections"]],
                "characters": len(document["content"]),
                "content_sha256": document["content_sha256"],
            }
            if document
            else None
        ),
        "trace_steps": len(approval_trace.ticket_trace(engine, ticket_id)),
    }


def cmd_smoke(args: argparse.Namespace) -> int:
    require_models()
    pdf_dir = Path(args.pdf_dir)
    with tempfile.TemporaryDirectory(prefix="rfp-smoke-") as scratch:
        engine = create_engine(
            f"sqlite:///{Path(scratch) / 'rfp.sqlite'}",
            connect_args={"check_same_thread": False},
        )
        store.initialize_schema(engine)
        settings = get_rfp_settings().model_copy(
            update={"upload_dir": Path(scratch) / "pdf", "approvers": SMOKE_OWNERS}
        )
        results = []
        change_request: str | None = SMOKE_CHANGE_REQUEST
        for case in SMOKE_CASES:
            pdf = pdf_dir / case["file"]
            if not pdf.is_file():
                raise CliError(f"missing sample PDF: {pdf}", 2)
            ticket_id = create_from_pdf(engine, pdf, settings, uploaded_by="smoke")
            statuses = ["analyzing"]
            started = time.perf_counter()
            runner.run_intake(ticket_id, engine, settings=settings)
            seconds = round(time.perf_counter() - started, 1)
            detail = detail_or_exit(engine, str(ticket_id))
            verdict = check_case(case["expect"], detail)
            classification = detail.get("classification") or {}
            statuses.append(detail["status"])
            drafting = None
            approval = None
            if (
                "proposal" in case
                and not args.skip_drafting
                and detail["status"] == "intake_complete"
            ):
                started = time.perf_counter()
                proposal_runner.draft_ticket(ticket_id, engine, settings=settings)
                drafted = detail_or_exit(engine, str(ticket_id))
                drafting = {
                    **check_proposal(case["proposal"], drafted),
                    "seconds": round(time.perf_counter() - started, 1),
                    "llm_tokens_used": drafted["llm_tokens_used"] - detail["llm_tokens_used"],
                    "error": (
                        f"{drafted['error_stage']}: {drafted['error_message']}"
                        if drafted["error_stage"]
                        else None
                    ),
                    "sections_detail": drafted["sections"],
                    "proposal": drafted.get("proposal"),
                }
                verdict["passed"] = verdict["passed"] and drafting["passed"]
                mark = "PASS" if drafting["passed"] else "FAIL"
                print(
                    f"{mark} {case['file']} drafting: {drafting['status']}, "
                    f"{ {k: v['iterations'] for k, v in drafting['sections'].items()} } "
                    f"({drafting['seconds']}s)",
                    file=sys.stderr,
                )
                statuses.append(drafted["status"])
                if not args.skip_approval and drafted["status"] in DRAFTED:
                    started = time.perf_counter()
                    before = drafted["llm_tokens_used"]
                    approval = simulated_approval(
                        engine,
                        ticket_id,
                        settings,
                        currency=case["proposal"]["currency"],
                        change_request=change_request,
                        statuses=statuses,
                    )
                    change_request = None  # one live revision per smoke run
                    approved = detail_or_exit(engine, str(ticket_id))
                    approval["seconds"] = round(time.perf_counter() - started, 1)
                    approval["llm_tokens_used"] = approved["llm_tokens_used"] - before
                    verdict["passed"] = verdict["passed"] and approval["passed"]
                    mark = "PASS" if approval["passed"] else "FAIL"
                    print(
                        f"{mark} {case['file']} approval: {approval['status']}, "
                        f"{approval['statuses']} ({approval['seconds']}s)",
                        file=sys.stderr,
                    )
            results.append(
                {
                    "file": case["file"],
                    "label": case["label"],
                    "expect": case["expect"],
                    **verdict,
                    "seconds": seconds,
                    "llm_tokens_used": detail["llm_tokens_used"],
                    "classification": {
                        key: classification.get(key)
                        for key in ("document_kind", "confidence", "language", "flags", "reasons")
                    },
                    "discard_reason": detail["discard_reason"],
                    "error": (
                        f"{detail['error_stage']}: {detail['error_message']}"
                        if detail["error_stage"]
                        else None
                    ),
                    "metadata": detail.get("metadata"),
                    "sales_brief": detail.get("sales_brief"),
                    "sections": detail["sections"],
                    "drafting": drafting,
                    "approval": approval,
                }
            )
            mark = "PASS" if verdict["passed"] else "FAIL"
            print(f"{mark} {case['file']}: {verdict['actual']} ({seconds}s)", file=sys.stderr)

    decided = [r for r in results if r["actual"]["status"] in {"intake_complete", "discarded"}]
    correct = sum(1 for r in decided if r["checks"].get("status"))
    draft_runs = [r["drafting"] for r in results if r.get("drafting")]
    iterations = [
        section["iterations"]
        for run in draft_runs
        for section in run["sections"].values()
        if section["iterations"]
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": get_rag_settings().generation_model,
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "correct_classification_rate": round(correct / len(results), 3) if results else None,
        "average_iterations_per_section": (
            round(sum(iterations) / len(iterations), 2) if iterations else None
        ),
        "max_draft_iterations": settings.max_draft_iterations,
        "approvals_done": sum(
            1 for r in results if (r.get("approval") or {}).get("status") == "done"
        ),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    print(
        f"{report['passed']}/{report['cases']} passed; correct classification rate "
        f"{report['correct_classification_rate']}; average iterations per section "
        f"{report['average_iterations_per_section']}; wrote {output}",
        file=sys.stderr,
    )
    return 0 if report["passed"] == report["cases"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rfp", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="create a ticket from a local PDF, analyse and draft it")
    run.add_argument("pdf")
    run.add_argument("--no-draft", action="store_true", help="stop after the intake (Part 1)")
    run.add_argument("--no-approval", action="store_true", help="stop after drafting (Part 2)")
    run.set_defaults(handler=cmd_run)

    reprocess = commands.add_parser("reprocess", help="re-run a failed or discarded ticket")
    reprocess.add_argument("ticket_id")
    reprocess.set_defaults(handler=cmd_reprocess)

    commands.add_parser("list", help="the newest tickets").set_defaults(handler=cmd_list)

    show = commands.add_parser("show", help="one ticket")
    show.add_argument("ticket_id")
    show.add_argument("--json", action="store_true", help="everything, as JSON")
    show.set_defaults(handler=cmd_show)

    handoff_cmd = commands.add_parser("handoff", help="a ticket's Part 2 payload")
    handoff_cmd.add_argument("ticket_id")
    handoff_cmd.set_defaults(handler=cmd_handoff)

    draft_cmd = commands.add_parser("draft", help="draft one ticket's proposal, or the queue")
    draft_cmd.add_argument("ticket_id", nargs="?")
    draft_cmd.add_argument("--limit", type=int, default=None, help="at most this many tickets")
    draft_cmd.set_defaults(handler=cmd_draft)

    redraft = commands.add_parser("redraft", help="run Part 2 again for a drafted ticket")
    redraft.add_argument("ticket_id")
    redraft.set_defaults(handler=cmd_redraft)

    proposal = commands.add_parser("proposal", help="a ticket's Part 3 payload")
    proposal.add_argument("ticket_id")
    proposal.set_defaults(handler=cmd_proposal)

    commands.add_parser(
        "kpi", help="iterations per section, approval time, build time"
    ).set_defaults(handler=cmd_kpi)

    graph = commands.add_parser("graph", help="the graphs as Mermaid")
    graph.add_argument("--part", choices=("1", "2", "3"), default="1")
    graph.set_defaults(handler=cmd_graph)

    request_approval = commands.add_parser(
        "request-approval", help="open the approvals of queued proposals (Part 3)"
    )
    request_approval.add_argument("--limit", type=int, default=None)
    request_approval.set_defaults(handler=cmd_request_approval)

    approvals = commands.add_parser("approvals", help="a ticket's open approval requests")
    approvals.add_argument("ticket_id")
    approvals.set_defaults(handler=cmd_approvals)

    decide = commands.add_parser("decide", help="an owner's decision, applied to the graph")
    decide.add_argument("ticket_id")
    decide.add_argument("department", choices=("warehouse", "lastmile", "reverse"))
    decide.add_argument("action", choices=("approve", "request-changes", "reject"))
    decide.add_argument("--as", dest="decided_by", required=True, help="the owner's account")
    decide.add_argument("--comment", default=None)
    decide.add_argument("--round", type=int, default=None, help="the round reviewed")
    decide.set_defaults(handler=cmd_decide)

    resume_cmd = commands.add_parser("resume", help="continue a stopped approval run")
    resume_cmd.add_argument("ticket_id")
    resume_cmd.set_defaults(handler=cmd_resume)

    document = commands.add_parser("document", help="a done ticket's final document")
    document.add_argument("ticket_id")
    document.add_argument("--output", default=None)
    document.set_defaults(handler=cmd_document)

    trace_cmd = commands.add_parser("trace", help="which agent did what, in order")
    trace_cmd.add_argument("ticket_id")
    trace_cmd.add_argument("--json", action="store_true")
    trace_cmd.add_argument(
        "--state", action="store_true", help="the approval graph state's trace (Part 3)"
    )
    trace_cmd.set_defaults(handler=cmd_trace)

    schema = commands.add_parser("handoff-schema", help="the handoff JSON Schema")
    schema.add_argument("--write", action="store_true", help=f"update {CONTRACT_DOC.name}")
    schema.set_defaults(handler=cmd_handoff_schema)

    proposal_schema = commands.add_parser("proposal-schema", help="the Part 3 JSON Schema")
    proposal_schema.add_argument("--write", action="store_true", help=f"update {PROPOSAL_DOC.name}")
    proposal_schema.set_defaults(handler=cmd_proposal_schema)

    smoke = commands.add_parser("smoke", help="the briefing's three PDFs, live, on SQLite")
    smoke.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    smoke.add_argument("--output", default=str(DEFAULT_SMOKE_OUTPUT))
    smoke.add_argument("--skip-drafting", action="store_true", help="Part 1 only")
    smoke.add_argument("--skip-approval", action="store_true", help="Parts 1 and 2 only")
    smoke.set_defaults(handler=cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
