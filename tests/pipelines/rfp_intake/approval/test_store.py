"""Part 3 persistence: the lease, decisions applied once, and recovery after a crash."""

from datetime import timedelta

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.models import utcnow
from tests.pipelines.rfp_intake.approval import support


def test_one_worker_at_a_time_holds_a_ticket_and_a_dead_ones_lease_expires(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    ttl = timedelta(minutes=30)

    assert approval_store.acquire_lease(engine, approval.ticket_id, "worker-a", ttl=ttl)
    assert not approval_store.acquire_lease(engine, approval.ticket_id, "worker-b", ttl=ttl)
    later = utcnow() + timedelta(minutes=31)
    assert approval_store.acquire_lease(engine, approval.ticket_id, "worker-b", ttl=ttl, now=later)


def test_a_decision_waits_while_another_worker_holds_the_ticket_then_is_applied(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    assert approval_store.acquire_lease(
        engine, approval.ticket_id, "someone-else", ttl=timedelta(minutes=30)
    )

    approval.submit("reverse", "approve")
    assert approval.apply() == "waiting_for_approval"
    assert approval.sections()["reverse"].approval_status == "pending"  # not applied yet
    [recorded] = approval_store.recorded_decisions(engine, approval.ticket_id)

    approval_store.release_lease(engine, approval.ticket_id, "someone-else")
    approval.apply()
    assert approval.sections()["reverse"].approval_status == "approved"
    applied = approval_store.get_decision(engine, recorded.id)
    assert applied is not None and applied.status == "applied"


def test_recover_applies_decisions_a_dead_process_left_recorded(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    approval.submit("warehouse", "approve")
    approval.submit("reverse", "approve")  # recorded; the process "died" before applying

    touched = approval_runner.recover(
        engine, agents=approval.agents.bundle(), settings=approval.settings
    )

    assert touched == [approval.ticket_id]
    assert approval.status() == "done"


def test_a_failure_keeps_the_decisions_and_resume_continues_from_the_checkpoint(
    engine, tmp_path, monkeypatch
):
    approval = support.opened(engine, tmp_path, "formal")
    approval.submit("warehouse", "approve")
    approval.submit("reverse", "approve")

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    with monkeypatch.context() as patch:
        patch.setattr(approval_store, "complete_approval", broken)
        assert approval.apply() == "waiting_for_approval"

    ticket = store.get_ticket(engine, approval.ticket_id)
    assert ticket is not None
    assert (ticket.error_stage, ticket.error_message) == (
        "complete_approval",
        "unexpected RuntimeError",
    )
    run = approval_store.get_run(engine, approval.ticket_id)
    assert run is not None and run.status == "failed"
    assert {s.approval_status for s in approval.sections().values()} == {"approved"}

    assert (
        approval_runner.resume(
            approval.ticket_id, engine, agents=approval.agents.bundle(), settings=approval.settings
        )
        == "done"
    )
    # Neither branch was opened again (the two branches open in parallel, in either order).
    assert sorted(approval.events("open_review")) == [
        "open_review:reverse#1",
        "open_review:warehouse#1",
    ]
    ticket = store.get_ticket(engine, approval.ticket_id)
    assert ticket is not None and ticket.error_stage is None
