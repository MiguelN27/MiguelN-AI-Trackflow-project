"""The hub every live-stream connection shares (`services/notifications/stream.py`).

What makes 2 or 50 open dashboards cost the same to the database: one watcher
per process, started by the first connection and stopped with the last, each
event validated and encoded once, a slow client disconnected without slowing
the others, and an upload's wake-up from any thread.
"""

import asyncio
import threading
from typing import Any

import pytest

from data.notifications import outbox
from services.notifications import stream as notifications
from services.notifications.stream import (
    NotificationHub,
    StreamUnavailable,
    Subscription,
)
from tests.pipelines.rfp_intake.support import new_ticket, sqlite_engine


@pytest.fixture
def engine(tmp_path):
    engine = sqlite_engine(tmp_path / "hub.sqlite")
    yield engine
    engine.dispose()


async def receive(subscription: Subscription, timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(subscription.queue.get(), timeout)


async def until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_one_event_is_encoded_once_and_reaches_every_connection(engine, tmp_path):
    async def scenario():
        hub = NotificationHub(poll_seconds=30, queue_size=10)
        subscriptions = [await hub.subscribe(engine) for _ in range(3)]
        ticket_id = new_ticket(engine, tmp_path)
        hub.wake()
        received = [await receive(subscription) for subscription in subscriptions]
        hub.close()
        return ticket_id, received

    ticket_id, received = asyncio.run(scenario())

    assert {item.subject for item in received} == {str(ticket_id)}
    assert {item.event for item in received} == {"rfp_ticket_created"}
    # The very same object: validated and framed once, not once per tab.
    assert received[0] is received[1] is received[2]
    assert received[0].frame.startswith(b"id: 1\nevent: rfp_ticket_created\ndata: {")


def test_the_watcher_reads_nothing_while_nobody_is_connected(engine, monkeypatch):
    reads: list[str] = []

    def counted(name: str):
        original = getattr(outbox, name)

        def read(*args: Any, **kwargs: Any) -> Any:
            reads.append(name)
            return original(*args, **kwargs)

        return read

    for name in ("latest_seq", "notifications_after"):
        monkeypatch.setattr(outbox, name, counted(name))

    async def scenario():
        hub = NotificationHub(poll_seconds=0.05, queue_size=10)
        hub.wake()  # no connection: a no-op
        await asyncio.sleep(0.2)
        assert reads == [] and not hub.watching
        subscription = await hub.subscribe(engine)
        assert hub.watching
        await asyncio.sleep(0.2)
        hub.unsubscribe(subscription)
        await until(lambda: not hub.watching)
        count = len(reads)
        await asyncio.sleep(0.2)
        return count

    count = asyncio.run(scenario())

    assert reads[0] == "latest_seq"
    assert len(reads) == count, "the watcher kept polling after the last connection left"


def test_a_slow_client_is_cut_off_after_its_backlog_without_slowing_the_others(engine, tmp_path):
    async def scenario():
        hub = NotificationHub(poll_seconds=30, queue_size=2)
        slow, fast = await hub.subscribe(engine), await hub.subscribe(engine)
        received = []
        for _ in range(3):
            new_ticket(engine, tmp_path)
            hub.wake()
            received.append(await receive(fast))
        backlog = [slow.queue.get_nowait() for _ in range(slow.queue.qsize())]
        hub.close()
        return slow, fast, received, backlog

    slow, fast, received, backlog = asyncio.run(scenario())

    assert [item.seq for item in received] == [1, 2, 3]
    assert slow.ended_by == "slow_consumer"
    # Its backlog, then the end of its stream; it resumes by Last-Event-ID from seq 2.
    assert [item.seq for item in backlog[:-1]] == [1, 2] and backlog[-1] is None


def test_an_upload_from_a_worker_thread_wakes_the_hub_at_once(engine, tmp_path):
    async def scenario():
        hub = NotificationHub(poll_seconds=30, queue_size=10)
        subscription = await hub.subscribe(engine)

        def register_and_wake():
            new_ticket(engine, tmp_path)
            hub.wake()

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.to_thread(register_and_wake)
        item = await receive(subscription, timeout=1.0)
        elapsed = loop.time() - started
        hub.close()
        return item, elapsed

    item, elapsed = asyncio.run(scenario())

    assert item.seq == 1
    assert elapsed < 1.0


def test_the_ring_replays_only_what_this_hub_saw(engine, tmp_path):
    async def scenario():
        new_ticket(engine, tmp_path)  # before the hub started: seq 1
        hub = NotificationHub(poll_seconds=30, queue_size=10)
        subscription = await hub.subscribe(engine)
        new_ticket(engine, tmp_path)  # seq 2, seen by the hub
        hub.wake()
        await receive(subscription)
        answers = {after: hub.replay_from_ring(after) for after in (0, 1, 2, 3)}
        hub.close()
        return answers

    answers = asyncio.run(scenario())

    assert answers[0] is None  # older than the ring: the database answers
    assert [item.seq for item in answers[1] or []] == [2]
    assert answers[2] == []
    assert answers[3] is None  # newer than anything seen: checked against the database


def test_a_row_the_contract_refuses_is_skipped_and_the_feed_moves_on(engine, tmp_path):
    rows = [
        {"seq": 1, "event": "rfp_ticket_exploded", "payload": {}, "created_at": None},
        {
            "seq": 2,
            "event": "rfp_ticket_created",
            "payload": {"ticket_id": "x"},
            "created_at": None,
        },
    ]

    assert [notifications.to_notification(row) for row in rows] == [None, None]

    new_ticket(engine, tmp_path)
    [valid] = outbox.notifications_after(engine, 0, limit=1)

    async def scenario():
        hub = NotificationHub(poll_seconds=30, queue_size=10)
        subscription = await hub.subscribe(engine)
        # The watcher's own step, fed two bad rows and then a good one.
        hub._publish([*rows, {**valid, "seq": 3}])  # noqa: SLF001
        item = await receive(subscription)
        cursor = hub.cursor
        hub.close()
        return cursor, item

    cursor, item = asyncio.run(scenario())

    assert item.seq == 3 and cursor == 3  # the bad rows did not stop the feed


def test_shutdown_ends_every_stream_and_refuses_new_ones(engine):
    async def scenario():
        hub = NotificationHub(poll_seconds=30, queue_size=10)
        subscriptions = [await hub.subscribe(engine) for _ in range(2)]
        hub.close()
        ends = [await receive(subscription) for subscription in subscriptions]
        with pytest.raises(StreamUnavailable):
            await hub.subscribe(engine)
        return ends, [subscription.ended_by for subscription in subscriptions]

    ends, reasons = asyncio.run(scenario())

    assert ends == [None, None]
    assert reasons == ["shutdown", "shutdown"]


def test_wake_without_a_hub_or_a_loop_is_harmless():
    notifications.reset_hub()
    notifications.wake()
    NotificationHub(poll_seconds=1, queue_size=1).wake()
    threading.Thread(target=notifications.wake).start()
