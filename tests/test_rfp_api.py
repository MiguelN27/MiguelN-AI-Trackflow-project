"""`/rfp/tickets`: upload, list and read RFP tickets over HTTP (staff only).

The routes run against a SQLite engine (patched in for the API's PostgreSQL
one). Uploads either fake the background run, to check exactly what the route
schedules, or run the real graph with scripted agents through the background
task, to check what Sales then reads.
"""

import ast
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.config import get_rfp_settings
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from services.core import security
from services.rfp import router as rfp_router
from tests.pipelines.rfp_intake.proposal import support as proposal_support
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES, ScriptedAgents, sqlite_engine
from tests.test_agent_api import sign_in

ROUTER = Path(rfp_router.__file__)
FORMAL_PDF = (PDF_DIR / SAMPLES["formal"]).read_bytes()
ROUTES = [("post", "/rfp/tickets"), ("get", "/rfp/tickets"), ("get", f"/rfp/tickets/{'0' * 32}")]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = sqlite_engine(tmp_path / "rfp-api.sqlite")
    monkeypatch.setattr(rfp_router, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def staff(client, engine, monkeypatch):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, user_id = sign_in(client, "sales@trackflow.com", role="manager")
    return client, headers, user_id


@pytest.fixture
def scheduled(monkeypatch):
    """The background runs the route scheduled, instead of running them."""
    calls: list[tuple] = []
    monkeypatch.setattr(runner, "run_intake", lambda *args, **kwargs: calls.append(args))
    return calls


@pytest.fixture
def auto_draft(monkeypatch):
    """Drafting follows the intake (on by default outside the tests)."""
    monkeypatch.setenv("RFP_AUTO_DRAFT", "true")
    monkeypatch.setenv("RFP_COMPLIANCE_REVIEWER", "false")
    get_rfp_settings.cache_clear()
    yield
    get_rfp_settings.cache_clear()


@pytest.fixture
def scripted_drafting(monkeypatch):
    """Run the real Part 2 graphs in the background, with scripted proposal agents."""

    def install(**kwargs) -> proposal_support.ScriptedProposalAgents:
        script = proposal_support.ScriptedProposalAgents(**kwargs)
        monkeypatch.setattr(proposal_runner, "default_agents", script.bundle)
        return script

    return install


@pytest.fixture
def scripted(monkeypatch):
    """Run the real graph in the background task, with the canned outputs for `kind`."""

    def install(kind: str) -> ScriptedAgents:
        script = ScriptedAgents(kind)
        monkeypatch.setattr(runner, "default_agents", script.bundle)
        return script

    return install


def upload(
    client, headers, content: bytes = FORMAL_PDF, name: str = "rfp.pdf", kind="application/pdf"
):
    return client.post("/rfp/tickets", headers=headers, files={"file": (name, content, kind)})


# --- Access -----------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_requires_a_bearer_token(client, engine, method, path):
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_customer_accounts_are_refused(client, engine, monkeypatch, method, path):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, _ = sign_in(client, "someone@example.com")  # self-registered: role `user`

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "This area is available to TrackFlow staff only"}


def test_admins_are_staff_too(client, engine, monkeypatch):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, _ = sign_in(client, "admin@trackflow.com", role="admin")

    assert client.get("/rfp/tickets", headers=headers).status_code == 200


# --- Upload -----------------------------------------------------------------------------


def test_an_upload_stores_the_pdf_creates_the_ticket_and_schedules_the_run(
    staff, engine, scheduled
):
    client, headers, user_id = staff

    response = upload(client, headers, name="ModaViva RFP.pdf")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "analyzing"
    assert body["original_filename"] == "ModaViva RFP.pdf"
    ticket = store.get_ticket(engine, body["ticket_id"])
    assert ticket.uploaded_by == user_id
    assert ticket.pdf_sha256 == hashlib.sha256(FORMAL_PDF).hexdigest()
    assert ticket.pdf_bytes == len(FORMAL_PDF)
    stored = get_rfp_settings().upload_dir / f"{body['ticket_id']}.pdf"
    assert stored.read_bytes() == FORMAL_PDF
    assert ticket.raw_pdf_path == str(stored)  # outside the repo in tests: absolute
    assert [str(args[0]) for args in scheduled] == [body["ticket_id"]]
    assert scheduled[0][1] is engine


