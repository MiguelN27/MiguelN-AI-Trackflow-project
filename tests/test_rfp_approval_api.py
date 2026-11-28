"""Part 3 over HTTP: owners decide on their own section, the document and the trace are served.

The routes run against a SQLite engine (patched in for the API's PostgreSQL
one). An upload runs Parts 1 and 2 with scripted agents and opens the
approvals in the same background tasks, as `RFP_AUTO_DRAFT` and
`RFP_AUTO_REQUEST_APPROVAL` do outside the tests. The TestClient runs
background tasks before it returns, so the resume after a decision has
happened by the next request.
"""

import pytest
from sqlalchemy.exc import OperationalError

from data.pipelines.rfp_intake import runner
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.config import get_rfp_settings
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from services.core import security
from services.rfp import router as rfp_router
from tests.pipelines.rfp_intake.approval.support import OWNERS
from tests.pipelines.rfp_intake.proposal import support as proposal_support
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES, ScriptedAgents, sqlite_engine
from tests.test_agent_api import sign_in

TICKET = "0" * 32
ROUTES = [
    ("post", f"/rfp/tickets/{TICKET}/sections/reverse/decision"),
    ("get", f"/rfp/tickets/{TICKET}/document"),
    ("get", f"/rfp/tickets/{TICKET}/document.md"),
    ("get", f"/rfp/tickets/{TICKET}/trace"),
]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = sqlite_engine(tmp_path / "rfp-api.sqlite")
    monkeypatch.setattr(rfp_router, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def accounts(client, engine, monkeypatch):
    """Sales and the three owners (manager accounts), signed in."""
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers = {"sales": sign_in(client, "sales@trackflow.com", role="manager")[0]}
    for dept, email in OWNERS.items():
        headers[dept] = sign_in(client, email, role="manager")[0]
    headers["admin"] = sign_in(client, "admin@trackflow.com", role="admin")[0]
    return headers


@pytest.fixture
def pipeline(monkeypatch):
    """Parts 1-3 run in the upload's background tasks, with scripted agents."""
    monkeypatch.setenv("RFP_AUTO_DRAFT", "true")
    monkeypatch.setenv("RFP_AUTO_REQUEST_APPROVAL", "true")
    monkeypatch.setenv("RFP_COMPLIANCE_REVIEWER", "false")
    get_rfp_settings.cache_clear()
    proposal = proposal_support.ScriptedProposalAgents()
    monkeypatch.setattr(proposal_runner, "default_agents", proposal.bundle)
    monkeypatch.setattr(approval_runner, "default_agents", proposal.bundle)

    def install(kind: str) -> proposal_support.ScriptedProposalAgents:
        monkeypatch.setattr(runner, "default_agents", ScriptedAgents(kind).bundle)
        return proposal

    yield install
    get_rfp_settings.cache_clear()


def uploaded(client, headers, kind: str = "formal") -> str:
    pdf = (PDF_DIR / SAMPLES[kind]).read_bytes()
    reply = client.post(
        "/rfp/tickets", headers=headers, files={"file": ("rfp.pdf", pdf, "application/pdf")}
    )
    assert reply.status_code == 202
    return reply.json()["ticket_id"]


def section(body: dict, dept: str) -> dict:
    return next(s for s in body["sections"] if s["department_id"] == dept)


def decide(client, headers, ticket_id, dept, action, comment=None, review_round=1):
    return client.post(
        f"/rfp/tickets/{ticket_id}/sections/{dept}/decision",
        headers=headers,
        json={"action": action, "comment": comment, "review_round": review_round},
    )


# --- Access -------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_the_part_3_routes_need_a_staff_session(client, engine, monkeypatch, method, path):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    body = {"json": {"action": "approve", "review_round": 1}} if method == "post" else {}
    assert getattr(client, method)(path, **body).status_code == 401
    customer, _ = sign_in(client, "shopper@example.com")
    assert getattr(client, method)(path, headers=customer, **body).status_code == 403


# --- The flow -----------------------------------------------------------------------------


def test_owners_decide_over_http_and_the_final_document_is_served(client, accounts, pipeline):
    pipeline("formal")
    ticket_id = uploaded(client, accounts["sales"])

    # Sales sees the requests; only each owner may decide on their own section.
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["sales"]).json()
    assert body["status"] == "waiting_for_approval"
    assert body["approval"]["status"] == "paused"
    assert body["approval"]["thread_id"] == f"rfp-{ticket_id}"
    assert (body["approval"]["approved"], body["approval"]["sections"]) == (0, 2)
    for dept in ("warehouse", "reverse"):
        assert section(body, dept)["awaiting_decision"] is True
        assert section(body, dept)["can_decide"] is False
    as_sofia = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["reverse"]).json()
    assert section(as_sofia, "reverse")["can_decide"] is True
    assert section(as_sofia, "warehouse")["can_decide"] is False
    assert as_sofia["approval"]["viewer_departments"] == ["reverse"]

    # Sofía approves reverse while Ana's warehouse request stays open.
    reply = decide(client, accounts["reverse"], ticket_id, "reverse", "approve", "Fine by me.")
    assert reply.status_code == 202
    assert reply.json()["decided_by"] == OWNERS["reverse"]
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["sales"]).json()
    assert section(body, "reverse")["approval_status"] == "approved"
    assert section(body, "reverse")["approver"] == "Sofía Ramos"
    assert [d["status"] for d in section(body, "reverse")["decisions"]] == ["applied"]
    assert section(body, "warehouse")["approval_status"] == "pending"
    assert body["status"] == "waiting_for_approval"
    assert body["final_document"] is None
    missing = client.get(f"/rfp/tickets/{ticket_id}/document", headers=accounts["sales"])
    assert missing.status_code == 404
    assert "every department owner has approved" in missing.json()["detail"]

    assert decide(client, accounts["warehouse"], ticket_id, "warehouse", "approve").status_code == (
        202
    )
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["sales"]).json()
    assert body["status"] == "done"
    assert body["approval"]["status"] == "done"
    assert body["final_document"]["sections"] == ["warehouse", "reverse"]

    document = client.get(f"/rfp/tickets/{ticket_id}/document", headers=accounts["sales"]).json()
    assert document["currency"] == "EUR"
    assert [s["department_id"] for s in document["sections"]] == ["warehouse", "reverse"]
    assert {"ticket_id", "sections", "currency", "generated_at"} <= set(document)
    download = client.get(
        f"/rfp/tickets/{ticket_id}/document.md",
        headers={**accounts["sales"], "Origin": "http://localhost:3001"},
    )
    assert download.headers["content-type"].startswith("text/markdown")
    # The backoffice is another origin: it can read the file name only if it is exposed.
    assert "content-disposition" in download.headers["access-control-expose-headers"].lower()
    assert 'filename="trackflow-proposal-modaviva-sl.md"' in download.headers["content-disposition"]
    assert download.text == document["content"]

    steps = client.get(f"/rfp/tickets/{ticket_id}/trace", headers=accounts["sales"]).json()["steps"]
    assert {step["part"] for step in steps} == {1, 2, 3}
    assert steps[-1]["step"] == "complete_approval"
    owner_step = next(s for s in steps if s["step"] == "sign_off" and s["department"] == "reverse")
    assert owner_step["agent"] == "Sofía Ramos (owner of Reverse Logistics)"


