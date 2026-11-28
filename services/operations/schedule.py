"""Counting only the time a location is expected to be active.

`active_minutes_between` is the clock every rule runs on: wall-clock minutes for
a rule without `operating_hours`, otherwise only the minutes inside the weekly
window, in the location's own timezone. Differences are taken in UTC, so a
daylight-saving change never adds or loses an hour.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from services.operations.rules import DAYS, OperatingHours


def _minutes(start: datetime, end: datetime) -> float:
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 60


def active_minutes_between(start: datetime, end: datetime, hours: OperatingHours | None) -> float:
    if end <= start:
        return 0.0
    if hours is None:
        return _minutes(start, end)
    zone = ZoneInfo(hours.timezone)
    # Aware datetimes sharing a tzinfo compare and subtract as wall times, so
    # every comparison below is between UTC instants.
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    total = 0.0
    day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day:
        if DAYS[day.weekday()] in hours.days:
            opens = datetime.combine(day, hours.start, tzinfo=zone).astimezone(UTC)
            closes = datetime.combine(day, hours.end, tzinfo=zone).astimezone(UTC)
            low, high = max(opens, start_utc), min(closes, end_utc)
            if high > low:
                total += _minutes(low, high)
        day += timedelta(days=1)
    return total
