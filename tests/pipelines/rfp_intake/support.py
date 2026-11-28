"""Shared fixtures for the RFP intake tests: the sample PDFs, canned model outputs, fakes.

No test reaches a model or PostgreSQL. The canned outputs mirror what the live
models returned for the briefing's three PDFs (`npm run rfp:smoke`), and the
scripted agents run them through the *real* deterministic guards (`decide`,
`finalize`, `reconcile`, `check`), so the graph tests exercise everything but
the model call itself.
"""

import json
import shutil
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlmodel import create_engine

from data.pipelines.rfp_intake import conversion, store
from data.pipelines.rfp_intake.agents import (
    AgentReply,
    IntakeAgents,
    classifier,
    metadata,
    orchestrator,
    synthesizer,
    workers,
)
from data.pipelines.rfp_intake.llm import Usage

ROOT = Path(__file__).resolve().parents[3]
PDF_DIR = ROOT / "PDF-examples"
SAMPLES = {
    "formal": "trackflow-request-1.pdf",
    "informal": "trackflow-request-2.pdf",
    "invalid": "trackflow-request-3.pdf",
}


@cache
def sample_markdown(kind: str) -> str:
    return conversion.pdf_to_markdown(PDF_DIR / SAMPLES[kind])


def sqlite_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    store.initialize_schema(engine)
    return engine


def new_ticket(engine: Engine, directory: Path, kind: str = "formal") -> UUID:
    """A ticket as the upload route leaves it: PDF stored, status `analyzing`."""
    ticket_id = uuid4()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{ticket_id}.pdf"
    if kind in SAMPLES:
        shutil.copyfile(PDF_DIR / SAMPLES[kind], target)
    else:
        target.write_bytes(kind.encode())
    store.create_ticket(
        engine,
        ticket_id=ticket_id,
        original_filename=SAMPLES.get(kind, "broken.pdf"),
        raw_pdf_path=str(target),
        pdf_sha256="0" * 64,
        pdf_bytes=target.stat().st_size,
        uploaded_by="staff-uuid",
    )
    return ticket_id


# --- Canned model outputs (what the models returned live) ------------------------------

CLASSIFICATIONS = {
    "formal": classifier.DocumentClassification(
        document_kind="formal_rfp",
        is_rfp=True,
        confidence=0.98,
        language="mixed",
        sender="ModaViva, S.L.",
        reasons=["A structured RFP with a reference number, scope and evaluation criteria."],
        evidence_quotes=["REQUEST FOR PROPOSAL (RFP)", "Issuing Organization: ModaViva, S.L."],
    ),
    "informal": classifier.DocumentClassification(
        document_kind="informal_rfp",
        is_rfp=True,
        confidence=0.99,
        language="en",
        sender="Luna Cosmetics",
        reasons=["An e-commerce brand describes its logistics needs and asks for a proposal."],
        evidence_quotes=["Could you send over a proposal?"],
    ),
    "invalid": classifier.DocumentClassification(
        document_kind="carrier_or_vendor_offer",
        is_rfp=False,
        confidence=0.99,
        language="en",
        sender="RapidShip Carriers",
        reasons=["RapidShip is offering its own carrier services and rates to TrackFlow."],
        evidence_quotes=[
            "We'd love the opportunity to become one of TrackFlow's preferred carrier partners."
        ],
    ),
}

EXTRACTED_METADATA = {
    "formal": metadata.ExtractedMetadata(
        client_name="ModaViva, S.L.",
        client_name_quote="Issuing Organization: ModaViva, S.L.",
        client_country="ES",
        client_country_quote="ModaViva es una marca de moda española con venta online en España",
        services_requested=["warehousing", "reverse_logistics"],
        services_quotes=[
            "Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano a Zaragoza",
            "Gestión de devoluciones: inspección, reacondicionamiento y restock",
        ],
        monthly_volume=None,
        deadline="2026-08-28",
        deadline_text="Proposal Due Date: August 28, 2026",
        deadline_kind="proposal_due",
    ),
    "informal": metadata.ExtractedMetadata(
        client_name="Luna Cosmetics",
        client_name_quote="We're Luna Cosmetics, a small direct-to-consumer skincare brand",
        client_country="US",
        client_country_quote="Luna Cosmetics — Los Angeles, CA, USA",
        services_requested=["warehousing", "last_mile"],
        services_quotes=["We need someone to handle warehousing and shipping for us across the US"],
        monthly_volume=5000,
        monthly_volume_quote="we just crossed 5,000 orders a month",
        deadline=None,
        deadline_text="around August 24th",
        deadline_kind="service_start",
    ),
}

PLANS = {
    "formal": orchestrator.DepartmentPlan(
        subtasks=[
            orchestrator.DepartmentSubtask(
                department_id="warehouse",
                objective="Size storage near Zaragoza and quote cost per pallet/SKU.",
                relevant_extracts=[
                    # A copied list number, as the live orchestrator writes them.
                    "1. Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano "
                    "a Zaragoza",
                    "Proposal Due Date: August 28, 2026",
                ],
            ),
            orchestrator.DepartmentSubtask(
                department_id="reverse",
                objective="Quote returns inspection, reconditioning and restock.",
                relevant_extracts=[
                    "Gestión de devoluciones: inspección, reacondicionamiento y restock"
                ],
            ),
        ],
        unassigned_requirements=["Integration with the client's ERP via API."],
    ),
    "informal": orchestrator.DepartmentPlan(
        subtasks=[
            orchestrator.DepartmentSubtask(
                department_id="warehouse",
                objective="Quote US warehousing for a growing DTC skincare brand.",
                relevant_extracts=[
                    "we just crossed 5,000 orders a month",
                    "We need someone to handle warehousing and shipping for us across the US",
                ],
            ),
            orchestrator.DepartmentSubtask(
                department_id="lastmile",
                objective="Quote US shipping and carrier selection.",
                relevant_extracts=[
                    "we just crossed 5,000 orders a month",
                    "ideally someone\nwho can also handle carrier selection since we don't have "
                    "relationships with any carriers yet",
                ],
            ),
        ],
    ),
}

