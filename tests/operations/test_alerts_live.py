"""Inactivity alerts over a real socket: pushed live, and recovered after a dropped connection.

The API runs under uvicorn on a free local port (as the RFP live tests do), the
dashboard is a real streaming connection read in a thread (`SseReader`), and
the monitor is evaluated from the test the way the API's own task does it
(`evaluate`, then `wake`).
"""

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from services.core import security
from services.main import app
from services.notifications import router as notifications_router
from services.notifications import stream as notifications
from services.notifications.config import get_live_stream_settings
from services.operations import monitor
from services.operations import router as operations_router
from tests.operations.support import at, ops_engine, order, product, rules
from tests.sse_support import SseReader
from tests.test_agent_api import sign_in

ZARAGOZA = rules(
    {
        "rule_id": "zaragoza-outbound",
        "source": "warehouse_orders",
        "warehouse": "Zaragoza",
        "activity": "outbound_order_created",
        "expected_within_minutes": 120,
    }
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met"
        time.sleep(0.02)


@pytest.fixture
def api(client, tmp_path, monkeypatch) -> Iterator[tuple[str, dict[str, str], object]]:
    engine = ops_engine(tmp_path / "ops-live.sqlite")
    for module in (operations_router, notifications_router):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("LIVE_STREAM_POLL_SECONDS", "30")
    monkeypatch.setenv("LIVE_STREAM_HEARTBEAT_SECONDS", "0.2")
    get_live_stream_settings.cache_clear()
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, _ = sign_in(client, "carlos.vega@trackflow.com", role="manager")
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
    _until(lambda: server.started, 10)
    yield f"http://127.0.0.1:{port}", headers, engine
    server.should_exit = True
    thread.join(timeout=10)
    engine.dispose()


def evaluate(engine, now):
    """What the API's monitor task does each round."""
    changes = monitor.evaluate(engine, ZARAGOZA, now=now)
    if changes:
        notifications.wake()
    return changes


def ready(reader: SseReader):
    frame = reader.next_event()
    while frame.event != "rfp_stream_ready":
        frame = reader.next_event()
    return frame


def test_an_alert_reaches_an_open_dashboard_at_once(api):
    base_url, headers, engine = api
    item = product(engine, "Zaragoza")
    reader = SseReader(base_url, headers)
    try:
        ready(reader)
        started = time.monotonic()
        [opened] = evaluate(engine, at("2026-11-30 10:00"))
        detected = reader.next_event(timeout=2)
        elapsed = time.monotonic() - started

        order(engine, item, at("2026-11-30 10:05"))
        evaluate(engine, at("2026-11-30 10:06"))
        resumed = reader.next_event(timeout=2)
    finally:
        reader.close()

    assert detected.event == "operational_inactivity_detected"
    assert detected.json()["alert_id"] == str(opened.alert.alert_id)
    assert elapsed < 1.5, f"took {elapsed:.2f}s with a 30 s poll"
    assert resumed.event == "operational_activity_resumed"
    assert resumed.json()["alert_id"] == str(opened.alert.alert_id)


def test_an_alert_raised_while_disconnected_is_recovered_once(api):
    base_url, headers, engine = api
    item = product(engine, "Zaragoza")
    reader = SseReader(base_url, headers)
    cursor = ready(reader).id
    reader.close()  # the network drops
    _until(lambda: notifications.get_hub().subscriber_count == 0)

    [opened] = evaluate(engine, at("2026-11-30 10:00"))  # while nobody is connected

    reader = SseReader(base_url, {**headers, "Last-Event-ID": str(cursor)})
    try:
        replayed = reader.next_event()
        caught_up = reader.next_event()
        order(engine, item, at("2026-11-30 10:05"))
        evaluate(engine, at("2026-11-30 10:06"))
        live = reader.next_event(timeout=2)
    finally:
        reader.close()

    assert (replayed.event, caught_up.event) == (
        "operational_inactivity_detected",
        "rfp_stream_ready",
    )
    assert caught_up.json()["replayed"] == 1
    assert live.event == "operational_activity_resumed"
    alert_events = [replayed, live]
    assert [frame.json()["alert_id"] for frame in alert_events] == [str(opened.alert.alert_id)] * 2
    assert [frame.json()["status"] for frame in alert_events] == ["open", "resolved"]
    assert int(replayed.id or 0) < int(live.id or 0)