def test_request_changes_over_http_revises_the_section_for_another_review(
    client, accounts, pipeline
):
    script = pipeline("informal")
    ticket_id = uploaded(client, accounts["sales"], "informal")

    reply = decide(
        client, accounts["lastmile"], ticket_id, "lastmile", "request_changes", "Add Nevada."
    )
    assert reply.status_code == 202

    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["lastmile"]).json()
    lastmile = section(body, "lastmile")
    assert (lastmile["review_round"], lastmile["review_rounds"]) == (2, 1)
    assert lastmile["approval_status"] == "pending" and lastmile["can_decide"] is True
    assert "generate:lastmile#2" in script.calls
    stale = decide(client, accounts["lastmile"], ticket_id, "lastmile", "approve", review_round=1)
    assert stale.status_code == 409
    assert (
        decide(
            client, accounts["lastmile"], ticket_id, "lastmile", "approve", review_round=2
        ).status_code
        == 202
    )


def test_a_rejection_over_http_ends_the_proposal(client, accounts, pipeline):
    pipeline("informal")
    ticket_id = uploaded(client, accounts["sales"], "informal")

    reply = decide(
        client, accounts["warehouse"], ticket_id, "warehouse", "reject", "No capacity in Q3."
    )
    assert reply.status_code == 202

    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["sales"]).json()
    assert body["status"] == "rejected"
    assert body["approval"]["status"] == "rejected"
    assert section(body, "lastmile")["approval_status"] == "withdrawn"
    assert section(body, "lastmile")["can_decide"] is False
    assert client.get(
        f"/rfp/tickets/{ticket_id}/document", headers=accounts["sales"]
    ).status_code == (404)


