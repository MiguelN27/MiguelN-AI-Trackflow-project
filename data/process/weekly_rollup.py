"""The Weekly Warehouse & Client Performance rollup, as pure Pandas functions.

This module turns the four mandatory inventory events into one row per
`warehouse`, `client_id` and ISO week (`PIPELINE_DESIGN.md` 2.7). It is shared
by the Prefect flow, the backfill flow and the tests, and follows the same
rules as `services/telemetry/analysis.py`:

- No I/O, no clock, no randomness. The caller passes the weeks and the data, so
  the same input always gives the same output, which the idempotent load
  relies on to detect unchanged rows.
- Everything is UTC. `week_start` is the Monday of the event's UTC week.
- Nothing is dropped silently. Every event left out is returned with its reason.

Event time is trusted only up to arrival: `effective_time = min(timestamp,
created_at)`, so a client clock running ahead cannot move an event into a week
that had not started when it arrived.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Final, cast

import numpy as np
import pandas as pd

INBOUND: Final = "inbound_order_created"
OUTBOUND: Final = "outbound_order_created"
THRESHOLD: Final = "stock_threshold_triggered"
DISCREPANCY: Final = "inventory_discrepancy_detected"
EVENT_TYPES: Final = (INBOUND, OUTBOUND, THRESHOLD, DISCREPANCY)

# `services/inventory/models.py::Warehouse` and the `warehouse` enum in
# docs/telemetry/event-schemas.json. BRIEFING's `los_angeles` is a template value.
WAREHOUSES: Final = ("Monterrey", "Zaragoza")
THRESHOLD_BANDS: Final = ("low", "out_of_stock")

# A lowercase slug assigned by Commercial and Client Relations (design 2.4).
CLIENT_ID_PATTERN: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"
CLIENT_ID_MAX_LENGTH: Final = 64

# Rates are rounded here, not in the database, so a re-run produces the same
# value and the upsert sees the row as unchanged.
RATE_DECIMALS: Final = 4

MAX_LOOKBACK_WEEKS: Final = 12

# What extraction hands over: one row per event, payload fields still as text.
EVENT_COLUMNS: Final = (
    "id",
    "timestamp",
    "created_at",
    "event_type",
    "warehouse",
    "client_id",
    "product_id",
    "quantity",
    "order_id",
    "triggering_order_id",
    "threshold_band",
    "reconciliation_id",
    "schema_version",
)
TEXT_COLUMNS: Final = tuple(
    name for name in EVENT_COLUMNS if name not in ("timestamp", "created_at")
)
PRODUCT_COLUMNS: Final = ("product_id", "client_id", "warehouse")

KEY: Final = ["warehouse", "client_id", "week_start"]
FIGURES: Final = (
    "inbound_units_count",
    "outbound_orders_count",
    "stockout_events_count",
    "discrepancy_events_count",
    "discrepancy_rate",
)
COUNT_COLUMNS: Final = FIGURES[:4]
ROW_COLUMNS: Final = (*KEY, *FIGURES)

# The keys of `pipeline_runs.events_excluded`, in the order they are checked.
# An event gets the first reason that applies.
EXCLUSION_REASONS: Final = (
    "unknown_warehouse",
    "invalid_quantity",
    "missing_client_id",
    "unknown_threshold_band",
    "outside_window",
    "duplicate_business_key",
)
EXCLUSION_COLUMNS: Final = ("event_id", "reason")


class InvalidWeek(ValueError):
    """A requested week is not a Monday, not closed yet, or the range is unusable."""


class RollupValidationError(ValueError):
    """The rollup breaks a rule the destination table relies on. Nothing is loaded.

    The message names the rule and how many rows break it, never their values.
    """


@dataclass(frozen=True)
class Rollup:
    """The output of the transform: the destination rows and what was left out."""

    rows: pd.DataFrame
    exclusions: pd.DataFrame
    events_read: int
    events_excluded: dict[str, int] = field(default_factory=dict)


# --- Weeks ---------------------------------------------------------------------------


def week_start_of_day(day: date) -> date:
    """The Monday of the ISO week containing `day`."""
    return day - timedelta(days=day.weekday())


def week_start_of(moment: datetime) -> date:
    """The Monday of the UTC ISO week containing `moment`."""
    return week_start_of_day(moment.astimezone(timezone.utc).date())


def last_closed_week(now: datetime) -> date:
    """The most recent week that has ended: the Monday before this week's Monday."""
    return week_start_of(now) - timedelta(weeks=1)


