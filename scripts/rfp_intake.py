"""RFP intake and proposal drafting from the command line (Milestone 9 Parts 1-2).

Not a second HTTP API.

    npm run rfp -- run PDF-examples/trackflow-request-1.pdf   # intake + drafting, synchronously
    npm run rfp -- list
    npm run rfp -- show <ticket_id> [--json]                  # the Sales view, or everything
    npm run rfp -- reprocess <ticket_id>                      # a failed or discarded ticket
    npm run rfp -- handoff <ticket_id>                        # the Part 2 input (JSON)
    npm run rfp -- draft [<ticket_id>]                        # draft one ticket, or the queue
    npm run rfp -- redraft <ticket_id>                        # Part 2 again, from the handoff
    npm run rfp -- proposal <ticket_id>                       # the Part 3 payload (JSON)
    npm run rfp -- kpi                                        # average iterations per section
    npm run rfp -- graph [--part 1|2]                         # the graphs as Mermaid
    npm run rfp -- handoff-schema [--write]                   # docs/rfp/handoff-contract.md
    npm run rfp -- proposal-schema [--write]                  # the Part 3 contract doc
    LLM_TIMEOUT_SECONDS=120 npm run rfp:smoke                 # the three briefing PDFs, live

All commands but `smoke`, `graph` and the schemas use the API's database
(`DATABASE_URL`) and create the `rfp_*` tables when missing, like the API's
start-up. `smoke` uses the real models but a temporary SQLite database, so it
leaves no ticket behind. It runs intake and then drafting on each PDF, and
writes `data/eval/rfp_intake/smoke.json` with the briefing's KPIs (§3):
correct classification rate and average iterations per section.

Exit codes: 0 done, 1 a smoke expectation failed or a run did not complete,
2 configuration (models or database), 3 unknown ticket or refused
reprocess/draft/redraft.
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
        detail = store.get_ticket_detail(engine, ticket_id)
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
    return "\n".join(lines)


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
    print(json.dumps(proposal_store.iteration_kpi(api_engine()), indent=2))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    if args.part == "1":
        print(draw_mermaid())
        return 0
    for name, mermaid in proposal_graph.draw_mermaid().items():
        print(f"%% {name}\n{mermaid}")
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


def cmd_smoke(args: argparse.Namespace) -> int:
    require_models()
    pdf_dir = Path(args.pdf_dir)
    with tempfile.TemporaryDirectory(prefix="rfp-smoke-") as scratch:
        engine = create_engine(
            f"sqlite:///{Path(scratch) / 'rfp.sqlite'}",
            connect_args={"check_same_thread": False},
        )
        store.initialize_schema(engine)
        settings = get_rfp_settings().model_copy(update={"upload_dir": Path(scratch) / "pdf"})
        results = []
        for case in SMOKE_CASES:
            pdf = pdf_dir / case["file"]
            if not pdf.is_file():
                raise CliError(f"missing sample PDF: {pdf}", 2)
            ticket_id = create_from_pdf(engine, pdf, settings, uploaded_by="smoke")
            started = time.perf_counter()
            runner.run_intake(ticket_id, engine, settings=settings)
            seconds = round(time.perf_counter() - started, 1)
            detail = detail_or_exit(engine, str(ticket_id))
            verdict = check_case(case["expect"], detail)
            classification = detail.get("classification") or {}
            drafting = None
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

    commands.add_parser("kpi", help="average iterations per section").set_defaults(handler=cmd_kpi)

    graph = commands.add_parser("graph", help="the graphs as Mermaid")
    graph.add_argument("--part", choices=("1", "2"), default="1")
    graph.set_defaults(handler=cmd_graph)

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
