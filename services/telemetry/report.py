"""The technical report served by `GET /telemetry/report`.

The period is resolved here, once, and the same `start` and `end` go to every
metric function in `analysis.py`; none of them has a default window of its own.
Finished reports are cached for 60 seconds, so the pipeline does not run on
every request.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Lock

from sqlalchemy.exc import SQLAlchemyError

from services.telemetry import analysis
from services.telemetry.models import TelemetryReport

DEFAULT_WINDOW = timedelta(days=7)
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 32

CacheKey = tuple[datetime | None, datetime | None]


class InvalidPeriod(ValueError):
    """The requested period is empty or reversed."""


class ReportUnavailable(Exception):
    """PostgreSQL could not be read. Nothing was calculated; a retry is safe."""


def to_utc(value: datetime) -> datetime:
    """A datetime without an offset is read as UTC, like event timestamps at ingest."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_period(
    start_date: datetime | None, end_date: datetime | None, *, now: datetime
) -> tuple[datetime, datetime]:
    """The `[start, end)` window in UTC.

    `end_date` defaults to `now` and `start_date` to seven days before the end,
    so no parameter means the last seven days and an end alone the seven days
    before it.
    """
    end = to_utc(end_date) if end_date is not None else to_utc(now)
    start = to_utc(start_date) if start_date is not None else end - DEFAULT_WINDOW
    if start >= end:
        raise InvalidPeriod("start_date must be before end_date")
    return start, end


def build_report(start: datetime, end: datetime) -> TelemetryReport:
    """Run every metric of the pipeline over the same period."""
    try:
        metrics = {
            "events_per_day": analysis.events_per_day(start, end),
            "error_rate_by_type": analysis.error_rate_by_type(start, end),
            "latency_by_route": analysis.latency_by_route(start, end),
            "event_type_share": analysis.event_type_share(start, end),
        }
    except SQLAlchemyError as error:
        # The class name only: a driver message can quote the connection URL.
        raise ReportUnavailable(type(error).__name__) from None

    return TelemetryReport.model_validate(
        {"period": {"from": start, "to": end}, "metrics": metrics}
    )


class ReportCache:
    """Finished reports, kept for a fixed time per requested period. Process-local.

    Bounded because its keys come from the query string: past `max_entries` the
    least recently used report is dropped, and expired ones go whenever a
    report is stored. Two identical requests that arrive together may both
    calculate; the report stored last is kept. Nothing is stored for a request
    that failed.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("ttl_seconds and max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, TelemetryReport]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: CacheKey) -> TelemetryReport | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, cached = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return cached

    def set(self, key: CacheKey, value: TelemetryReport) -> None:
        now = self._clock()
        with self._lock:
            expired = [k for k, (expires_at, _) in self._entries.items() if expires_at <= now]
            for k in expired:
                del self._entries[k]
            self._entries[key] = (now + self._ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


report_cache = ReportCache(ttl_seconds=CACHE_TTL_SECONDS, max_entries=CACHE_MAX_ENTRIES)


def get_report(
    start_date: datetime | None, end_date: datetime | None, *, now: datetime
) -> TelemetryReport:
    """The report for the requested period, from the cache while it is younger than the TTL.

    The key is the period as requested - each parameter in UTC, None when it
    was left out - not as resolved. Resolved, a request without parameters
    would end at a new `now` every time and never hit. So within the TTL the
    default report is the one calculated first, ending up to 60 seconds ago.
    The same instant written with different offsets shares one entry.
    """
    key = (
        to_utc(start_date) if start_date is not None else None,
        to_utc(end_date) if end_date is not None else None,
    )
    cached = report_cache.get(key)
    if cached is not None:
        return cached

    start, end = resolve_period(start_date, end_date, now=now)
    fresh = build_report(start, end)
    report_cache.set(key, fresh)
    return fresh
