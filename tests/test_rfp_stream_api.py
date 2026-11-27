"""`GET /rfp/notifications/stream`: the live RFP notification stream, asserted on the wire.

Each test reads the real response bytes and parses the SSE framing (`event:`,
`id:`, `data:`, comment frames), then checks `data` against the list's own
contract (`RfpTicketSummary`) and the briefing's Ticket fields (§2.3). Starlette's
TestClient returns a body only once the response ends, so these tests bound the
stream with a short `RFP_STREAM_MAX_SECONDS` (a stream also ends at its token's
expiry). Live delivery, dropped connections and recovery over a real socket are
in `test_rfp_stream_live.py`.
"""

import re
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.briefing import TicketStatus
from data.pipelines.rfp_intake.config import get_rfp_settings
from data.pipelines.rfp_intake.models import RfpTicketNotification, utcnow
from services.core import security
from services.rfp import notifications
from services.rfp import router as rfp_router
from services.rfp.models import RfpTicketSummary
from tests.pipelines.rfp_intake.support import PDF_DIR, SAMPLES, sqlite_engine
from tests.sse_support import Frame, events, parse_sse
from tests.test_agent_api import sign_in

STREAM = "/rfp/notifications/stream"
FORMAL_PDF = (PDF_DIR / SAMPLES["formal"]).read_bytes()
# Briefing §2.3: "Ticket: `ticket_id`, `rfp_id`, `status`, `raw_pdf_path`, `created_at`,
# `updated_at`". The server-side path is not something a browser needs.
BRIEFING_TICKET_FIELDS = {"ticket_id", "rfp_id", "status", "created_at", "updated_at"}
NEVER_SENT = {"raw_pdf_path", "uploaded_by", "pdf_sha256", "markdown"}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = sqlite_engine(tmp_path / "rfp-stream.sqlite")
    monkeypatch.setattr(rfp_router, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def bounded(monkeypatch):
    """Streams that end on their own: short lifetime, frequent heartbeats."""
    monkeypatch.setenv("RFP_STREAM_MAX_SECONDS", "0.5")
    monkeypatch.setenv("RFP_STREAM_HEARTBEAT_SECONDS", "0.2")
    get_rfp_settings.cache_clear()
    yield
    get_rfp_settings.cache_clear()


@pytest.fixture
def staff(client, engine, bounded, monkeypatch):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    # The intake itself is not what these tests are about: record it instead.
    monkeypatch.setattr(runner, "run_intake", lambda *args, **kwargs: None)
    headers, user_id = sign_in(client, "sales@trackflow.com", role="manager")
    return client, headers, user_id


def upload(client, headers, name="trackflow-request-1.pdf") -> dict:
    response = client.post(
        "/rfp/tickets", headers=headers, files={"file": (name, FORMAL_PDF, "application/pdf")}
    )
    assert response.status_code == 202, response.text
    return response.json()


def read_stream(client, headers, last_event_id: str | None = None):
    extra = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    response = client.get(STREAM, headers={**headers, **extra})
    return response, parse_sse(response.text)


def named(frames: list[Frame], name: str) -> list[Frame]:
    return [frame for frame in events(frames) if frame.event == name]


# --- The wire -------------------------------------------------------------------------


def test_the_stream_is_text_event_stream_with_keep_alive_comment_frames(staff):
    client, headers, _ = staff

    response, frames = read_stream(client, headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"] == "keep-alive"
    keep_alives = [frame for frame in frames if frame.is_comment]
    assert keep_alives, "an idle stream must send keep-alive comment frames"
    assert all(frame.comments == ["keep-alive"] for frame in keep_alives)
    assert re.search(r"(^|\n): keep-alive\n\n", response.text)
    # Never a generic "message": every event the browser dispatches is named.
    assert all(frame.event for frame in events(frames))


def test_a_registered_ticket_arrives_as_a_named_rfp_ticket_created_event(staff):
    client, headers, _ = staff
    accepted = upload(client, headers)

    response, frames = read_stream(client, headers, last_event_id="0")

    created = named(frames, "rfp_ticket_created")
    assert len(created) == 1
    frame = created[0]
    assert frame.id == "1"
    assert "event: rfp_ticket_created\n" in response.text
    assert re.search(r"\ndata: \{.*\}\n\n", response.text)
    data = frame.json()
    # The briefing's Ticket fields, under the briefing's names...
    assert set(data) >= BRIEFING_TICKET_FIELDS
    assert not NEVER_SENT & set(data)
    # ...which ticket, and that it needs processing (the briefing's first status).
    assert data["ticket_id"] == accepted["ticket_id"]
    assert data["rfp_id"] == accepted["rfp_id"]
    assert data["status"] == accepted["status"]
    assert data["status"] == TicketStatus.ANALYZING.value
    assert data["original_filename"] == "trackflow-request-1.pdf"
    datetime.fromisoformat(data["created_at"])
    # The same shape as one row of GET /rfp/tickets: the UI renders it without a refetch.
    assert set(data) == set(RfpTicketSummary.model_fields)
    listed = client.get("/rfp/tickets", headers=headers).json()["tickets"][0]
    assert RfpTicketSummary.model_validate(data) == RfpTicketSummary.model_validate(listed)


def test_the_stream_says_when_it_is_caught_up(staff):
    client, headers, _ = staff
    upload(client, headers)
    upload(client, headers)

    _, frames = read_stream(client, headers)

    # A first connection starts from now: nothing replayed, the cursor to resume from.
    assert not named(frames, "rfp_ticket_created")
    [ready] = named(frames, "rfp_stream_ready")
    assert ready.id == "2"
    assert ready.json() == {"cursor": 2, "replayed": 0, "heartbeat_seconds": 0.2}


# --- Authentication: the same JWT as the backoffice API --------------------------------


def test_unauthenticated_clients_receive_no_event(staff, client):
    _, _, user_id = staff
    expired, _ = security._encode(
        {"sub": user_id, "role": "manager", "typ": "access"}, timedelta(seconds=-5)
    )
    reset, _ = security.create_password_reset_token(user_id, "jti-1")
    attempts = [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {expired}"},
        # A password-reset link is not a session.
        {"Authorization": f"Bearer {reset}"},
    ]
    for headers in attempts:
        response = client.get(STREAM, headers=headers)
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"] == "Bearer"
        assert not response.headers["content-type"].startswith("text/event-stream")
        assert "event:" not in response.text


def test_a_customer_account_receives_no_event(staff, client):
    customer, _ = sign_in(client, "customer@example.com")

    response = client.get(STREAM, headers=customer)

    assert response.status_code == 403
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_the_stream_ends_when_its_token_expires(staff, monkeypatch):
    client, _, user_id = staff
    monkeypatch.setenv("RFP_STREAM_MAX_SECONDS", "60")
    get_rfp_settings.cache_clear()
    token, expires_at = security._encode(
        {"sub": user_id, "role": "manager", "typ": "access"}, timedelta(seconds=2)
    )

    started = time.monotonic()
    response, frames = read_stream(client, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert named(frames, "rfp_stream_ready")
    # Bounded by the token (at most 2 s), not by the 60 s lifetime.
    assert time.monotonic() - started < 5


# --- Recovery: Last-Event-ID ------------------------------------------------------------


def test_last_event_id_replays_only_what_the_client_missed_in_order(staff):
    client, headers, _ = staff
    first, second, third = (upload(client, headers, f"rfp-{n}.pdf") for n in (1, 2, 3))

    _, frames = read_stream(client, headers, last_event_id="1")

    created = named(frames, "rfp_ticket_created")
    assert [frame.id for frame in created] == ["2", "3"]
    assert [frame.json()["ticket_id"] for frame in created] == [
        second["ticket_id"],
        third["ticket_id"],
    ]
    assert first["ticket_id"] not in {frame.json()["ticket_id"] for frame in created}
    [ready] = named(frames, "rfp_stream_ready")
    assert ready.json()["cursor"] == 3 and ready.json()["replayed"] == 2
    # Replay first, then "caught up": the client knows which tickets it missed.
    assert events(frames).index(ready) == 2


def test_a_client_that_is_up_to_date_gets_nothing_twice(staff):
    client, headers, _ = staff
    upload(client, headers)

    _, frames = read_stream(client, headers, last_event_id="1")

    assert not named(frames, "rfp_ticket_created")
    assert named(frames, "rfp_stream_ready")[0].json()["replayed"] == 0


@pytest.mark.parametrize(
    ("last_event_id", "reason"),
    [("99", "cursor_unknown"), ("abc", "cursor_invalid"), ("-1", "cursor_invalid")],
)
def test_a_cursor_that_cannot_be_replayed_asks_for_a_resync(staff, last_event_id, reason):
    client, headers, _ = staff
    upload(client, headers)

    _, frames = read_stream(client, headers, last_event_id=last_event_id)

    [resync] = named(frames, "rfp_stream_resync")
    assert resync.json() == {"reason": reason, "cursor": 1}
    assert not named(frames, "rfp_ticket_created")
    assert named(frames, "rfp_stream_ready")[0].json()["cursor"] == 1


def test_too_many_missed_tickets_ask_for_a_resync_instead_of_a_flood(staff, monkeypatch):
    client, headers, _ = staff
    monkeypatch.setenv("RFP_STREAM_REPLAY_LIMIT", "2")
    get_rfp_settings.cache_clear()
    for n in range(3):
        upload(client, headers, f"rfp-{n}.pdf")

    _, frames = read_stream(client, headers, last_event_id="0")

    assert named(frames, "rfp_stream_resync")[0].json()["reason"] == "replay_limit"
    assert not named(frames, "rfp_ticket_created")


def test_missed_tickets_older_than_the_replay_window_ask_for_a_resync(staff, engine):
    client, headers, _ = staff
    upload(client, headers)
    with Session(engine) as session:
        session.execute(
            update(RfpTicketNotification).values(created_at=utcnow() - timedelta(hours=25))
        )
        session.commit()
    notifications.reset_hub()

    _, frames = read_stream(client, headers, last_event_id="0")

    assert named(frames, "rfp_stream_resync")[0].json()["reason"] == "cursor_expired"


def test_reprocessing_a_ticket_never_announces_it_again(staff, engine):
    client, headers, _ = staff
    accepted = upload(client, headers)
    store.fail(engine, accepted["ticket_id"], stage="convert_to_markdown", message="test")
    store.reset_ticket(engine, accepted["ticket_id"])

    _, frames = read_stream(client, headers, last_event_id="0")

    assert [frame.json()["ticket_id"] for frame in named(frames, "rfp_ticket_created")] == [
        accepted["ticket_id"]
    ]


# --- Wiring ----------------------------------------------------------------------------


def test_the_upload_wakes_the_stream_before_anything_else(staff, monkeypatch):
    client, headers, _ = staff
    order: list[str] = []
    monkeypatch.setattr(notifications, "wake", lambda: order.append("wake"))
    monkeypatch.setattr(runner, "run_intake", lambda *args, **kwargs: order.append("intake"))

    upload(client, headers)

    assert order == ["wake", "intake"]


def test_an_unreadable_feed_is_a_503_before_any_event(staff, monkeypatch):
    client, headers, _ = staff

    def down(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(store, "latest_notification_seq", down)

    response = client.get(STREAM, headers=headers)

    assert response.status_code == 503
    assert response.json() == {"detail": "RFP storage unavailable"}
