"""`GET /telemetry/report`: period resolution, the response shape, failures and the cache.

The metric functions are replaced by recorders here; what they calculate is
covered by `tests/test_telemetry_analysis.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from sqlalchemy.exc import OperationalError

from services.telemetry import analysis, report

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

ROWS = {
    "events_per_day": [
        {"date": "2026-10-20", "events": 3, "sessions": 1, "event_types": 2, "partial_day": True}
    ],
    "error_rate_by_type": [
        {
            "date": "2026-10-20",
            "error_class": "auth",
            "errors": 1,
            "requests": 4,
            "error_rate": 0.25,
        }
    ],
    "latency_by_route": [
        {
            "date": "2026-10-20",
            "method": "POST",
            "route_template": "/auth/login",
            "samples": 4,
            "requests": 4,
            "p50_ms": 200.0,
            "p95_ms": 400.0,
            "mean_ms": 250.0,
            "max_ms": 400.0,
            "error_rate": 0.25,
        }
    ],
    "event_type_share": [
        {"date": "2026-10-20", "event_type": "section_viewed", "events": 3, "share": 1.0}
    ],
}


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A fresh, empty report cache per test, on a clock the test moves itself."""
    fake = FakeClock()
    cache = report.ReportCache(
        ttl_seconds=report.CACHE_TTL_SECONDS, max_entries=report.CACHE_MAX_ENTRIES, clock=fake
    )
    monkeypatch.setattr(report, "report_cache", cache)
    return fake


@pytest.fixture
def metrics(monkeypatch):
    """Every call each metric function receives, as `(name, start, end)`."""
    calls = []

    def recorder(name):
        def metric(start, end):
            calls.append((name, start, end))
            return ROWS[name]

        return metric

    for name in ROWS:
        monkeypatch.setattr(analysis, name, recorder(name))
    return calls


def periods(calls):
    return {(start, end) for _, start, end in calls}


def test_report_is_public_and_has_the_agreed_shape(client, metrics):
    response = client.get(
        "/telemetry/report",
        params={"start_date": "2026-10-13T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "period": {"from": "2026-10-13T00:00:00Z", "to": "2026-10-20T00:00:00Z"},
        "metrics": ROWS,
    }


@time_machine.travel(NOW, tick=False)
def test_without_parameters_the_period_is_the_last_seven_days(client, metrics):
    response = client.get("/telemetry/report")

    assert response.json()["period"] == {
        "from": "2026-10-13T12:00:00Z",
        "to": "2026-10-20T12:00:00Z",
    }
    assert periods(metrics) == {
        (
            datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
        )
    }


def test_the_period_is_resolved_once_and_every_metric_gets_it(client, metrics):
    client.get(
        "/telemetry/report",
        params={"start_date": "2026-10-13T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"},
    )

    assert sorted(name for name, _, _ in metrics) == sorted(ROWS)
    assert len(periods(metrics)) == 1


@pytest.mark.parametrize(
    ("start_date", "expected"),
    [
        ("2026-10-13T00:00:00Z", datetime(2026, 10, 13, tzinfo=timezone.utc)),
        ("2026-10-13T02:00:00+02:00", datetime(2026, 10, 13, tzinfo=timezone.utc)),
        ("2026-10-12T18:00:00-06:00", datetime(2026, 10, 13, tzinfo=timezone.utc)),
        ("2026-10-13T00:00:00", datetime(2026, 10, 13, tzinfo=timezone.utc)),
        ("2026-10-13", datetime(2026, 10, 13, tzinfo=timezone.utc)),
    ],
)
def test_iso_8601_dates_are_converted_to_utc(client, metrics, start_date, expected):
    response = client.get(
        "/telemetry/report",
        params={"start_date": start_date, "end_date": "2026-10-20T00:00:00Z"},
    )

    assert response.status_code == 200
    assert response.json()["period"]["from"] == "2026-10-13T00:00:00Z"
    assert {start for start, _ in periods(metrics)} == {expected}
    assert {start.tzinfo for start, _ in periods(metrics)} == {timezone.utc}


def test_an_end_alone_reports_the_seven_days_before_it(client, metrics):
    response = client.get("/telemetry/report", params={"end_date": "2026-10-10T00:00:00Z"})

    assert response.json()["period"] == {
        "from": "2026-10-03T00:00:00Z",
        "to": "2026-10-10T00:00:00Z",
    }


@time_machine.travel(NOW, tick=False)
def test_a_start_alone_reports_up_to_now(client, metrics):
    response = client.get("/telemetry/report", params={"start_date": "2026-10-01T00:00:00Z"})

    assert response.json()["period"] == {
        "from": "2026-10-01T00:00:00Z",
        "to": "2026-10-20T12:00:00Z",
    }


@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "last tuesday"},
        {"end_date": "2026-13-01T00:00:00Z"},
        {"start_date": "2026-10-20T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"},
        {"start_date": "2026-10-21T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"},
    ],
)
def test_an_unreadable_or_empty_period_is_refused(client, metrics, params):
    response = client.get("/telemetry/report", params=params)

    assert response.status_code == 422
    assert metrics == []


