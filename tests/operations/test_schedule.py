"""The monitor's clock: only working time counts, in the location's own timezone."""

from services.operations.rules import OperatingHours
from services.operations.schedule import active_minutes_between
from tests.operations.support import at

MADRID_WEEKDAYS = OperatingHours.model_validate(
    {
        "timezone": "Europe/Madrid",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "08:00",
        "end": "20:00",
    }
)
MONTERREY = OperatingHours.model_validate(
    {"timezone": "America/Monterrey", "start": "08:00", "end": "20:00"}
)


def test_without_operating_hours_every_minute_counts():
    assert active_minutes_between(at("2026-11-27 10:00"), at("2026-11-27 12:30"), None) == 150
    assert active_minutes_between(at("2026-11-27 12:30"), at("2026-11-27 10:00"), None) == 0


def test_nights_and_weekends_do_not_count():
    # Friday 18:00 Madrid (17:00 UTC) to Monday 09:30 Madrid (08:30 UTC):
    # Friday 18:00-20:00 (120) + Monday 08:00-09:30 (90). The weekend and nights: 0.
    assert (
        active_minutes_between(at("2026-11-27 17:00"), at("2026-11-30 08:30"), MADRID_WEEKDAYS)
        == 210
    )
    # A whole Saturday in Zaragoza is no working time at all.
    assert (
        active_minutes_between(at("2026-11-28 08:00"), at("2026-11-28 20:00"), MADRID_WEEKDAYS) == 0
    )


def test_the_same_instants_count_differently_in_monterrey_and_madrid():
    # 14:00-16:00 UTC is 15:00-17:00 in Madrid (working) but 08:00-10:00 in Monterrey (working)...
    start, end = at("2026-11-26 14:00"), at("2026-11-26 16:00")
    assert active_minutes_between(start, end, MADRID_WEEKDAYS) == 120
    assert active_minutes_between(start, end, MONTERREY) == 120
    # ...while 20:00-22:00 UTC is after hours in Madrid and the middle of the day in Monterrey.
    start, end = at("2026-11-26 20:00"), at("2026-11-26 22:00")
    assert active_minutes_between(start, end, MADRID_WEEKDAYS) == 0
    assert active_minutes_between(start, end, MONTERREY) == 120


def test_a_daylight_saving_day_is_still_twelve_working_hours():
    # Sunday 2026-10-25: Madrid falls back an hour at 03:00. 08:00-20:00 that day is
    # still 12 hours of real time; counted every day of the week here.
    every_day = OperatingHours.model_validate(
        {"timezone": "Europe/Madrid", "start": "08:00", "end": "20:00"}
    )
    assert active_minutes_between(at("2026-10-24 22:00"), at("2026-10-25 22:00"), every_day) == 720
    # Across the change, from Saturday 12:00 to Sunday 12:00 local: 8 h + 4 h.
    assert active_minutes_between(at("2026-10-24 10:00"), at("2026-10-25 11:00"), every_day) == 720