ANALYSES = {
    "warehouse": workers.DepartmentAnalysis(
        key_aspects=[
            workers.KeyAspect(
                aspect="Storage volume",
                detail="Storage for approximately 15,000 SKUs near Zaragoza.",
                source_quote="Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano a "
                "Zaragoza",
            )
        ],
        needed_from_department=["Cost per pallet and per SKU in EUR", "Onboarding time"],
        open_questions=["What units per SKU and pallet mix should we plan for?"],
    ),
    "reverse": workers.DepartmentAnalysis(
        key_aspects=[
            workers.KeyAspect(
                aspect="Returns scope",
                detail="Inspection, reconditioning and restock of returns.",
                source_quote="2. Gestión de devoluciones: inspección, reacondicionamiento y "
                "restock",
            )
        ],
        needed_from_department=["Returns processing cost and turnaround time"],
        open_questions=["What monthly returns volume should we assume?"],
    ),
    "lastmile": workers.DepartmentAnalysis(
        key_aspects=[
            workers.KeyAspect(
                aspect="Monthly orders",
                detail="About 5,000 orders a month shipped across the US.",
                source_quote="we just crossed 5,000 orders a month",
            )
        ],
        needed_from_department=["Cost per shipment in USD", "Carriers by US destination"],
        open_questions=["Which US regions receive most orders?"],
    ),
}

BRIEFS = {
    "formal": synthesizer.SalesBrief(
        summary="ModaViva, S.L. (Spain) requests warehousing near Zaragoza and returns "
        "handling; the proposal is due August 28, 2026.",
        department_asks=[
            synthesizer.DepartmentAsk(
                department_id="warehouse",
                ask="Quote storage for about 15,000 SKUs near Zaragoza, in EUR.",
            ),
            synthesizer.DepartmentAsk(
                department_id="Sofía Ramos", ask="Quote returns processing cost and turnaround."
            ),
        ],
        client_questions=["What monthly returns volume should we assume?"],
        risks=["The monthly order volume is not stated."],
    ),
    "informal": synthesizer.SalesBrief(
        summary="Luna Cosmetics (Los Angeles) needs US warehousing and shipping for about "
        "5,000 orders a month, live around August 24th.",
        department_asks=[
            synthesizer.DepartmentAsk(department_id="warehouse", ask="Quote US storage in USD."),
            synthesizer.DepartmentAsk(department_id="lastmile", ask="Quote cost per shipment."),
        ],
        client_questions=["Which US regions do you ship to most?"],
        risks=["Go-live in about three weeks."],
    ),
}

USAGE = Usage(prompt_tokens=100, completion_tokens=20)


@dataclass
class ScriptedAgents:
    """Canned model outputs through the real guards. Records what each agent received."""

    kind: str
    fail: dict[str, BaseException] = field(default_factory=dict)
    calls: dict[str, list[Any]] = field(default_factory=dict)

    def _record(self, name: str, received: Any) -> None:
        self.calls.setdefault(name, []).append(received)
        if name in self.fail:
            raise self.fail.pop(name)

    def classify_document(self, markdown: str) -> AgentReply:
        self._record("classify_document", markdown)
        return AgentReply(classifier.decide(markdown, CLASSIFICATIONS[self.kind]), USAGE)

    def extract_metadata(self, markdown: str) -> AgentReply:
        self._record("extract_metadata", markdown)
        return AgentReply(metadata.finalize(markdown, EXTRACTED_METADATA[self.kind]), USAGE)

    def plan_departments(self, markdown: str, meta: dict[str, Any]) -> AgentReply:
        self._record("plan_departments", (markdown, meta))
        return AgentReply(orchestrator.reconcile(markdown, meta, PLANS[self.kind]), USAGE)

    def analyze_department(self, task: dict[str, Any]) -> AgentReply:
        self._record("analyze_department", task)
        return AgentReply(workers.check(task, ANALYSES[task["department_id"]]), USAGE)

    def synthesize_brief(
        self, meta: dict[str, Any], sections: list[dict[str, Any]], plan: dict[str, Any]
    ) -> AgentReply:
        self._record("synthesize_brief", (meta, sections, plan))
        return AgentReply(synthesizer.finalize(meta, sections, plan, BRIEFS[self.kind]), USAGE)

    def bundle(self) -> IntakeAgents:
        return IntakeAgents(
            classify_document=self.classify_document,
            extract_metadata=self.extract_metadata,
            plan_departments=self.plan_departments,
            analyze_department=self.analyze_department,
            synthesize_brief=self.synthesize_brief,
        )


# --- A fake OpenAI client, for the agents' own tests ---------------------------------------


class FakeModel:
    """Answers `chat.completions.create` with queued tool-call arguments.

    Each queued reply is a dict (the tool's arguments), a raw string (sent as
    is), an exception (raised), or None (a reply with no tool call).
    """

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        name = request["tools"][0]["function"]["name"]
        calls = None
        if reply is not None:
            arguments = reply if isinstance(reply, str) else json.dumps(reply)
            calls = [SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))]
        message = SimpleNamespace(content=None, tool_calls=calls)
        usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    @property
    def system(self) -> str:
        return self.requests[-1]["messages"][0]["content"]

    @property
    def user(self) -> str:
        return self.requests[-1]["messages"][1]["content"]
