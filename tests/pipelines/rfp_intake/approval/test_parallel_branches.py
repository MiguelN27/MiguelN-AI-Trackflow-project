"""Parallel branches, not serial fake-parallelism: one department paused never blocks another."""

from data.pipelines.rfp_intake.approval import checkpoint
from data.pipelines.rfp_intake.approval import store as approval_store
from tests.pipelines.rfp_intake.approval import support


def test_department_b_is_approved_while_department_a_remains_interrupted(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")  # A = warehouse, B = reverse
    warehouse_interrupt = approval.interrupt_ids()["warehouse"]

    assert approval.decide("reverse", "approve") == "waiting_for_approval"

    sections = approval.sections()
    reverse, warehouse = sections["reverse"], sections["warehouse"]
    assert reverse.approval_status == "approved"
    assert reverse.approver == "Sofía Ramos"
    assert reverse.approved_by == support.OWNERS["reverse"]
    assert reverse.approved_at is not None
    # A is still paused, on the very same interrupt, and was never asked again.
    assert warehouse.approval_status == "pending"
    assert approval.interrupt_ids() == {"warehouse": warehouse_interrupt}
    assert approval.events("open_review:warehouse") == ["open_review:warehouse#1"]
    # B's branch finished: its sign-off is in the trace while A has no decision yet.
    nodes = [entry["node"] for entry in approval.trace()]
    assert "sign_off:reverse#1" in nodes
    assert not any(node.startswith("await_decision:warehouse") for node in nodes)
    assert approval_store.get_document(engine, approval.ticket_id) is None

    assert approval.decide("warehouse", "approve") == "done"


def test_b_is_revised_and_comes_back_while_a_waits(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    warehouse_interrupt = approval.interrupt_ids()["warehouse"]

    approval.decide("reverse", "request_changes", "Say who pays international return shipping.")

    requests = approval.requests()
    assert requests["reverse"]["review_round"] == 2
    assert "asked for changes" in requests["reverse"]["reason"]
    assert approval.interrupt_ids()["warehouse"] == warehouse_interrupt
    assert requests["warehouse"]["review_round"] == 1
    # The revision ran Part 2's loop for reverse only, with the owner's request.
    revision = approval.agents.calls["generate:reverse#2"][0]
    request = revision["assignment"]["revision_request"]
    assert request["comment"] == "Say who pays international return shipping."
    assert request["requested_by"] == "Sofía Ramos (owner of Reverse Logistics)"
    assert "generate:warehouse#2" not in approval.agents.calls
    assert approval.sections()["reverse"].review_rounds == 1

    assert approval.approve_all() == "done"


def test_several_departments_resume_in_one_call(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "informal")

    approval.submit("warehouse", "approve")
    approval.submit("lastmile", "approve")

    assert approval.apply() == "done"
    assert approval.requests() == {}


def test_concurrent_tickets_never_share_a_checkpoint(engine, tmp_path):
    first = support.opened(engine, tmp_path / "first", "formal")
    second = support.opened(engine, tmp_path / "second", "informal")
    assert checkpoint.thread_id(first.ticket_id) != checkpoint.thread_id(second.ticket_id)

    assert first.approve_all() == "done"

    assert second.status() == "waiting_for_approval"
    assert set(second.requests()) == {"warehouse", "lastmile"}
    saver = checkpoint.get_checkpointer(engine)
    threads = {item.config["configurable"]["thread_id"] for item in saver.list(None)}
    assert {f"rfp-{first.ticket_id}", f"rfp-{second.ticket_id}"} <= threads
    assert all(
        item.config["configurable"]["thread_id"] == f"rfp-{second.ticket_id}"
        for item in saver.list(second.config())
    )
