"""The backoffice's live notification stream: one hub per API process, one SSE response per tab.

`GET /notifications/stream` (and its alias `/rfp/notifications/stream`) pushes
named events the moment they happen, so nobody has to reload a page:
- `rfp_ticket_created`: a new RFP ticket was registered;
- `operational_inactivity_detected` / `operational_activity_resumed` /
  `operational_alert_acknowledged`: an operational inactivity alert opened,
  resolved, or was taken in charge.
It is a communication layer only: nothing here calls a model or an agent.

The feed is the `live_notifications` outbox (`data/notifications/`): every
writer appends in the same transaction as its change, under an advisory lock on
PostgreSQL, so `seq` order is commit order across every event type. `seq` is
the SSE `id:`, so one cursor resumes everything. Each event's `data` is
validated against its model in `EVENT_MODELS`; an event with no model is
skipped (logged), never sent unvalidated.

Every connection is independent at the edge (its own response, JWT check,
cursor and bounded queue), and they all share one `NotificationHub` behind it:
- **one watcher task** reads the feed while at least one tab is connected: at
  once when a write through this API wakes it (`wake`), otherwise every
  `LIVE_STREAM_POLL_SECONDS`, which catches events written by another process
  (the CLI, another worker). Nobody connected: no query at all;
- each event is validated and encoded **once**, then put on every queue;
- a connection never queries the database by itself while live, never holds a
  database connection, and never holds a worker thread (the stream is async);
- the hub keeps the last `RING_SIZE` events, so a reconnect usually replays
  from memory.

Recovery (`Last-Event-ID`): the stream subscribes first, then replays what the
client missed (ring, else database), then sends `rfp_stream_ready` with the new
cursor and goes live, skipping anything at or below that cursor: no gap, no
double send. A cursor the replay cannot honour (unknown, older than
`LIVE_STREAM_REPLAY_HOURS`, more than `LIVE_STREAM_REPLAY_LIMIT` events behind,
or malformed) gets `rfp_stream_resync`: the client reloads its snapshots once.
(The two control events kept their first names, so clients built for the RFP
stream keep working.)

A stream ends normally at its token's expiry or after `LIVE_STREAM_MAX_SECONDS`;
the client resumes it at once with `Last-Event-ID`. A client that falls more than
`LIVE_STREAM_QUEUE_SIZE` events behind is disconnected after its backlog, and
resumes the same way. See `docs/rfp/live-notifications.md` and
`docs/operations/inactivity-alerts.md`.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import anyio
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sse_starlette import ServerSentEvent

from data.notifications import outbox
from data.pipelines.rfp_intake.store import TICKET_CREATED_EVENT
from services.notifications.config import LiveStreamSettings, get_live_stream_settings
from services.operations import schemas as operations
from services.rfp.models import RfpTicketSummary

logger = logging.getLogger("api.notifications.stream")

TICKET_CREATED: Final = TICKET_CREATED_EVENT
# Every event the stream sends, and the model its `data` must satisfy.
EVENT_MODELS: Final[dict[str, type[BaseModel]]] = {
    TICKET_CREATED: RfpTicketSummary,
    operations.INACTIVITY_DETECTED: operations.OperationalAlert,
    operations.ACTIVITY_RESUMED: operations.OperationalAlert,
    operations.ALERT_ACKNOWLEDGED: operations.OperationalAlert,
}
STREAM_READY: Final = "rfp_stream_ready"
STREAM_RESYNC: Final = "rfp_stream_resync"
KEEP_ALIVE: Final = "keep-alive"
# One line ending everywhere on the wire (the SSE spec accepts LF, CRLF or CR).
SEP: Final = "\n"
RING_SIZE: Final = 256
# Rows one watcher read takes at most; a full batch is followed by another read at once.
WATCH_BATCH: Final = 100
# How long a new connection waits for a cold hub to learn the feed's cursor.
READY_TIMEOUT_SECONDS: Final = 10.0
# A client that stops reading for this long is cut off (sse-starlette's send timeout).
SEND_TIMEOUT_SECONDS: Final = 30.0


class StreamUnavailable(RuntimeError):
    """The feed could not be read to open a stream. The API answers 503."""


@dataclass(frozen=True, slots=True)
class Notification:
    """One feed row, validated and encoded once for every connection."""

    seq: int
    event: str
    # What the event is about: a ticket id, an alert id (the outbox's `subject`).
    subject: str
    created_at: datetime
    frame: bytes


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _frame(event: str, seq: int, data: str) -> bytes:
    return ServerSentEvent(data=data, event=event, id=str(seq), sep=SEP).encode()


def keep_alive() -> ServerSentEvent:
    """The heartbeat: a comment frame (`: keep-alive`), which no client dispatches as an event."""
    return ServerSentEvent(comment=KEEP_ALIVE, sep=SEP)


def to_notification(row: dict[str, Any]) -> Notification | None:
    """A feed row as a ready-to-send frame; None (logged) for a row the contract refuses.

    The payload is validated by its event's model (`EVENT_MODELS`): a new ticket
    by the list's own `RfpTicketSummary`, so the event carries exactly what
    `GET /rfp/tickets` would show; an alert by `OperationalAlert`, the shape
    `GET /operations/alerts` returns.
    """
    seq = int(row["seq"])
    event = str(row["event"])
    model = EVENT_MODELS.get(event)
    if model is None:
        logger.warning("live stream: skipped seq %d, unknown event %r", seq, event)
        return None
    try:
        payload = model.model_validate(row["payload"])
    except ValidationError as error:
        logger.error(
            "live stream: skipped seq %d (%s), payload refused (%d errors)",
            seq,
            event,
            error.error_count(),
        )
        return None
    return Notification(
        seq=seq,
        event=event,
        subject=str(row["subject"]),
        created_at=row["created_at"],
        frame=_frame(event, seq, payload.model_dump_json()),
    )


class Subscription:
    """One connection's mailbox. `None` in the queue means: end this stream."""

    def __init__(self, limit: int) -> None:
        self.queue: asyncio.Queue[Notification | None] = asyncio.Queue()
        self.limit = limit
        self.ended_by: str | None = None

    def offer(self, notification: Notification) -> None:
        if self.ended_by is not None:
            return
        if self.queue.qsize() >= self.limit:
            # Slow client: it gets its backlog, then the stream ends and it
            # resumes from its last event by replay. Nothing is lost.
            self.end("slow_consumer")
            return
        self.queue.put_nowait(notification)

    def end(self, reason: str) -> None:
        if self.ended_by is None:
            self.ended_by = reason
            self.queue.put_nowait(None)