@pytest.mark.parametrize(
    ("who", "dept", "action", "comment", "review_round", "status", "message"),
    [
        ("warehouse", "reverse", "approve", None, 1, 403, "Only Sofía Ramos"),
        ("admin", "reverse", "approve", None, 1, 403, "Only Sofía Ramos"),
        ("sales", "warehouse", "approve", None, 1, 403, "Only Ana Whitfield"),
        ("reverse", "reverse", "reject", None, 1, 422, "comment is required"),
        ("reverse", "reverse", "approve", None, 2, 409, "changed since you opened it"),
        ("lastmile", "lastmile", "approve", None, 1, 404, "no lastmile section"),
    ],
)
def test_a_decision_is_refused_with_a_clear_reason(
    client, accounts, pipeline, who, dept, action, comment, review_round, status, message
):
    pipeline("formal")
    ticket_id = uploaded(client, accounts["sales"])

    reply = decide(client, accounts[who], ticket_id, dept, action, comment, review_round)

    assert reply.status_code == status
    assert message in reply.json()["detail"]
    body = client.get(f"/rfp/tickets/{ticket_id}", headers=accounts["sales"]).json()
    assert {s["approval_status"] for s in body["sections"]} == {"pending"}


def test_malformed_bodies_and_unknown_targets_are_refused(client, accounts, pipeline):
    pipeline("formal")
    ticket_id = uploaded(client, accounts["sales"])
    headers = accounts["reverse"]
    url = f"/rfp/tickets/{ticket_id}/sections"

    assert (
        client.post(
            f"{url}/sales/decision", headers=headers, json={"action": "approve", "review_round": 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{url}/reverse/decision", headers=headers, json={"action": "maybe", "review_round": 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{url}/reverse/decision",
            headers=headers,
            json={"action": "approve", "review_round": 1, "extra": 1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{url}/reverse/decision",
            headers=headers,
            json={"action": "approve", "review_round": 0},
        ).status_code
        == 422
    )
    unknown = f"/rfp/tickets/{'f' * 32}/sections/reverse/decision"
    assert (
        client.post(
            unknown, headers=headers, json={"action": "approve", "review_round": 1}
        ).status_code
        == 404
    )
    assert client.get(f"/rfp/tickets/{'f' * 32}/trace", headers=headers).status_code == 404


def test_a_database_failure_is_a_503_that_names_nothing(client, accounts, pipeline, monkeypatch):
    pipeline("formal")
    ticket_id = uploaded(client, accounts["sales"])

    def down(*args, **kwargs):
        raise OperationalError("SELECT secret FROM vault", {}, Exception("password=hunter2"))

    monkeypatch.setattr(approval_runner, "submit_decision", down)
    reply = decide(client, accounts["reverse"], ticket_id, "reverse", "approve")

    assert reply.status_code == 503
    assert reply.json() == {"detail": "RFP storage unavailable"}
