"""`/operations/alerts` and the alert events on the live stream, asserted on the wire.

The stream tests read the real response bytes of `GET /notifications/stream`
(bounded by a short `LIVE_STREAM_MAX_SECONDS`, since TestClient returns a body
only once the response ends) and check the named events, the `id:` seq and the
`data:` JSON against `OperationalAlert`. RFP tickets and alerts share one cursor.
"""

import asyncio

import pytest

from data.pipelines.rfp_intake import runner
from services.core import security
from services.notifications import router as notifications_router
from services.notifications import stream as notifications
from services.notifications.config import get_live_stream_settings
from services.operations import lifecycle, monitor
from services.operations import router as operations_router
from services.operations.config import get_operations_settings
from services.operations.schemas import OperationalAlert
from services.rfp import router as rfp_router
from tests.operations.support import announcements, at, ops_engine, order, product, rules
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES
from tests.sse_support import events, parse_sse
from tests.test_agent_api import sign_in

STREAM = "/notifications/stream"
ZARAGOZA = rules(
    {
        "rule_id": "zaragoza-outbound",
        "source": "warehouse_orders",
        "warehouse": "Zaragoza",
        "activity": "outbound_order_created",
        "expected_within_minutes": 120,
    }
)
NOW = at("2026-11-30 10:00")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = ops_engine(tmp_path / "ops-api.sqlite")
    for module in (operations_router, notifications_router, rfp_router):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("LIVE_STREAM_MAX_SECONDS", "0.5")
    monkeypatch.setenv("LIVE_STREAM_HEARTBEAT_SECONDS", "0.2")
    get_live_stream_settings.cache_clear()
    yield engine
    engine.dispose()


@pytest.fixture
def staff(client, engine, monkeypatch):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, _ = sign_in(client, "ana.whitfield@trackflow.com", role="manager")
    return client, headers


def open_alert(engine) -> OperationalAlert:
    product(engine, "Zaragoza")
    [change] = monitor.evaluate(engine, ZARAGOZA, now=NOW)
    return change.alert


# --- The snapshot --------------------------------------------------------------------------------


def test_the_open_alerts_are_a_staff_only_snapshot(staff, client, engine):
    client_, headers = staff
    alert = open_alert(engine)
    customer, _ = sign_in(client, "customer@example.com")

    assert client.get("/operations/alerts").status_code == 401
    assert client.get("/operations/alerts", headers=customer).status_code == 403
    response = client_.get("/operations/alerts", headers=headers)

    assert response.status_code == 200
    [listed] = response.json()["alerts"]
    assert OperationalAlert.model_validate(listed) == alert
    assert client_.get("/operations/alerts?status=resolved", headers=headers).json() == {
        "alerts": []
    }


# --- Acknowledging: REST up, broadcast down -------------------------------------------------------


def test_acknowledging_takes_the_alert_in_charge_once_and_announces_it(staff, engine, monkeypatch):
    client, headers = staff
    alert = open_alert(engine)
    woken: list[bool] = []
    monkeypatch.setattr(notifications, "wake", lambda: woken.append(True))
    url = f"/operations/alerts/{alert.alert_id}/acknowledge"

    first = client.post(url, headers=headers)
    again = client.post(url, headers=headers)

    assert first.status_code == again.status_code == 200
    assert first.json()["acknowledged_by"] == "ana.whitfield@trackflow.com"
    assert again.json()["acknowledged_at"] == first.json()["acknowledged_at"]
    assert [row["event"] for row in announcements(engine)] == [
        "operational_inactivity_detected",
        "operational_alert_acknowledged",
    ]
    assert woken == [True]  # the stream was woken for the one real change


def test_a_resolved_unknown_or_customer_acknowledgement_is_refused(staff, client, engine):
    client_, headers = staff
    alert = open_alert(engine)
    order(engine, product(engine, "Zaragoza"), NOW)
    monitor.evaluate(engine, ZARAGOZA, now=NOW)  # resolved
    customer, _ = sign_in(client, "customer@example.com")

    resolved = client_.post(f"/operations/alerts/{alert.alert_id}/acknowledge", headers=headers)
    unknown = client_.post(f"/operations/alerts/{'0' * 32}/acknowledge", headers=headers)
    forbidden = client.post(f"/operations/alerts/{alert.alert_id}/acknowledge", headers=customer)

    assert (resolved.status_code, unknown.status_code, forbidden.status_code) == (409, 404, 403)
    assert resolved.json() == {"detail": "The activity has resumed: this alert is resolved."}