def test_a_reversed_period_names_the_parameter(client, metrics):
    response = client.get(
        "/telemetry/report",
        params={"start_date": "2026-10-21T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"},
    )

    assert response.json() == {
        "detail": [
            {
                "loc": ["query", "start_date"],
                "msg": "start_date must be before end_date",
                "type": "value_error",
            }
        ]
    }


def test_database_failure_answers_503_without_details(client, metrics, monkeypatch):
    def unreachable(start, end):
        raise OperationalError("SELECT ...", {}, Exception("password=secret host=db"))

    monkeypatch.setattr(analysis, "latency_by_route", unreachable)

    response = client.get("/telemetry/report")

    assert response.status_code == 503
    assert response.json() == {"detail": "Telemetry report unavailable"}
    assert "secret" not in response.text


# --- the 60-second cache ----------------------------------------------------

PERIOD = {"start_date": "2026-10-13T00:00:00Z", "end_date": "2026-10-20T00:00:00Z"}


def runs(calls):
    """How many times the whole pipeline ran: each run calls every metric once."""
    assert len(calls) % len(ROWS) == 0
    return len(calls) // len(ROWS)


def test_the_cache_ttl_is_sixty_seconds():
    assert report.CACHE_TTL_SECONDS == 60


def test_the_same_period_within_the_ttl_is_served_without_recalculating(client, metrics, clock):
    first = client.get("/telemetry/report", params=PERIOD)
    clock.advance(59.9)
    second = client.get("/telemetry/report", params=PERIOD)

    assert runs(metrics) == 1
    assert second.status_code == 200
    assert second.json() == first.json()


def test_the_report_is_recalculated_once_the_ttl_has_passed(client, metrics, clock):
    client.get("/telemetry/report", params=PERIOD)
    clock.advance(60)
    client.get("/telemetry/report", params=PERIOD)

    assert runs(metrics) == 2


def test_another_period_is_calculated_on_its_own(client, metrics):
    client.get("/telemetry/report", params=PERIOD)
    client.get("/telemetry/report", params={**PERIOD, "end_date": "2026-10-19T00:00:00Z"})
    client.get("/telemetry/report", params={"start_date": PERIOD["start_date"]})

    assert runs(metrics) == 3


def test_the_same_instants_in_other_offsets_share_the_cached_report(client, metrics):
    client.get("/telemetry/report", params=PERIOD)
    client.get(
        "/telemetry/report",
        params={"start_date": "2026-10-13T02:00:00+02:00", "end_date": "2026-10-19T18:00:00-06:00"},
    )

    assert runs(metrics) == 1


def test_the_default_period_is_cached_and_keeps_the_period_it_was_calculated_for(
    client, metrics, clock
):
    with time_machine.travel(NOW, tick=False):
        first = client.get("/telemetry/report")
    clock.advance(30)
    with time_machine.travel(NOW + timedelta(seconds=30), tick=False):
        cached = client.get("/telemetry/report")
    clock.advance(30)
    with time_machine.travel(NOW + timedelta(seconds=60), tick=False):
        fresh = client.get("/telemetry/report")

    assert runs(metrics) == 2
    assert cached.json()["period"] == first.json()["period"]
    assert fresh.json()["period"] == {"from": "2026-10-13T12:01:00Z", "to": "2026-10-20T12:01:00Z"}


def test_a_failed_report_is_not_cached(client, metrics, monkeypatch):
    attempts = []

    def flaky(start, end):
        attempts.append(start)
        if len(attempts) == 1:
            raise OperationalError("SELECT ...", {}, Exception("timeout"))
        return ROWS["latency_by_route"]

    monkeypatch.setattr(analysis, "latency_by_route", flaky)

    assert client.get("/telemetry/report", params=PERIOD).status_code == 503
    assert len(report.report_cache) == 0
    assert client.get("/telemetry/report", params=PERIOD).status_code == 200
    assert client.get("/telemetry/report", params=PERIOD).status_code == 200
    assert len(attempts) == 2


def test_the_cache_keeps_at_most_max_entries_dropping_the_least_recently_used():
    clock = FakeClock()
    cache = report.ReportCache(ttl_seconds=60, max_entries=2, clock=clock)
    a, b, c = (object() for _ in range(3))

    cache.set(("a", None), a)
    cache.set(("b", None), b)
    assert cache.get(("a", None)) is a  # now the most recently used
    cache.set(("c", None), c)

    assert len(cache) == 2
    assert cache.get(("b", None)) is None
    assert cache.get(("a", None)) is a
    assert cache.get(("c", None)) is c


def test_expired_reports_are_dropped_when_another_is_stored():
    clock = FakeClock()
    cache = report.ReportCache(ttl_seconds=60, max_entries=32, clock=clock)

    cache.set(("a", None), object())
    clock.advance(61)
    cache.set(("b", None), object())

    assert len(cache) == 1
