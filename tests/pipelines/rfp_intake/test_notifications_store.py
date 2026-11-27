"""The live stream's feed in the store: `rfp_ticket_notifications` (the outbox).

A ticket and its `rfp_ticket_created` announcement are written in one
transaction, so whatever process registers a ticket (the API, the CLI) the
stream announces exactly the tickets that exist, once each.
"""

import pytest
from sqlalchemy import func, select
from sqlmodel import Session

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.models import RfpTicket, RfpTicketNotification
from tests.pipelines.rfp_intake.support import new_ticket, sqlite_engine


@pytest.fixture
def engine(tmp_path):
    engine = sqlite_engine(tmp_path / "notifications.sqlite")
    yield engine
    engine.dispose()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_registering_a_ticket_announces_it_once_with_the_list_shape(engine, tmp_path):
    ticket_id = new_ticket(engine, tmp_path)
    ticket = store.get_ticket(engine, ticket_id)
    assert ticket is not None

    [row] = store.notifications_after(engine, 0, limit=10)

    assert row["seq"] == 1
    assert row["event"] == store.TICKET_CREATED_EVENT == "rfp_ticket_created"
    assert row["ticket_id"] == str(ticket_id)
    assert row["payload"] == store.new_ticket_summary(ticket)
    assert row["payload"] == {
        "ticket_id": str(ticket_id),
        "rfp_id": str(ticket.rfp_id),
        "status": "analyzing",
        "current_stage": "queued",
        "original_filename": "trackflow-request-1.pdf",
        "client_name": None,
        "client_country": None,
        "departments_needed": [],
        "created_at": store.as_utc(ticket.created_at).isoformat(),  # type: ignore[union-attr]
        "updated_at": store.as_utc(ticket.updated_at).isoformat(),  # type: ignore[union-attr]
    }
    # The same fields the list route serves for that ticket.
    assert set(row["payload"]) == set(store.list_tickets(engine)[0])


def test_the_ticket_and_its_announcement_commit_together_or_not_at_all(
    engine, tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "new_ticket_summary", lambda ticket: {"not json": object()})

    with pytest.raises(Exception):  # noqa: B017 - whatever the driver raises
        new_ticket(engine, tmp_path)

    assert count(engine, RfpTicket) == 0
    assert count(engine, RfpTicketNotification) == 0


def test_the_feed_is_read_in_seq_order_after_a_cursor(engine, tmp_path):
    assert store.latest_notification_seq(engine) == 0
    tickets = [str(new_ticket(engine, tmp_path)) for _ in range(4)]

    assert store.latest_notification_seq(engine) == 4
    assert [row["seq"] for row in store.notifications_after(engine, 0, limit=10)] == [1, 2, 3, 4]
    assert [row["ticket_id"] for row in store.notifications_after(engine, 1, limit=2)] == tickets[
        1:3
    ]
    assert store.notifications_after(engine, 4, limit=10) == []
    assert all(
        row["created_at"].tzinfo is not None
        for row in store.notifications_after(engine, 0, limit=4)
    )


def test_reprocessing_does_not_announce_a_ticket_again(engine, tmp_path):
    ticket_id = new_ticket(engine, tmp_path)
    store.fail(engine, ticket_id, stage="convert_to_markdown", message="test")

    store.reset_ticket(engine, ticket_id)

    assert count(engine, RfpTicketNotification) == 1
