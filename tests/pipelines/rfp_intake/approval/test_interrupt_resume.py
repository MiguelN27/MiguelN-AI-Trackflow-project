"""The human-in-the-loop pause: one interrupt per department, a checkpoint first, resume."""

import pytest
from langgraph.types import Command

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import checkpoint, decisions
from data.pipelines.rfp_intake.approval import graph as approval_graph
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from tests.pipelines.rfp_intake.approval import support
from tests.pipelines.rfp_intake.proposal.support import bad_returns, good


def test_the_test_owner_accounts_are_the_configured_ones():
    assert support.settings().approvers == support.OWNERS


def test_opening_pauses_every_department_for_its_owner_after_a_checkpoint(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")

    requests = approval.requests()
    assert set(requests) == {"warehouse", "reverse"}
    assert requests["warehouse"]["owner"] == "Ana Whitfield"
    assert requests["reverse"]["owner"] == "Sofía Ramos"
    for request in requests.values():
        assert request["review_round"] == 1
        assert request["allowed_actions"] == ["approve", "request_changes", "reject"]
        assert request["approver_configured"] is True

    assert approval.status() == "waiting_for_approval"
    ticket = store.get_ticket(engine, approval.ticket_id)
    assert ticket is not None
    assert ticket.current_stage == "awaiting_approval:warehouse,reverse"
    assert {s.approval_status for s in approval.sections().values()} == {"pending"}

    # The state was persisted before the pause: a checkpoint for the ticket's thread exists,
    # and the request was written (open_review) before the interrupt.
    saver = checkpoint.get_checkpointer(engine)
    assert saver.get_tuple(approval.config()) is not None
    assert {"open_review:warehouse#1", "open_review:reverse#1"} <= set(approval.events())
    assert approval.snapshot().next == ("review_department", "review_department")
    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None
    assert (run.status, run.thread_id) == ("paused", f"rfp-{approval.ticket_id}")


def test_resume_is_an_entry_point_not_a_restart(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    before = {
        "open": approval.events("open_approval"),
        "part_1": approval.events("classify_document"),
        "part_2": approval.events("plan_assignments"),
        "open_review": approval.events("open_review"),
        "generator_calls": sorted(approval.agents.calls),
    }

    assert approval.decide("reverse", "approve") == "waiting_for_approval"

    assert approval.events("open_approval") == before["open"] == ["open_approval"]
    assert approval.events("classify_document") == before["part_1"]
    assert approval.events("plan_assignments") == before["part_2"]
    # Neither branch was opened again: the paused one kept its request.
    assert approval.events("open_review") == before["open_review"]
    assert sorted(approval.agents.calls) == before["generator_calls"]


def test_a_paused_run_resumes_after_a_restart(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    approval.decide("warehouse", "approve")

    # A new process: no compiled graph, no open checkpointer connection.
    approval_graph.clear_graphs()
    checkpoint.close_checkpointers()

    assert approval.decide("reverse", "approve") == "done"
    assert approval.events("open_approval") == ["open_approval"]
    assert approval_store.get_document(engine, approval.ticket_id) is not None


def test_a_needs_human_review_ticket_is_accepted_and_flagged(engine, tmp_path):
    approval = support.drafted(
        engine,
        tmp_path,
        "formal",
        drafts={"warehouse": [good("warehouse")], "reverse": [bad_returns, good("reverse")]},
        max_draft_iterations=1,
    )
    assert approval.status() == "needs_human_review"

    approval.start()

    assert approval.status() == "waiting_for_approval"
    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None and run.status == "paused"


@pytest.mark.parametrize(
    ("dept", "action", "comment", "by", "code"),
    [
        ("reverse", "approve", None, support.OWNERS["warehouse"], "not_owner"),
        ("reverse", "approve", None, "admin@trackflow.com", "not_owner"),
        ("reverse", "reject", None, None, "comment_required"),
        ("reverse", "request_changes", "   ", None, "comment_required"),
        ("reverse", "maybe", None, None, "invalid"),
        ("lastmile", "approve", None, None, "not_found"),
    ],
)
def test_a_decision_is_validated_before_it_is_stored(
    engine, tmp_path, dept, action, comment, by, code
):
    approval = support.opened(engine, tmp_path, "formal")

    with pytest.raises(decisions.DecisionRefused) as refused:
        approval.submit(dept, action, comment, by=by or support.OWNERS.get(dept))

    assert refused.value.code == code
    assert approval_store.recorded_decisions(engine, approval.ticket_id) == []
    assert approval.sections()["reverse"].approval_status == "pending"


def test_a_stale_or_repeated_decision_is_refused(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")

    with pytest.raises(decisions.DecisionRefused) as stale:
        approval.submit("reverse", "approve", review_round=2)
    assert stale.value.code == "stale"

    approval.submit("reverse", "approve")
    with pytest.raises(decisions.DecisionRefused) as twice:
        approval.submit("reverse", "approve")
    assert twice.value.code == "already_decided"

    approval.apply()
    with pytest.raises(decisions.DecisionRefused) as decided:
        approval.submit("reverse", "reject", "Changed my mind")
    assert decided.value.code == "not_waiting"


def test_a_raw_resume_is_validated_inside_the_graph_and_asked_again(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    graph = approval_runner.graph_for(engine, approval.settings)
    context = approval_graph.ApprovalContext(
        engine=engine, agents=approval.agents.bundle(), settings=approval.settings
    )
    request = approval.requests()["reverse"]

    # Malformed, then well formed but not from the owner: both refused by validate_decision.
    graph.invoke(
        Command(resume={approval.interrupt_ids()["reverse"]: {"action": "approve"}}),
        approval.config(),
        context=context,
    )
    assert "malformed" in approval.requests()["reverse"]["error"]

    impostor = {
        "decision_id": "00000000-0000-4000-8000-000000000001",
        "department_id": "reverse",
        "review_round": 1,
        "draft_sha256": request["draft_sha256"],
        "action": "approve",
        "comment": None,
        "decided_by": {"source": "owner", "email": support.OWNERS["warehouse"], "name": "Ana"},
        "decided_at": "2026-11-23T10:00:00+00:00",
    }
    graph.invoke(
        Command(resume={approval.interrupt_ids()["reverse"]: impostor}),
        approval.config(),
        context=context,
    )
    assert "Only Sofía Ramos" in approval.requests()["reverse"]["error"]
    assert approval.sections()["reverse"].approval_status == "pending"
    assert [e for e in approval.trace() if e["node"].startswith("validate_decision:reverse")][-1][
        "output"
    ]["valid"] is False

    # The owner's own decision then goes through.
    assert approval.decide("reverse", "approve") == "waiting_for_approval"
    assert approval.sections()["reverse"].approval_status == "approved"


def test_an_approval_cannot_start_twice(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    proposal = proposal_handoff.load_proposal_handoff(engine, approval.ticket_id)
    assert proposal is not None

    with pytest.raises(store.TicketStateError):
        approval_runner.start(proposal, engine, agents=approval.agents.bundle())
    assert approval_runner.start_pending(engine, agents=approval.agents.bundle()) == []
