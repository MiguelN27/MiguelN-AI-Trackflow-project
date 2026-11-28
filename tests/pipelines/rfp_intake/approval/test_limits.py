"""Iteration limits: per-section revisions and arbitration rounds always end the loop."""

import pytest

from data.pipelines.rfp_intake.approval import decisions
from data.pipelines.rfp_intake.approval import store as approval_store
from tests.pipelines.rfp_intake.approval import support
from tests.pipelines.rfp_intake.proposal.support import bad_returns, good


def test_request_changes_stops_at_the_revision_limit_but_approve_still_works(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal", max_revision_rounds=1)

    approval.decide("reverse", "request_changes", "Add the return window.")
    request = approval.requests()["reverse"]
    assert (request["review_round"], request["revisions_used"]) == (2, 1)
    assert request["allowed_actions"] == ["approve", "reject"]

    with pytest.raises(decisions.DecisionRefused) as refused:
        approval.submit("reverse", "request_changes", "And one more thing.")
    assert refused.value.code == "limit"
    assert "limit (1)" in refused.value.message

    assert approval.approve_all() == "done"


def test_a_revision_that_never_passes_goes_back_to_its_owner_for_review(engine, tmp_path):
    approval = support.opened(
        engine,
        tmp_path,
        "informal",
        drafts={"warehouse": [good("warehouse")], "lastmile": [good("lastmile"), bad_lastmile]},
        max_draft_iterations=2,
    )

    approval.decide("lastmile", "request_changes", "Name the carriers per state.")

    request = approval.requests()["lastmile"]
    assert request["review_round"] == 2
    assert request["needs_human_review"] is True
    section = approval.sections()["lastmile"]
    assert section.draft_status == "needs_human_review"
    assert section.draft_iterations == 2  # the revision's own loop hit its limit
    assert approval.status() == "waiting_for_approval"


def bad_lastmile(assignment: dict) -> object:
    draft = good("lastmile")(assignment)
    draft.discount_tiers = []  # fails volume-discount-table every time
    return draft


def test_a_conflict_unresolved_at_the_arbitration_limit_rejects_the_proposal(engine, tmp_path):
    approval = support.opened(
        engine,
        tmp_path,
        "formal",
        drafts={"warehouse": [good("warehouse")], "reverse": [bad_returns]},
        max_draft_iterations=1,
        max_arbitration_rounds=1,
    )
    # Round 1 sent reverse back; its revision still promises 24-hour returns.
    assert approval.requests()["reverse"]["review_round"] == 2

    assert approval.approve_all() == "rejected"

    sections = approval.sections()
    assert sections["reverse"].approval_status == "rejected"
    assert sections["warehouse"].approval_status == "approved"
    assert approval_store.get_document(engine, approval.ticket_id) is None
    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None and run.status == "rejected"
    assert [r["action"] for r in run.rulings] == ["request_changes", "reject"]
    rejection = [
        d for d in approval_store.decisions_for(engine, approval.ticket_id) if d.action == "reject"
    ]
    assert [(d.source, d.decided_by_name, d.trigger_id) for d in rejection] == [
        ("arbiter", "Sofía Ramos", "returns-sla-breach")
    ]
    assert "still unresolved after 1 arbitration round" in (rejection[0].comment or "")


def test_an_owner_rejection_ends_the_proposal_and_withdraws_the_other_requests(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "informal")

    assert approval.decide("warehouse", "reject", "We cannot onboard before the go-live.") == (
        "rejected"
    )

    sections = approval.sections()
    assert sections["warehouse"].approval_status == "rejected"
    assert sections["lastmile"].approval_status == "withdrawn"
    assert approval.requests() == {}
    withdrawn = [
        d for d in approval_store.decisions_for(engine, approval.ticket_id) if d.source == "system"
    ]
    assert [(d.department_id, d.action, d.status) for d in withdrawn] == [
        ("lastmile", "withdrawn", "applied")
    ]
    assert approval_store.get_document(engine, approval.ticket_id) is None