# --- On the wire ----------------------------------------------------------------------------------


def test_an_inactivity_alert_is_a_named_event_whose_data_is_the_alert(staff, engine):
    client, headers = staff
    alert = open_alert(engine)

    response = client.get(STREAM, headers={**headers, "Last-Event-ID": "0"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\nevent: operational_inactivity_detected\ndata: {" in response.text
    frames = parse_sse(response.text)
    [detected] = [
        frame for frame in events(frames) if frame.event == "operational_inactivity_detected"
    ]
    assert detected.id == "1"
    data = detected.json()
    assert OperationalAlert.model_validate(data) == alert
    assert data["subject"] == {"warehouse": "Zaragoza", "client_id": None}
    assert data["activity"] == "outbound_order_created"
    assert data["status"] == "open"
    assert any(frame.comments == ["keep-alive"] for frame in frames)


def test_tickets_and_alerts_share_one_cursor_in_commit_order(staff, engine, monkeypatch):
    client, headers = staff
    monkeypatch.setattr(runner, "run_intake", lambda *args, **kwargs: None)
    pdf = (PDF_DIR / SAMPLES["formal"]).read_bytes()
    client.post(
        "/rfp/tickets", headers=headers, files={"file": ("a.pdf", pdf, "application/pdf")}
    )  # seq 1
    alert = open_alert(engine)  # seq 2
    client.post(f"/operations/alerts/{alert.alert_id}/acknowledge", headers=headers)  # seq 3
    order(engine, product(engine, "Zaragoza"), NOW)
    monitor.evaluate(engine, ZARAGOZA, now=NOW)  # seq 4

    # A tab that saw the ticket (seq 1) and dropped resumes with everything after it.
    frames = events(parse_sse(client.get(STREAM, headers={**headers, "Last-Event-ID": "1"}).text))

    assert [(frame.id, frame.event) for frame in frames] == [
        ("2", "operational_inactivity_detected"),
        ("3", "operational_alert_acknowledged"),
        ("4", "operational_activity_resumed"),
        ("4", "rfp_stream_ready"),
    ]
    assert frames[-1].json()["replayed"] == 3
    assert {frame.json()["alert_id"] for frame in frames[:3]} == {str(alert.alert_id)}
    # The old RFP route is the same stream.
    old = events(
        parse_sse(
            client.get("/rfp/notifications/stream", headers={**headers, "Last-Event-ID": "3"}).text
        )
    )
    assert [frame.event for frame in old] == ["operational_activity_resumed", "rfp_stream_ready"]


# --- The monitor in the API -----------------------------------------------------------------------


def test_a_monitor_round_wakes_the_stream_only_when_something_changed(monkeypatch):
    woken: list[bool] = []
    monkeypatch.setattr(notifications, "wake", lambda: woken.append(True))
    results = iter([["a change"], []])
    monkeypatch.setattr(lifecycle, "evaluate", lambda engine, rules: next(results))
    monkeypatch.setattr(lifecycle, "get_engine", lambda: None)

    assert asyncio.run(lifecycle.run_once()) == 1
    assert asyncio.run(lifecycle.run_once()) == 0
    assert woken == [True]


def test_a_failing_round_is_logged_and_the_api_carries_on(monkeypatch, caplog):
    def broken(engine, rules):
        raise RuntimeError("database gone")

    monkeypatch.setattr(lifecycle, "evaluate", broken)
    monkeypatch.setattr(lifecycle, "get_engine", lambda: None)

    assert asyncio.run(lifecycle.run_once()) == 0
    assert "operational monitor round failed: RuntimeError" in caplog.text


def test_the_monitor_does_not_start_when_disabled(monkeypatch):
    async def start_and_report():
        lifecycle.start()
        running = lifecycle._task is not None  # noqa: SLF001
        await lifecycle.stop()
        return running

    assert asyncio.run(start_and_report()) is False  # OPS_MONITOR_ENABLED=false in the tests
    monkeypatch.setenv("OPS_MONITOR_ENABLED", "true")
    monkeypatch.setenv("OPS_MONITOR_INTERVAL_SECONDS", "3600")
    get_operations_settings.cache_clear()
    monkeypatch.setattr(lifecycle, "evaluate", lambda engine, rules: [])
    monkeypatch.setattr(lifecycle, "get_engine", lambda: None)
    assert asyncio.run(start_and_report()) is True