class NotificationHub:
    """The intermediate layer every connection of this process shares."""

    def __init__(self, *, poll_seconds: float, queue_size: int, ring_size: int = RING_SIZE) -> None:
        self.poll_seconds = poll_seconds
        self.queue_size = queue_size
        self._subscribers: set[Subscription] = set()
        self._ring: deque[Notification] = deque(maxlen=ring_size)
        # The ring holds every published event with floor < seq <= cursor.
        self._floor = 0
        self._cursor = 0
        self._engine: Engine | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._watcher: asyncio.Task[None] | None = None
        self._closed = False

    # --- What the stream reads ---

    @property
    def cursor(self) -> int:
        """The newest seq this hub has published."""
        return self._cursor

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def watching(self) -> bool:
        return self._watcher is not None and not self._watcher.done()

    def replay_from_ring(self, after: int) -> list[Notification] | None:
        """The published events after `after`, or None when the ring cannot tell."""
        if not self._ready.is_set() or not self._floor <= after <= self._cursor:
            return None
        return [notification for notification in self._ring if notification.seq > after]

    # --- Connections ---

    async def subscribe(self, engine: Engine) -> Subscription:
        """Register a connection; a cold hub starts its watcher and learns the cursor first.

        Registration comes before anything is read, so whatever the watcher
        publishes from now on reaches this connection.
        """
        if self._closed:
            raise StreamUnavailable("the API is shutting down")
        self._bind(asyncio.get_running_loop())
        subscription = Subscription(self.queue_size)
        self._subscribers.add(subscription)
        if not self.watching:
            self._engine = engine
            self._ready = asyncio.Event()
            self._watcher = asyncio.create_task(self._watch(engine), name="live-stream-watcher")
        try:
            with anyio.fail_after(READY_TIMEOUT_SECONDS):
                await self._ready.wait()
        except TimeoutError:
            self.unsubscribe(subscription)
            raise StreamUnavailable("the notification feed did not answer in time") from None
        except BaseException:
            self.unsubscribe(subscription)
            raise
        if subscription.ended_by == "unavailable":
            self.unsubscribe(subscription)
            raise StreamUnavailable("the notification feed could not be read")
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)

    def wake(self) -> None:
        """Read the feed now (a ticket was just committed). Any thread; a no-op when idle."""
        loop = self._loop
        if loop is None or loop.is_closed() or not self.watching:
            return
        with contextlib.suppress(RuntimeError):  # the loop closed in between
            loop.call_soon_threadsafe(self._wake.set)

    def close(self) -> None:
        """Shutdown: end every stream and stop the watcher (from the hub's own loop)."""
        self._closed = True
        for subscription in list(self._subscribers):
            subscription.end("shutdown")
        self._subscribers.clear()
        if self._watcher is not None and not self._watcher.done():
            self._watcher.cancel()

    # --- The watcher ---

    def _bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Forget state that belongs to another event loop (each test client has its own)."""
        if self._loop is loop:
            return
        self._loop = loop
        self._subscribers.clear()
        self._ring.clear()
        self._watcher = None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()

    def _publish(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            notification = to_notification(row)
            self._cursor = int(row["seq"])
            if notification is None:
                continue
            if self._ring.maxlen is not None and len(self._ring) == self._ring.maxlen:
                self._floor = self._ring[0].seq
            self._ring.append(notification)
            for subscription in list(self._subscribers):
                subscription.offer(notification)

    async def _watch(self, engine: Engine) -> None:
        failures = 0
        try:
            while True:
                full_batch = False
                try:
                    if not self._ready.is_set():
                        latest = await anyio.to_thread.run_sync(outbox.latest_seq, engine)
                        self._cursor = self._floor = latest
                        self._ring.clear()
                        self._ready.set()
                    else:
                        rows = await anyio.to_thread.run_sync(
                            lambda: outbox.notifications_after(
                                engine, self._cursor, limit=WATCH_BATCH
                            )
                        )
                        self._publish(rows)
                        full_batch = len(rows) == WATCH_BATCH
                    failures = 0
                except SQLAlchemyError as error:
                    failures += 1
                    if failures == 1 or failures % 30 == 0:
                        logger.error(
                            "live stream feed read failed (%d in a row): %s",
                            failures,
                            type(error).__name__,
                        )
                    if not self._ready.is_set():
                        # Nobody can be told where the feed stands: refuse the waiting connections.
                        for subscription in list(self._subscribers):
                            subscription.end("unavailable")
                        self._subscribers.clear()
                        self._ready.set()
                        return
                if not self._subscribers:
                    return
                if full_batch:
                    continue
                try:
                    with anyio.move_on_after(self.poll_seconds):
                        await self._wake.wait()
                finally:
                    self._wake.clear()
        finally:
            # A later subscriber starts a fresh watcher, which learns the cursor again.
            if self._watcher is asyncio.current_task():
                self._watcher = None
            self._ready = asyncio.Event()


_hub: NotificationHub | None = None


def get_hub(settings: LiveStreamSettings | None = None) -> NotificationHub:
    global _hub
    if _hub is None:
        settings = settings or get_live_stream_settings()
        _hub = NotificationHub(poll_seconds=settings.poll_seconds, queue_size=settings.queue_size)
    return _hub


def wake() -> None:
    """Read the outbox now: something was just committed (an upload's 202, an alert change)."""
    if _hub is not None:
        _hub.wake()


def close_hub() -> None:
    """Lifespan shutdown: end the streams. A later request (tests) gets a new hub."""
    global _hub
    if _hub is not None:
        _hub.close()
        _hub = None


def reset_hub() -> None:
    """Tests: forget the hub without touching its (possibly closed) event loop."""
    global _hub
    _hub = None


# --- One connection ------------------------------------------------------------------


def _resync(reason: str, cursor: int) -> bytes:
    return _frame(STREAM_RESYNC, cursor, _json({"reason": reason, "cursor": cursor}))


def _parse_cursor(last_event_id: str | None) -> int | None | str:
    """None for a first connection, the seq for a resume, "invalid" for anything else."""
    if last_event_id is None or not last_event_id.strip():
        return None
    value = last_event_id.strip()
    if not value.isdigit():
        return "invalid"
    return int(value)


class Stream:
    """One open connection: what it missed, then whatever the hub publishes."""

    def __init__(
        self,
        hub: NotificationHub,
        subscription: Subscription,
        *,
        preamble: list[bytes],
        cursor: int,
        replayed: int,
        deadline: float,
    ) -> None:
        self.hub = hub
        self.subscription = subscription
        self.preamble = preamble
        self.cursor = cursor
        self.replayed = replayed
        self.deadline = deadline

    def close(self) -> None:
        self.hub.unsubscribe(self.subscription)

    async def frames(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        # Unless the stream ends itself below, the client went away (or the server stopped).
        reason = "disconnected"
        try:
            for frame in self.preamble:
                yield frame
            while True:
                remaining = self.deadline - loop.time()
                if remaining <= 0:
                    reason = "lifetime"
                    break
                # Wait inside the scope, yield outside it: the deadline must never
                # cancel the send that follows a yield.
                item: Notification | None = None
                timed_out = True
                with anyio.move_on_after(remaining):
                    item = await self.subscription.queue.get()
                    timed_out = False
                if timed_out:
                    reason = "lifetime"
                    break
                if item is None:
                    reason = self.subscription.ended_by or "ended"
                    break
                if item.seq <= self.cursor:
                    continue
                self.cursor = item.seq
                yield item.frame
        finally:
            self.close()
            logger.info(
                "live stream closed: %s (cursor %d, %d still connected)",
                reason,
                self.cursor,
                self.hub.subscriber_count,
            )


async def open_stream(
    engine: Engine,
    *,
    last_event_id: str | None,
    token_expires_at: float,
    settings: LiveStreamSettings,
    hub: NotificationHub | None = None,
) -> Stream:
    """Subscribe, work out what the client missed, and return the stream to send.

    Raises `StreamUnavailable` when the feed cannot be read (the API answers 503
    before any event byte, and the client retries with backoff).
    """
    hub = hub or get_hub(settings)
    subscription = await hub.subscribe(engine)
    try:
        preamble, cursor, replayed = await _catch_up(
            hub, engine, subscription, last_event_id, settings
        )
    except SQLAlchemyError as error:
        hub.unsubscribe(subscription)
        logger.error("live stream replay failed: %s", type(error).__name__)
        raise StreamUnavailable("the notification feed could not be read") from None
    except BaseException:
        hub.unsubscribe(subscription)
        raise
    preamble.append(
        _frame(
            STREAM_READY,
            cursor,
            _json(
                {
                    "cursor": cursor,
                    "replayed": replayed,
                    "heartbeat_seconds": settings.heartbeat_seconds,
                }
            ),
        )
    )
    loop = asyncio.get_running_loop()
    lifetime = min(settings.max_seconds, max(0.0, token_expires_at - time.time()))
    logger.info(
        "live stream opened (cursor %d, replayed %d, %d connected)",
        cursor,
        replayed,
        hub.subscriber_count,
    )
    return Stream(
        hub,
        subscription,
        preamble=preamble,
        cursor=cursor,
        replayed=replayed,
        deadline=loop.time() + lifetime,
    )


async def _catch_up(
    hub: NotificationHub,
    engine: Engine,
    subscription: Subscription,
    last_event_id: str | None,
    settings: LiveStreamSettings,
) -> tuple[list[bytes], int, int]:
    """(frames to send first, the cursor after them, how many events were replayed)."""
    after = _parse_cursor(last_event_id)
    if after is None:
        # A first connection: from now on. The client takes its snapshots (the
        # ticket list, the open alerts) after `rfp_stream_ready`, so nothing
        # falls between the two.
        return [], hub.cursor, 0
    if isinstance(after, str):
        return [_resync("cursor_invalid", hub.cursor)], hub.cursor, 0

    missed = hub.replay_from_ring(after)
    if missed is None:
        rows = await anyio.to_thread.run_sync(
            lambda: outbox.notifications_after(engine, after, limit=settings.replay_limit + 1)
        )
        if not rows and after > hub.cursor:
            latest = await anyio.to_thread.run_sync(outbox.latest_seq, engine)
            if after > latest:
                # A cursor from the future: the database was reset, or another one.
                return [_resync("cursor_unknown", hub.cursor)], hub.cursor, 0
        if len(rows) > settings.replay_limit:
            return [_resync("replay_limit", hub.cursor)], hub.cursor, 0
        missed = [n for n in map(to_notification, rows) if n is not None]
        last_seq = int(rows[-1]["seq"]) if rows else after
    else:
        if len(missed) > settings.replay_limit:
            return [_resync("replay_limit", hub.cursor)], hub.cursor, 0
        last_seq = missed[-1].seq if missed else after

    oldest_allowed = datetime.now(timezone.utc) - timedelta(hours=settings.replay_hours)
    if missed and missed[0].created_at < oldest_allowed:
        return [_resync("cursor_expired", hub.cursor)], hub.cursor, 0
    return [notification.frame for notification in missed], max(after, last_seq), len(missed)
