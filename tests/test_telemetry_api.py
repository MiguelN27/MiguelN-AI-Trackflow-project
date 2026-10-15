import json
from uuid import uuid4

from services.telemetry.models import MAX_BATCH_SIZE


def make_event(**overrides):
    event = {
        "eventId": str(uuid4()),
        "timestamp": "2026-10-15T10:00:00Z",
        "sessionId": str(uuid4()),
        "userId": None,
        "event_type": "section_viewed",
        "schemaVersion": "1.0.0",
        "requestId": str(uuid4()),
        "properties": {"section": "login"},
    }
    return {**event, **overrides}


def test_accepts_batch_without_auth(client):
    response = client.post("/telemetry/events", json={"events": [make_event(), make_event()]})

    assert response.status_code == 200
    assert response.json() == {"received": 2}


def test_accepts_text_plain_body_as_sent_by_beacon(client):
    response = client.post(
        "/telemetry/events",
        content=json.dumps({"events": [make_event()]}),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )

    assert response.status_code == 200
    assert response.json() == {"received": 1}


def test_rejects_empty_batch(client):
    response = client.post("/telemetry/events", json={"events": []})

    assert response.status_code == 422


def test_rejects_oversized_batch(client):
    events = [make_event() for _ in range(MAX_BATCH_SIZE + 1)]

    assert client.post("/telemetry/events", json={"events": events}).status_code == 422


def test_rejects_malformed_event(client):
    response = client.post("/telemetry/events", json={"events": [make_event(eventId="not-a-uuid")]})

    assert response.status_code == 422


def test_rejects_invalid_json(client):
    response = client.post("/telemetry/events", content="{not json")

    assert response.status_code == 422


def test_rejects_oversized_body(client):
    padding = "x" * (256 * 1024)
    response = client.post(
        "/telemetry/events", json={"events": [make_event(properties={"p": padding})]}
    )

    assert response.status_code == 413


def test_logs_event_types(client, caplog):
    with caplog.at_level("INFO", logger="api.telemetry"):
        client.post("/telemetry/events", json={"events": [make_event()]})

    assert "section_viewed" in caplog.text
