"""The final document: the approved sections verbatim, briefing §2.3 fields, `done` atomically."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import document
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from tests.pipelines.rfp_intake.approval import support

GENERATED = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def approved_rows(approval: support.Approval) -> list[dict]:
    return [
        {
            "department_id": row.department_id,
            "approval_status": "approved",
            "approved_at": GENERATED,
            "approved_by": support.OWNERS[row.department_id],
            "owner": row.owner,
            "draft_content": row.draft_content,
            "review_rounds": 0,
        }
        for row in approval.sections().values()
    ]


def compose(approval: support.Approval, rows: list[dict] | None = None) -> document.FinalDocument:
    proposal = proposal_handoff.load_proposal_handoff(approval.engine, approval.ticket_id)
    assert proposal is not None
    return document.compose(
        proposal,
        rows if rows is not None else approved_rows(approval),
        thread_id="rfp-test",
        volume_cap=None,
        rulings=[],
        generated_at=GENERATED,
    )


def test_the_approved_sections_go_in_word_for_word_in_the_briefing_order(engine, tmp_path):
    approval = support.drafted(engine, tmp_path, "informal")
    final = compose(approval)

    assert [s.department_id for s in final.sections] == ["warehouse", "lastmile"]
    assert final.currency == "USD"
    assert final.content.startswith("# Pricing proposal for Luna Cosmetics")
    assert final.content.index("## 1. Warehouse Operations") < final.content.index(
        "## 2. Last Mile and Carrier Management"
    )
    for row in approval.sections().values():
        body_lines = [
            line
            for line in (row.draft_content or "").splitlines()[1:]
            if line and not line.startswith("For approval by") and not line.startswith("#")
        ]
        for line in body_lines:
            assert line in final.content
    assert "For approval by" not in final.content
    assert "Approved by Ana Whitfield on 23 November 2026." in final.content
    assert "#### Scope and approach" in final.content
    assert "| Warehouse Operations | Ana Whitfield | 23 November 2026 |" in final.content


def test_it_carries_the_briefing_fields_and_is_deterministic(engine, tmp_path):
    approval = support.drafted(engine, tmp_path, "formal")
    first, second = compose(approval), compose(approval)

    fields = first.model_dump(mode="json")
    assert {"ticket_id", "sections", "currency", "generated_at"} <= set(fields)
    assert str(first.ticket_id) == str(approval.ticket_id)
    assert first.content == second.content
    assert first.content_sha256 == second.content_sha256


def test_it_refuses_unless_every_active_section_is_approved(engine, tmp_path):
    approval = support.drafted(engine, tmp_path, "formal")
    rows = approved_rows(approval)
    rows[0]["approval_status"] = "pending"

    with pytest.raises(ValueError, match="not every section is approved: warehouse"):
        compose(approval, rows)


def test_done_and_the_document_are_written_together_or_not_at_all(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    broken = compose(approval).model_dump(mode="json")
    broken["currency"] = "GBP"  # violates ck_rfp_final_currency

    with pytest.raises(IntegrityError):
        approval_store.complete_approval(engine, approval.ticket_id, broken)

    assert approval.status() == "waiting_for_approval"
    assert approval_store.get_document(engine, approval.ticket_id) is None


def test_the_stored_document_is_what_the_graph_composed(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    assert approval.approve_all() == "done"

    stored = approval_store.get_document(engine, approval.ticket_id)
    assert stored is not None
    assert stored.currency == "EUR"
    assert [s["department_id"] for s in stored.sections] == ["warehouse", "reverse"]
    assert stored.approval_thread_id == f"rfp-{approval.ticket_id}"
    composed = approval.snapshot().values["document"]
    assert stored.content == composed["content"]
    ticket = store.get_ticket(engine, approval.ticket_id)
    assert ticket is not None
    assert (ticket.status, ticket.current_stage) == ("done", None)
    assert ticket.completed_at is not None