def test_the_background_run_takes_the_ticket_to_intake_complete(staff, engine, scripted):
    client, headers, _ = staff
    scripted("formal")

    ticket_id = upload(client, headers).json()["ticket_id"]
    detail = client.get(f"/rfp/tickets/{ticket_id}", headers=headers)

    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "intake_complete"
    assert body["metadata"]["currency"] == "EUR"
    assert [s["department_id"] for s in body["sections"]] == ["warehouse", "reverse"]
    assert body["sections"][0]["department_name"] == "Warehouse Operations"
    assert body["sales_brief"]["departments"][0]["owner"] == "Ana Whitfield"
    assert body["handoff"]["next_stage"] == "drafting"
    assert body["events"][0]["node"] == "convert_to_markdown"
    assert "markdown" not in body  # Sales reads the analysis, not the document


def test_with_auto_draft_the_upload_schedules_drafting_after_the_intake(
    staff, engine, scheduled, auto_draft, monkeypatch
):
    client, headers, _ = staff
    drained: list[tuple] = []
    monkeypatch.setattr(proposal_runner, "run_pending", lambda *args, **kw: drained.append(args))

    upload(client, headers)

    assert len(scheduled) == 1 and drained == [(engine,)]


def test_without_auto_draft_only_the_intake_is_scheduled(staff, engine, scheduled, monkeypatch):
    client, headers, _ = staff
    drained: list[tuple] = []
    monkeypatch.setattr(proposal_runner, "run_pending", lambda *args, **kw: drained.append(args))

    upload(client, headers)

    assert len(scheduled) == 1 and drained == []


def test_an_upload_ends_with_drafts_and_evaluations_for_every_department(
    staff, engine, scripted, scripted_drafting, auto_draft
):
    client, headers, _ = staff
    scripted("formal")
    scripted_drafting()

    ticket_id = upload(client, headers).json()["ticket_id"]
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=headers).json()

    assert body["status"] == "waiting_for_approval"
    assert body["proposal"]["contract_version"] == "rfp-proposal/1"
    assert body["proposal"]["sla_department"] == "warehouse"
    assert body["proposal"]["average_iterations_per_section"] == 1.0
    for section in body["sections"]:
        assert section["draft_status"] == "passed"
        assert section["draft_content"].startswith("## ")
        evaluation = section["evaluation"]
        assert evaluation["overall_pass"] is True
        assert evaluation["readability"]["pass"] is True  # the ticket's field name
        assert evaluation["compliance"]["rule_ids"] == []
        assert {line["currency"] for line in section["draft"]["price_lines"]} == {"EUR"}
    assert body["sections"][0]["is_sla_section"] is True
    assert body["sections"][0]["draft"]["storage_site"] == "Zaragoza"  # department fields kept


def test_a_section_that_never_passes_is_shown_for_human_review(
    staff, engine, scripted, scripted_drafting, auto_draft, monkeypatch
):
    client, headers, _ = staff
    monkeypatch.setenv("RFP_MAX_DRAFT_ITERATIONS", "2")
    get_rfp_settings.cache_clear()
    scripted("formal")
    scripted_drafting(
        drafts={
            "warehouse": [proposal_support.good("warehouse")],
            "reverse": [proposal_support.bad_returns],
        }
    )

    ticket_id = upload(client, headers).json()["ticket_id"]
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=headers).json()

    assert body["status"] == "needs_human_review"
    assert body["proposal"]["needs_human_review"] is True
    reverse = body["sections"][1]
    assert reverse["needs_human_review"] is True
    assert reverse["draft_iterations"] == 2 and reverse["max_iterations"] == 2
    assert reverse["evaluation"]["compliance"]["pass"] is False
    assert reverse["evaluation"]["compliance"]["rule_ids"] == ["returns-min-48h"]
    assert "[returns-min-48h]" in reverse["evaluation"]["feedback_for_generator"]


