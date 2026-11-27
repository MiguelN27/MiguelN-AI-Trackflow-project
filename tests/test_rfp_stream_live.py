"""The live RFP notification stream over a real socket: push, drop, recover.

The API runs under uvicorn on a free local port (as `tests/mcps/test_agent_over_mcp.py`
does), against SQLite. Dashboards are real streaming HTTP connections read in a
thread (`SseReader`), so a test can upload while they listen, and cut a
connection on purpose the way a lost network does. Covers the ticket's checklist:
- a new ticket reaches every open dashboard at once, with no polling;
- a ticket registered by another process (the CLI) arrives too;
- after a dropped connection, the tickets registered meanwhile are recovered by
  `Last-Event-ID`, then live delivery continues, and no `ticket_id` arrives twice;
- after an API restart (an empty in-memory ring), the replay comes from the database.

The browser side of reconnection (backoff, dedupe in the UI) is in
`tests/frontend/backoffice/rfp-stream.test.ts`.
"""

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from data.pipelines.rfp_intake import runner
from data.pipelines.rfp_intake.config import get_rfp_settings
from services.core import security
from services.main import app
from services.rfp import notifications
from services.rfp import router as rfp_router
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES, new_ticket, sqlite_engine
from tests.sse_support import Frame, SseReader
from tests.test_agent_api import sign_in

