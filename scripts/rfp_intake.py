"""RFP intake from the command line (Milestone 9 Part 1). Not a second HTTP API.

    npm run rfp -- run PDF-examples/trackflow-request-1.pdf   # one ticket, synchronously
    npm run rfp -- list
    npm run rfp -- show <ticket_id> [--json]                  # the Sales view, or everything
    npm run rfp -- reprocess <ticket_id>                      # a failed or discarded ticket
    npm run rfp -- handoff <ticket_id>                        # the Part 2 payload (JSON)
    npm run rfp -- graph                                      # the graph as Mermaid
    npm run rfp -- handoff-schema [--write]                   # docs/rfp/handoff-contract.md
    LLM_TIMEOUT_SECONDS=120 npm run rfp:smoke                 # the three briefing PDFs, live

`run`, `list`, `show`, `reprocess` and `handoff` use the API's database
(`DATABASE_URL`) and create the `rfp_*` tables when missing, like the API's
start-up. `smoke` uses the real models but a temporary SQLite database, so it
leaves no ticket behind; it writes `data/eval/rfp_intake/smoke.json` and gives
the briefing's "correct classification rate" KPI (§3).

Exit codes: 0 done, 1 a smoke expectation failed or a run did not complete,
2 configuration (models or database), 3 unknown ticket or refused reprocess.
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
from data.process.knowledge_base import KnowledgeBaseError, get_rag_settings

DEFAULT_PDF_DIR = ROOT_DIR / "PDF-examples"
DEFAULT_SMOKE_OUTPUT = ROOT_DIR / "data" / "eval" / "rfp_intake" / "smoke.json"
CONTRACT_DOC = ROOT_DIR / "docs" / "rfp" / "handoff-contract.md"
SCHEMA_BEGIN = "<!-- handoff-schema:begin -->"
SCHEMA_END = "<!-- handoff-schema:end -->"

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
    return "\n".join(lines)


# --- Commands --------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    require_models()
    engine = api_engine()
    ticket_id = create_from_pdf(engine, Path(args.pdf), get_rfp_settings(), uploaded_by="cli")
    print(f"ticket {ticket_id}: analyzing...", file=sys.stderr)
    status = runner.run_intake(ticket_id, engine)
    print(render(detail_or_exit(engine, str(ticket_id))))
    return 0 if status in {"intake_complete", "discarded"} else 1


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


def cmd_graph(args: argparse.Namespace) -> int:
    print(draw_mermaid())
    return 0


def schema_block() -> str:
    schema = json.dumps(handoff.json_schema(), indent=2, ensure_ascii=False)
    return f"{SCHEMA_BEGIN}\n```json\n{schema}\n```\n{SCHEMA_END}"


def cmd_handoff_schema(args: argparse.Namespace) -> int:
    block = schema_block()
    if not args.write:
        print(block)
        return 0
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    start, end = text.index(SCHEMA_BEGIN), text.index(SCHEMA_END) + len(SCHEMA_END)
    CONTRACT_DOC.write_text(text[:start] + block + text[end:], encoding="utf-8")
    print(f"wrote the schema into {CONTRACT_DOC.relative_to(ROOT_DIR)}", file=sys.stderr)
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
                }
            )
            mark = "PASS" if verdict["passed"] else "FAIL"
            print(f"{mark} {case['file']}: {verdict['actual']} ({seconds}s)", file=sys.stderr)

    decided = [r for r in results if r["actual"]["status"] in {"intake_complete", "discarded"}]
    correct = sum(1 for r in decided if r["checks"].get("status"))
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": get_rag_settings().generation_model,
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "correct_classification_rate": round(correct / len(results), 3) if results else None,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    print(
        f"{report['passed']}/{report['cases']} passed; correct classification rate "
        f"{report['correct_classification_rate']}; wrote {output}",
        file=sys.stderr,
    )
    return 0 if report["passed"] == report["cases"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rfp", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="create a ticket from a local PDF and analyse it")
    run.add_argument("pdf")
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

    commands.add_parser("graph", help="the graph as Mermaid").set_defaults(handler=cmd_graph)

    schema = commands.add_parser("handoff-schema", help="the handoff JSON Schema")
    schema.add_argument("--write", action="store_true", help=f"update {CONTRACT_DOC.name}")
    schema.set_defaults(handler=cmd_handoff_schema)

    smoke = commands.add_parser("smoke", help="the briefing's three PDFs, live, on SQLite")
    smoke.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    smoke.add_argument("--output", default=str(DEFAULT_SMOKE_OUTPUT))
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