def test_the_carrier_pitch_is_discarded_with_its_reason(staff, engine, scripted):
    client, headers, _ = staff
    scripted("invalid")
    pitch = (PDF_DIR / SAMPLES["invalid"]).read_bytes()

    ticket_id = upload(client, headers, pitch).json()["ticket_id"]
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=headers).json()

    assert body["status"] == "discarded"
    assert body["discard_reason"].startswith("Not a client RFP")
    assert body["classification"]["document_kind"] == "carrier_or_vendor_offer"


@pytest.mark.parametrize(
    ("content", "name", "kind", "status", "detail"),
    [
        (b"hello", "notes.txt", "text/plain", 415, "Upload the RFP as a PDF file."),
        (
            b"PK\x03\x04 not a pdf",
            "rfp.pdf",
            "application/pdf",
            415,
            "The uploaded file is not a valid PDF.",
        ),
        (b"", "rfp.pdf", "application/pdf", 422, "The uploaded file is empty."),
    ],
)
def test_what_is_not_a_pdf_is_refused(
    staff, engine, scheduled, content, name, kind, status, detail
):
    client, headers, _ = staff

    response = upload(client, headers, content, name, kind)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert scheduled == []
    assert store.list_tickets(engine) == []


def test_an_oversized_pdf_is_refused(staff, engine, scheduled, monkeypatch):
    client, headers, _ = staff
    monkeypatch.setenv("RFP_MAX_UPLOAD_MB", "1")
    get_rfp_settings.cache_clear()

    response = upload(client, headers, b"%PDF-1.7\n" + b"0" * (1024 * 1024))

    assert response.status_code == 413
    assert response.json() == {"detail": "The PDF is larger than 1 MB."}
    assert scheduled == []


def test_the_filename_keeps_no_path(staff, engine, scheduled):
    client, headers, _ = staff

    body = upload(client, headers, name="../../etc/C:\\temp\\rfp.pdf").json()

    assert body["original_filename"] == "rfp.pdf"


def test_a_database_failure_is_a_503_and_leaves_no_pdf(staff, engine, scheduled, monkeypatch):
    client, headers, _ = staff

    def down(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("postgresql://user:secret@db"))

    monkeypatch.setattr(store, "create_ticket", down)

    response = upload(client, headers)

    assert response.status_code == 503
    assert response.json() == {"detail": "RFP storage unavailable"}
    assert "secret" not in response.text
    assert list(get_rfp_settings().upload_dir.glob("*")) == []
    assert scheduled == []


# --- Reads ------------------------------------------------------------------------------


def test_the_list_is_newest_first(staff, engine, scheduled):
    client, headers, _ = staff
    first = upload(client, headers, name="first.pdf").json()["ticket_id"]
    second = upload(client, headers, name="second.pdf").json()["ticket_id"]

    response = client.get("/rfp/tickets", headers=headers)

    assert response.status_code == 200
    rows = response.json()["tickets"]
    assert [row["ticket_id"] for row in rows] == [second, first]
    assert rows[0]["status"] == "analyzing"
    assert rows[0]["current_stage"] == "queued"


def test_an_unknown_ticket_is_404_and_a_malformed_id_is_422(staff, engine):
    client, headers, _ = staff

    assert client.get(f"/rfp/tickets/{'0' * 32}", headers=headers).status_code == 404
    assert client.get("/rfp/tickets/not-a-uuid", headers=headers).status_code == 422


def test_a_database_failure_on_read_is_a_503(staff, engine, monkeypatch):
    client, headers, _ = staff

    def down(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(store, "list_tickets", down)

    assert client.get("/rfp/tickets", headers=headers).status_code == 503


# --- Layering ---------------------------------------------------------------------------


def test_the_router_only_triggers_and_queries_the_pipeline():
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    pipeline = {m for m in modules if m.startswith("data.")}
    assert pipeline == {
        "data.pipelines.rfp_intake",
        "data.pipelines.rfp_intake.config",
        "data.pipelines.rfp_intake.proposal",
    }
    assert not {m for m in modules if m.split(".")[0] in {"langgraph", "openai", "markitdown"}}
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"runner", "store"}
    }
    assert calls == {
        "runner.sweep",
        "store.create_ticket",
        "store.list_tickets",
        "store.get_ticket_detail",
    }