def closed_weeks(
    now: datetime, week_start: date | None = None, lookback_weeks: int = 2
) -> tuple[date, ...]:
    """The weeks a run recomputes, oldest first.

    With `week_start`, that one week, which must be a closed Monday. Without it,
    the last `lookback_weeks` closed weeks. The week in progress is never
    computed: half a week reads as a bad week to a non-technical reader.
    """
    newest = last_closed_week(now)
    if week_start is not None:
        if week_start.weekday() != 0:
            raise InvalidWeek("week_start must be a Monday")
        if week_start > newest:
            raise InvalidWeek("week_start must be a closed week")
        return (week_start,)
    if not 1 <= lookback_weeks <= MAX_LOOKBACK_WEEKS:
        raise InvalidWeek(f"lookback_weeks must be between 1 and {MAX_LOOKBACK_WEEKS}")
    return tuple(newest - timedelta(weeks=back) for back in reversed(range(lookback_weeks)))


def weeks_between(from_week: date, to_week: date) -> tuple[date, ...]:
    """Every Monday from `from_week` to `to_week`, both included, for a backfill."""
    if from_week.weekday() != 0 or to_week.weekday() != 0:
        raise InvalidWeek("from_week and to_week must be Mondays")
    if from_week > to_week:
        raise InvalidWeek("from_week must not be after to_week")
    count = (to_week - from_week).days // 7 + 1
    return tuple(from_week + timedelta(weeks=offset) for offset in range(count))


def window_bounds(weeks: tuple[date, ...]) -> tuple[datetime, datetime]:
    """`[Monday 00:00 UTC of the oldest week, Monday 00:00 UTC after the newest)`."""
    start = datetime.combine(min(weeks), time.min, tzinfo=timezone.utc)
    end = datetime.combine(max(weeks) + timedelta(weeks=1), time.min, tzinfo=timezone.utc)
    return start, end


# --- Transform -----------------------------------------------------------------------


def empty_events() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype="object") for name in EVENT_COLUMNS})


def empty_rows() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype="object") for name in ROW_COLUMNS})


def empty_exclusions() -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype="string") for name in EXCLUSION_COLUMNS})


def _valid_client_id(values: pd.Series) -> pd.Series:
    return values.str.fullmatch(CLIENT_ID_PATTERN).fillna(False).astype(bool) & (
        values.str.len() <= CLIENT_ID_MAX_LENGTH
    ).fillna(False).astype(bool)


def normalize_events(events: pd.DataFrame) -> pd.DataFrame:
    """Type the extracted events and assign each one to its UTC week.

    Adds `effective_time`, `week_start` (a `date`) and `quantity_value`
    (nullable integer: missing when `quantity` is absent or not a whole number).
    Text fields become the `string` dtype, so a missing value is always `<NA>`.
    """
    frame = events.loc[:, list(EVENT_COLUMNS)].copy()
    for name in TEXT_COLUMNS:
        text = frame[name].astype("string").str.strip()
        frame[name] = text.mask((text == "").fillna(False).astype(bool))
    frame["product_id"] = frame["product_id"].str.lower()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True)
    frame["effective_time"] = frame["timestamp"].where(
        frame["timestamp"] <= frame["created_at"], frame["created_at"]
    )
    day = frame["effective_time"].dt.floor("D")
    monday = day - pd.to_timedelta(day.dt.weekday, unit="D")
    frame["week_start"] = pd.Series(monday.dt.date, index=frame.index, dtype="object")

    whole = frame["quantity"].str.fullmatch(r"-?\d{1,9}").fillna(False).astype(bool)
    frame["quantity_value"] = pd.to_numeric(frame["quantity"].where(whole), errors="coerce").astype(
        "Int64"
    )
    return frame