FORMAL_PDF = (PDF_DIR / SAMPLES["formal"]).read_bytes()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _until(condition, timeout: float = 5.0, message: str = "condition not met") -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = sqlite_engine(tmp_path / "rfp-live.sqlite")
    monkeypatch.setattr(rfp_router, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings(monkeypatch):
    # A watcher poll far longer than any assertion below, unless a test lowers it:
    # what arrives at once arrived through the upload's wake-up, not a poll.
    monkeypatch.setenv("RFP_STREAM_POLL_SECONDS", "30")
    monkeypatch.setenv("RFP_STREAM_HEARTBEAT_SECONDS", "0.2")
    monkeypatch.setenv("RFP_STREAM_MAX_SECONDS", "60")
    get_rfp_settings.cache_clear()
    yield
    get_rfp_settings.cache_clear()


@pytest.fixture
def api(client, engine, settings, monkeypatch) -> Iterator[tuple[str, dict[str, str]]]:
    """The API on a real socket; yields its base URL and a staff account's headers."""
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    monkeypatch.setattr(runner, "run_intake", lambda *args, **kwargs: None)
    headers, _ = sign_in(client, "sales@trackflow.com", role="manager")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _until(lambda: server.started, 10, "the API did not start")
    yield f"http://127.0.0.1:{port}", headers
    server.should_exit = True
    thread.join(timeout=10)


def upload(base_url: str, headers: dict[str, str], name: str) -> str:
    response = httpx.post(
        f"{base_url}/rfp/tickets",
        headers=headers,
        files={"file": (name, FORMAL_PDF, "application/pdf")},
        timeout=10,
    )
    assert response.status_code == 202, response.text
    return response.json()["ticket_id"]


def connected() -> int:
    return notifications.get_hub().subscriber_count


def until_ready(reader: SseReader) -> Frame:
    frame = reader.next_event()
    while frame.event == "rfp_ticket_created":
        frame = reader.next_event()
    assert frame.event == "rfp_stream_ready", frame
    return frame


def test_a_new_ticket_reaches_every_open_dashboard_at_once(api):
    base_url, headers = api
    first, second = SseReader(base_url, headers), SseReader(base_url, headers)
    try:
        assert first.status == second.status == 200
        assert first.headers is not None
        assert first.headers["content-type"].startswith("text/event-stream")
        until_ready(first), until_ready(second)
        assert connected() == 2

        started = time.monotonic()
        ticket_id = upload(base_url, headers, "trackflow-request-1.pdf")
        events = [first.next_event(timeout=2), second.next_event(timeout=2)]
        elapsed = time.monotonic() - started

        for event in events:
            assert event.event == "rfp_ticket_created"
            assert event.json()["ticket_id"] == ticket_id
            assert event.json()["status"] == "analyzing"
        # Pushed by the upload itself: the watcher's poll is 30 s here.
        assert elapsed < 1.5, f"took {elapsed:.2f}s"
    finally:
        first.close()
        second.close()
    # A closed tab is noticed and forgotten: the hub keeps no dead mailbox.
    _until(lambda: connected() == 0, 5, "the dropped connections were not detected")


def test_a_ticket_registered_by_another_process_arrives_within_the_poll_interval(
    api, engine, tmp_path, monkeypatch
):
    base_url, headers = api
    monkeypatch.setenv("RFP_STREAM_POLL_SECONDS", "0.3")
    get_rfp_settings.cache_clear()
    notifications.reset_hub()
    reader = SseReader(base_url, headers)
    try:
        until_ready(reader)
        # As `npm run rfp -- run` does: the store, from outside the API, no wake-up.
        ticket_id = new_ticket(engine, tmp_path / "cli")

        event = reader.next_event(timeout=3)

        assert event.event == "rfp_ticket_created"
        assert event.json()["ticket_id"] == str(ticket_id)
    finally:
        reader.close()


def test_a_dropped_connection_recovers_the_missed_tickets_without_duplicates(api):
    base_url, headers = api
    seen: list[str] = []

    reader = SseReader(base_url, headers)
    until_ready(reader)
    upload(base_url, headers, "before-the-drop.pdf")
    event = reader.next_event()
    seen.append(event.json()["ticket_id"])
    last_event_id = event.id
    assert last_event_id is not None
    reader.close()  # the network drops
    _until(lambda: connected() == 0, 5, "the drop was not detected")

    missed = [upload(base_url, headers, f"while-offline-{n}.pdf") for n in (1, 2)]

    reader = SseReader(base_url, {**headers, "Last-Event-ID": last_event_id})
    try:
        replayed = [reader.next_event(), reader.next_event()]
        ready = reader.next_event()
        live_ticket = upload(base_url, headers, "after-the-reconnect.pdf")
        live = reader.next_event(timeout=2)
    finally:
        reader.close()

    assert [frame.event for frame in replayed] == ["rfp_ticket_created"] * 2
    assert [frame.json()["ticket_id"] for frame in replayed] == missed
    assert ready.event == "rfp_stream_ready"
    assert ready.json()["replayed"] == 2
    assert live.event == "rfp_ticket_created" and live.json()["ticket_id"] == live_ticket
    seen += [frame.json()["ticket_id"] for frame in (*replayed, live)]
    # Every ticket exactly once, in registration order.
    assert seen == [seen[0], *missed, live_ticket]
    assert len(set(seen)) == len(seen) == 4
    ids = [int(frame.id or 0) for frame in (*replayed, live)]
    assert ids == sorted(ids) and ids[0] == int(last_event_id) + 1


def test_after_an_api_restart_the_missed_tickets_come_from_the_database(api, engine, tmp_path):
    base_url, headers = api
    reader = SseReader(base_url, headers)
    until_ready(reader)
    upload(base_url, headers, "before-the-restart.pdf")
    last_event_id = reader.next_event().id
    reader.close()
    _until(lambda: connected() == 0, 5, "the drop was not detected")

    # The API restarts (a new hub, nothing in memory) and the CLI registers a ticket meanwhile.
    notifications.reset_hub()
    while_down = str(new_ticket(engine, tmp_path / "cli"))

    reader = SseReader(base_url, {**headers, "Last-Event-ID": str(last_event_id)})
    try:
        event = reader.next_event()
        ready = reader.next_event()
    finally:
        reader.close()

    assert event.event == "rfp_ticket_created" and event.json()["ticket_id"] == while_down
    assert ready.event == "rfp_stream_ready" and ready.json()["replayed"] == 1
