"""Parts 1 -> 3 as one process: upload, intake, drafting, per-department approval, document.

The reproducible end-to-end path of the ticket (`npm run rfp:e2e`): the real
graphs of the three parts, the real sample PDFs, the scripted agents, and
simulated owners whose decisions resume the checkpointed graph. It checks
that the ticket's statuses only move forward, that the client, currency and
departments survive every handoff unchanged, and that the final document
holds what the owners approved.
"""

from typing import Any

import pytest

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.approval import approvers
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.handoff import IntakeHandoff
from data.pipelines.rfp_intake.models import RfpHandoff
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from tests.pipelines.rfp_intake.approval import support
from tests.pipelines.rfp_intake.proposal.support import ScriptedProposalAgents, bad_returns, good
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket

FORWARD = [
    "analyzing",
    "intake_complete",
    "drafting",
    "under_evaluation",
    "waiting_for_approval",
    "done",
]


@pytest.fixture
def timeline(engine, monkeypatch):
    """The ticket's status at every event any part writes, deduplicated."""
    seen: list[str] = []
    original = store.record_event

    def recording(engine_, ticket_id, *args, **kwargs):
        original(engine_, ticket_id, *args, **kwargs)
        ticket = store.get_ticket(engine_, ticket_id)
        if ticket and (not seen or seen[-1] != ticket.status):
            seen.append(ticket.status)

    monkeypatch.setattr(store, "record_event", recording)
    return seen


def intake_payload(engine, ticket_id) -> IntakeHandoff:
    from sqlmodel import Session

    with Session(engine) as session:
        row = session.get(RfpHandoff, ticket_id)
        assert row is not None
        return IntakeHandoff.model_validate(row.payload)


@pytest.mark.parametrize(
    ("kind", "client", "currency", "departments"),
    [
        ("formal", "ModaViva, S.L.", "EUR", ["warehouse", "reverse"]),
        ("informal", "Luna Cosmetics", "USD", ["warehouse", "lastmile"]),
    ],
)
def test_an_rfp_travels_through_all_three_parts_as_one_process(
    engine, tmp_path, timeline, kind, client, currency, departments
):
    settings = support.settings()
    agents = ScriptedProposalAgents()
    # Part 1: upload and intake.
    ticket_id = new_ticket(engine, tmp_path / "pdf", kind)
    assert runner.run_intake(ticket_id, engine, agents=ScriptedAgents(kind).bundle()) == (
        "intake_complete"
    )
    # Part 2: drafting and self-evaluation.
    [(drafted, part_2)] = proposal_runner.run_pending(
        engine, agents=agents.bundle(), settings=settings
    )
    assert (drafted, part_2) == (ticket_id, "waiting_for_approval")
    # Part 3: every branch pauses for its owner...
    [(opened, part_3)] = approval_runner.start_pending(
        engine, agents=agents.bundle(), settings=settings
    )
    assert (opened, part_3) == (ticket_id, "waiting_for_approval")
    requests = {
        r["department_id"]: r for _, r in approval_runner.open_requests(engine, ticket_id, settings)
    }
    assert sorted(requests) == sorted(departments)
    assert {d: r["owner"] for d, r in requests.items()} == {
        d: approvers.owner_of(d) for d in departments
    }
    # ...and each owner signs off, one at a time (simulated resumes).
    for position, dept in enumerate(departments):
        approval_runner.submit_decision(
            engine,
            ticket_id,
            dept,
            action="approve",
            comment=None,
            review_round=1,
            decided_by_email=support.OWNERS[dept],
            settings=settings,
        )
        status = approval_runner.apply_decisions(
            ticket_id, engine, agents=agents.bundle(), settings=settings
        )
        assert status == ("done" if position == len(departments) - 1 else "waiting_for_approval")

    assert timeline == FORWARD

    # No data lost or changed between the parts.
    intake = intake_payload(engine, ticket_id)
    proposal = proposal_handoff.load_proposal_handoff(engine, ticket_id)
    final = approval_store.get_document(engine, ticket_id)
    assert proposal is not None and final is not None
    assert intake.client.name == proposal.client.name == final.client_name == client
    assert intake.client.currency == proposal.currency == final.currency == currency
    assert [d.department_id for d in intake.departments] == departments
    assert [s.department_id for s in proposal.sections] == departments
    assert [s["department_id"] for s in final.sections] == departments
    sections = {row.department_id: row for row in approval_store.load_sections(engine, ticket_id)}
    for dept in departments:
        row = sections[dept]
        assert (row.approval_status, row.approver) == ("approved", approvers.owner_of(dept))
        summary = (row.draft_data or {})["draft"]["summary"]
        assert summary in final.content
    detail: dict[str, Any] | None = store.get_ticket_detail(engine, ticket_id)
    assert detail is not None and detail["status"] == "done"
    assert detail["error_stage"] is None and detail["current_stage"] is None


def test_a_needs_human_review_ticket_also_reaches_done(engine, tmp_path, timeline):
    approval = support.drafted(
        engine,
        tmp_path,
        "formal",
        drafts={"warehouse": [good("warehouse")], "reverse": [bad_returns, good("reverse")]},
        max_draft_iterations=1,
    )
    assert approval.status() == "needs_human_review"
    approval.start()
    assert approval.approve_all() == "done"

    assert timeline == [
        "analyzing",
        "intake_complete",
        "drafting",
        "under_evaluation",
        "needs_human_review",
        "waiting_for_approval",
        "done",
    ]


def test_the_invalid_document_never_reaches_part_3(engine, tmp_path):
    ticket_id = new_ticket(engine, tmp_path / "pdf", "invalid")
    assert runner.run_intake(ticket_id, engine, agents=ScriptedAgents("invalid").bundle()) == (
        "discarded"
    )
    agents = ScriptedProposalAgents()

    assert proposal_runner.run_pending(engine, agents=agents.bundle()) == []
    assert approval_runner.start_pending(engine, agents=agents.bundle()) == []
    assert approval_store.get_run(engine, ticket_id) is None