def resolve_client(events: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Set `client_id`: the payload's when valid, else the owner of `product_id`.

    An event that neither gives keeps `<NA>` and is excluded later as
    `missing_client_id`. There is no "unassigned" bucket: it would mix goods of
    different clients in one row, which BRIEFING section 7 forbids.
    """
    owners = products.loc[:, list(PRODUCT_COLUMNS)].astype("string")
    owners["product_id"] = owners["product_id"].str.lower()
    owners = owners[_valid_client_id(owners["client_id"])]
    owner_of = owners.drop_duplicates("product_id").set_index("product_id")["client_id"]

    resolved = events.copy()
    from_payload = resolved["client_id"].where(_valid_client_id(resolved["client_id"]))
    from_product = resolved["product_id"].map(owner_of).astype("string")
    resolved["client_id"] = from_payload.fillna(from_product)
    return resolved


def exclusion_reasons(events: pd.DataFrame, weeks: tuple[date, ...]) -> pd.Series:
    """The first reason each event cannot be counted, or `<NA>` when it can.

    Ingest does not validate per-event properties (progress 2026-10-19), so
    this is the pipeline's own defence. Only inbound events need a quantity:
    Outbound Throughput counts orders, not units.
    """
    event_type = events["event_type"]
    quantity_ok = (events["quantity_value"] >= 1).fillna(False).astype(bool)
    checks = (
        ("unknown_warehouse", ~events["warehouse"].isin(WAREHOUSES).fillna(False)),
        ("invalid_quantity", (event_type == INBOUND).fillna(False) & ~quantity_ok),
        ("missing_client_id", events["client_id"].isna()),
        (
            "unknown_threshold_band",
            (event_type == THRESHOLD).fillna(False)
            & ~events["threshold_band"].isin(THRESHOLD_BANDS).fillna(False),
        ),
        ("outside_window", ~events["week_start"].isin(weeks)),
    )
    reason = pd.Series(pd.NA, index=events.index, dtype="string")
    for name, failed in checks:
        reason = reason.mask(reason.isna() & failed.astype(bool), name)
    return reason


def business_key(events: pd.DataFrame) -> pd.Series:
    """The key two reports of one business fact share (design 2.7 step 4).

    `eventId` already deduplicates retries at ingest. This catches the same
    action reported under two `eventId`s, for example by two emitters. Without
    the business id the key falls back to `eventId`, which never collides.
    """
    event_type = events["event_type"]
    by_event = "event:" + events["id"]
    by_order = "order:" + event_type + ":" + events["order_id"]
    by_trigger = "threshold:" + events["triggering_order_id"] + ":" + events["threshold_band"]
    by_count = "discrepancy:" + events["reconciliation_id"] + ":" + events["product_id"]

    def of_type(*names: str) -> pd.Series:
        return event_type.isin(names).fillna(False).astype(bool)

    key = by_event.copy()
    key = key.mask(of_type(INBOUND, OUTBOUND) & by_order.notna(), by_order)
    key = key.mask(of_type(THRESHOLD) & by_trigger.notna(), by_trigger)
    key = key.mask(of_type(DISCREPANCY) & by_count.notna(), by_count)
    return key


def duplicate_mask(events: pd.DataFrame) -> pd.Series:
    """True for every event a business-key duplicate supersedes.

    Orders and threshold crossings keep the first report. A discrepancy keeps
    the latest, because a re-count publishes the corrected figure. Ties are
    broken by arrival time and then `eventId`, so the choice is deterministic.
    """
    ordered = events.sort_values(["timestamp", "created_at", "id"], kind="stable")
    key = business_key(ordered)
    keep_latest = (ordered["event_type"] == DISCREPANCY).to_numpy(dtype=bool, na_value=False)
    duplicated = np.where(keep_latest, key.duplicated(keep="last"), key.duplicated(keep="first"))
    return pd.Series(duplicated, index=ordered.index).reindex(events.index)


def aggregate(
    counted: pd.DataFrame, products: pd.DataFrame, weeks: tuple[date, ...]
) -> pd.DataFrame:
    """One row per `(warehouse, client_id, week_start)` with the four KPIs.

    Every pair that owns products gets a row for every computed week, with
    zeros when nothing happened, so a missing row means only that the client
    has no products in that warehouse (design 2.7 step 7).
    """
    # Plain object keys on both sides, so the event keys and the zero-row grid
    # union into one index instead of two that only look alike.
    keyed = counted.astype({"warehouse": "object", "client_id": "object"})
    is_type = keyed["event_type"]
    figures = pd.concat(
        [
            keyed[is_type == INBOUND].groupby(KEY)["quantity_value"].sum().astype("int64"),
            keyed[is_type == OUTBOUND].groupby(KEY).size(),
            keyed[is_type == THRESHOLD].groupby(KEY).size(),
            keyed[is_type == DISCREPANCY].groupby(KEY).size(),
        ],
        axis=1,
        keys=list(COUNT_COLUMNS),
    )

    owners = products.loc[:, ["warehouse", "client_id"]].astype("string")
    owners = owners[owners["warehouse"].isin(WAREHOUSES) & _valid_client_id(owners["client_id"])]
    every_week = pd.DataFrame({"week_start": pd.Series(weeks, dtype="object")})
    grid = pd.MultiIndex.from_frame(
        owners.drop_duplicates().astype("object").merge(every_week, how="cross").loc[:, KEY]
    )

    if len(figures):
        keys = grid.union(cast(pd.MultiIndex, figures.index))
        rows = figures.reindex(keys).fillna(0).astype("int64")
    elif len(grid):
        rows = pd.DataFrame(0, index=grid, columns=list(COUNT_COLUMNS), dtype="int64")
    else:
        return empty_rows()

    rows = rows.rename_axis(KEY).reset_index()
    orders = rows["outbound_orders_count"]
    rate = (rows["discrepancy_events_count"] / orders.where(orders > 0)).round(RATE_DECIMALS)
    rows["discrepancy_rate"] = rate.fillna(0.0).astype("float64")
    rows = rows.astype({"warehouse": "object", "client_id": "object", "week_start": "object"})
    return rows.sort_values(["week_start", "warehouse", "client_id"], ignore_index=True).loc[
        :, list(ROW_COLUMNS)
    ]


def build_rollup(events: pd.DataFrame, products: pd.DataFrame, weeks: tuple[date, ...]) -> Rollup:
    """Normalize, resolve clients, exclude, deduplicate and aggregate.

    `events` has the `EVENT_COLUMNS` of extraction; `products` has
    `PRODUCT_COLUMNS`. Returns exactly the destination columns, sorted by
    `week_start, warehouse, client_id`, plus every excluded `eventId` and why.
    """
    normalized = resolve_client(normalize_events(events), products)
    reason = exclusion_reasons(normalized, weeks)
    valid = normalized[reason.isna()]
    duplicated = duplicate_mask(valid)
    reason.loc[duplicated[duplicated].index] = "duplicate_business_key"
    counted = normalized[reason.isna()]

    excluded = reason.dropna()
    exclusions = pd.DataFrame(
        {
            "event_id": normalized.loc[excluded.index, "id"].astype("string"),
            "reason": excluded.astype("string"),
        }
    ).reset_index(drop=True)
    tally = excluded.value_counts()
    return Rollup(
        rows=aggregate(counted, products, weeks),
        exclusions=exclusions if len(exclusions) else empty_exclusions(),
        events_read=len(normalized),
        events_excluded={name: int(tally.get(name, 0)) for name in EXCLUSION_REASONS},
    )


# --- Validation (the gate before the load) -------------------------------------------


def validate(rows: pd.DataFrame, weeks: tuple[date, ...], newest_closed: date) -> pd.DataFrame:
    """Refuse a rollup the destination must never hold (design 3.5).

    The table's constraints repeat most of these checks; failing here gives a
    readable reason before anything is written. Returns `rows` unchanged.
    """
    if tuple(rows.columns) != ROW_COLUMNS:
        raise RollupValidationError("The rollup does not have exactly the destination columns")
    if rows.empty:
        return rows

    problems: list[str] = []

    def check(broken: pd.Series, rule: str) -> None:
        count = int(broken.sum())
        if count:
            problems.append(f"{count} row(s) {rule}")

    week = rows["week_start"]
    check(rows.duplicated(KEY, keep=False), "repeat a (warehouse, client_id, week_start) key")
    check(week.map(lambda value: not isinstance(value, date)), "have no week_start date")
    dated = week[week.map(lambda value: isinstance(value, date))]
    check(dated.map(lambda value: value.weekday() != 0), "have a week_start that is not a Monday")
    check(~dated.isin(weeks), "fall outside the weeks this run computes")
    check(dated.map(lambda value: value > newest_closed), "belong to a week that has not closed")
    check(~rows["warehouse"].isin(WAREHOUSES), "name a warehouse outside Monterrey/Zaragoza")
    check(~_valid_client_id(rows["client_id"].astype("string")), "have an invalid client_id")

    counts = rows.loc[:, list(COUNT_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    check(counts.isna().any(axis=1), "have a missing count")
    check((counts < 0).any(axis=1), "have a negative count")
    check((counts % 1 != 0).any(axis=1), "have a count that is not a whole number")

    rate = pd.to_numeric(rows["discrepancy_rate"], errors="coerce")
    orders = counts["outbound_orders_count"]
    expected = (
        (counts["discrepancy_events_count"] / orders.where(orders > 0))
        .round(RATE_DECIMALS)
        .fillna(0.0)
    )
    check(rate.isna() | ((rate - expected).abs() > 1e-9), "have a rate that disagrees with counts")

    if problems:
        raise RollupValidationError("Rollup refused: " + "; ".join(problems))
    return rows
