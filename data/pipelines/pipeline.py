"""Weekly Warehouse & Client Performance pipeline: the Prefect flows and tasks.

Reads the four mandatory inventory events from `telemetry_events` (read-only),
rolls them up per warehouse, client and ISO week, and publishes the result to
`reporting.weekly_warehouse_client_performance`. The design, and why each
choice below was made, is in `data/pipelines/PIPELINE_DESIGN.md`.

Run from the repository root:

    python data/pipelines/pipeline.py                          # the last 2 closed weeks, once
    python data/pipelines/pipeline.py --week-start 2026-10-12  # one closed week
    python data/pipelines/pipeline.py --backfill 2026-09-28 2026-10-12
    python data/pipelines/pipeline.py --serve                  # Monday 06:00 America/Monterrey

(`uv run python ...` when the virtual environment is not activated.)

Stages, each an independent task with explicit inputs and outputs:

    open_run -> extract_source_events, extract_product_clients, extract_inventory_order_counts
             -> transform_weekly_rollup -> compute_collection_health (non-critical)
             -> validate_weekly_rows -> load_weekly_rows -> write_eval_snapshot (non-critical)
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final, cast
from uuid import UUID

import pandas as pd
from prefect import Flow, flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import (
    ConcurrencyLimitConfig,
    ConcurrencyLimitStrategy,
    FlowRun,
)
from prefect.context import TaskRunContext
from prefect.runtime import flow_run as current_flow_run
from prefect.schedules import Cron
from prefect.states import Cancelled, Completed, State
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from data.pipelines import reporting_db, run_log
from data.pipelines.weekly_warehouse_client_performance import extract, load, schema
from data.pipelines.weekly_warehouse_client_performance import trigger as deployment
from data.process import collection_health, weekly_rollup

PIPELINE_NAME: Final = schema.PIPELINE_NAME
DATA_DIR: Final = Path(__file__).resolve().parents[1]
ROOT_DIR: Final = DATA_DIR.parent

# Intermediate files: the persisted results behind the transform cache. They
# hold counts and excluded eventIds, never payloads or user ids. Git-ignored.
RESULTS_DIR: Final = DATA_DIR / "raw" / "prefect-results"
# Pipeline validation outputs, one JSON file per completed run. Git-ignored.
EVAL_DIR: Final = DATA_DIR / "eval" / PIPELINE_NAME
# Runs that `reporting.pipeline_runs` could not record: the database was
# unreachable, or the run failed before its row existed. One JSON line each,
# inside EVAL_DIR (git-ignored).
RUN_LOG_FALLBACK_FILE: Final = "run_log_fallback.jsonl"

# The reporting cycle (design 4.4): the ISO week closes Monday 00:00 UTC
# (Sunday 18:00 in Monterrey); 06:00 Monterrey leaves an hour before the 07:00
# Monday report leadership expects, and 12 hours for late frontend batches.
SCHEDULE_CRON: Final = "0 6 * * 1"
SCHEDULE_TIMEZONE: Final = "America/Monterrey"
DEFAULT_LOOKBACK_WEEKS: Final = 2

TASK_TIMEOUT_SECONDS: Final = 300

# Each run with the same input computes the same rollup, so a result younger
# than this is reused instead of recomputed (see `rollup_cache_key`).
CACHE_EXPIRATION: Final = timedelta(hours=1)

STAGE_MESSAGES: Final = {
    "extract": "Reading the source tables failed",
    "transform": "Computing the weekly rollup failed",
    "validate": "The rollup failed validation",
    "load": "Publishing the weekly rows failed",
}

logger = logging.getLogger("pipelines.weekly_warehouse_client_performance")


class PipelineStageError(RuntimeError):
    """A stage failed. Carries what the run log stores: the stage, the class, a safe message.

    Only the exception class and a fixed message per stage are recorded,
    because a driver message can quote the connection URL. The traceback stays
    in Prefect's logs. Validation messages are written by the pipeline itself
    and name rules and counts, not values, so they are kept.
    """

    def __init__(
        self,
        stage: run_log.ErrorStage,
        error_class: str,
        message: str,
        events_read: int | None = None,
    ) -> None:
        super().__init__(stage, error_class, message, events_read)
        self.stage = stage
        self.error_class = error_class
        self.message = message
        # How many events the run had read when it failed: None before extraction.
        self.events_read = events_read

    def __str__(self) -> str:
        return f"{self.stage}: {self.error_class}: {self.message}"


# --- Helpers -------------------------------------------------------------------------


@lru_cache(maxsize=1)
def code_version() -> str | None:
    """The git commit of the code that produced a run (`--dirty` when uncommitted)."""
    configured = os.environ.get("TRACKFLOW_CODE_VERSION")
    if configured:
        return configured
    try:
        described = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=12"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return described.stdout.strip() or None


def _source_digest(*modules: ModuleType) -> str:
    digest = hashlib.sha256()
    for module in modules:
        digest.update(Path(str(module.__file__)).read_bytes())
    return digest.hexdigest()


# Computed once per process: a code change to a transform invalidates its cache.
ROLLUP_SOURCE: Final = _source_digest(weekly_rollup)
HEALTH_SOURCE: Final = _source_digest(weekly_rollup, collection_health)


def frame_digest(frame: pd.DataFrame) -> str:
    """A content hash of a DataFrame: its column names and every cell, in order."""
    digest = hashlib.sha256("\x1f".join(map(str, frame.columns)).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes())
    return digest.hexdigest()


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1e".join(parts).encode()).hexdigest()


def rollup_cache_key(_context: TaskRunContext, parameters: dict[str, Any]) -> str:
    """Cache key of `transform_weekly_rollup`: everything its output depends on.

    The transform is a pure function, so its output is fully determined by
    (1) the source of `data/process/weekly_rollup.py`, (2) the weeks, (3) the
    extracted events and (4) the product owners, each hashed by content. Two
    runs within `CACHE_EXPIRATION` (1 hour) that read the same events reuse the
    stored rollup instead of recomputing it; one new event, a reassigned
    product or an edit to the transform gives a new key. Neither the run id
    nor the clock is part of the key: they do not change the result.
    """
    return "wwcp-rollup-" + _key(
        ROLLUP_SOURCE,
        ",".join(week.isoformat() for week in parameters["weeks"]),
        frame_digest(parameters["events"]),
        frame_digest(parameters["products"]),
    )


def health_cache_key(_context: TaskRunContext, parameters: dict[str, Any]) -> str:
    """Cache key of `compute_collection_health`, by the same rule: code plus inputs by content."""
    return "wwcp-health-" + _key(
        HEALTH_SOURCE,
        ",".join(week.isoformat() for week in parameters["weeks"]),
        frame_digest(parameters["events"]),
        frame_digest(parameters["exclusions"]),
        frame_digest(parameters["order_counts"]),
    )


TRANSIENT_ERRORS: Final = (OperationalError, InterfaceError, ConnectionError, TimeoutError)


def retry_on_transient_error(_task: Any, _task_run: Any, state: State) -> bool:
    """Retry only what a retry can fix: a lost or refused connection, a timeout.

    A `ProgrammingError` (a missing column, a typo in SQL) or an integrity
    violation fails the same way every time, so retrying it only delays the
    alert.
    """
    try:
        error = state.result(raise_on_failure=False)
    except Exception:  # noqa: BLE001 - an unreadable state is not evidence against retrying
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    return isinstance(error, TRANSIENT_ERRORS)


def flow_run_is_terminal(flow_run_id: UUID) -> bool | None:
    """Whether Prefect considers that flow run finished. None when Prefect cannot say."""
    try:
        with get_client(sync_client=True) as client:
            state = client.read_flow_run(flow_run_id).state
    except Exception:  # noqa: BLE001 - unreachable API or unknown run: "cannot say"
        return None
    return None if state is None else state.is_final()


def _current_flow_run_id() -> UUID:
    return UUID(str(current_flow_run.id))


# --- Tasks ---------------------------------------------------------------------------
#
# Retries: every task that talks to PostgreSQL retries transient errors only
# (`retry_on_transient_error`). The Supabase pooler has been unreachable before
# (2026-10-17 -> 2026-10-19), and a dropped connection usually comes back within
# seconds to a couple of minutes. Pure transforms never retry: the same input
# fails the same way. Flow-level retries (3 x 15 min) ride out longer outages.


# 2 retries, 5 s then 30 s. The step is idempotent per flow run id, so a retry
# whose first attempt did commit finds its own row and resumes it.
@task(
    retries=2,
    retry_delay_seconds=[5, 30],
    retry_condition_fn=retry_on_transient_error,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
    cache_policy=NO_CACHE,
)
def open_run(
    weeks: tuple[date, ...], trigger: run_log.Trigger, triggered_by: UUID | None
) -> run_log.RunHandle:
    """Create the reporting schema if needed, then open or resume this run's row."""
    engine = reporting_db.get_pipeline_engine()
    schema.ensure_schema(engine)
    return run_log.open_run(
        engine,
        flow_run_id=_current_flow_run_id(),
        trigger=trigger,
        triggered_by=triggered_by,
        weeks_from=min(weeks),
        weeks_to=max(weeks),
        code_version=code_version(),
        flow_run_is_terminal=flow_run_is_terminal,
    )


# 3 retries, 10 s / 30 s / 90 s: about two minutes of outage tolerated per read.
# The read is bounded by the run's arrival cutoff, so a retry reads exactly the
# same snapshot. Event rows are not persisted as Prefect results.
@task(
    retries=3,
    retry_delay_seconds=[10, 30, 90],
    retry_condition_fn=retry_on_transient_error,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
    persist_result=False,
    cache_policy=NO_CACHE,
)
def extract_source_events(
    window_start: datetime,
    window_end: datetime,
    arrival_cutoff: datetime,
    previous_cutoff: datetime | None,
) -> extract.SourceExtract:
    """The four mandatory event types of the window, read-only, as one snapshot."""
    return extract.read_source_events(
        reporting_db.get_pipeline_engine(),
        window_start=window_start,
        window_end=window_end,
        arrival_cutoff=arrival_cutoff,
        previous_cutoff=previous_cutoff,
    )


# Same retry policy as the event read: same database, same failure modes.
@task(
    retries=3,
    retry_delay_seconds=[10, 30, 90],
    retry_condition_fn=retry_on_transient_error,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
    persist_result=False,
    cache_policy=NO_CACHE,
)
def extract_product_clients() -> pd.DataFrame:
    """`product_id, client_id, warehouse` for every product, read-only."""
    return extract.read_product_clients(reporting_db.get_pipeline_engine())


# Same retry policy as the event read.
@task(
    retries=3,
    retry_delay_seconds=[10, 30, 90],
    retry_condition_fn=retry_on_transient_error,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
    persist_result=False,
    cache_policy=NO_CACHE,
)
def extract_inventory_order_counts(window_start: datetime, window_end: datetime) -> pd.DataFrame:
    """Inventory orders per warehouse and day: the independent count for coverage."""
    return extract.read_inventory_order_counts(
        reporting_db.get_pipeline_engine(), window_start=window_start, window_end=window_end
    )


@task(
    cache_key_fn=rollup_cache_key,
    cache_expiration=CACHE_EXPIRATION,
    persist_result=True,
    result_storage=RESULTS_DIR,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
)
def transform_weekly_rollup(
    events: pd.DataFrame, products: pd.DataFrame, weeks: tuple[date, ...]
) -> weekly_rollup.Rollup:
    """Normalize, resolve clients, exclude, deduplicate and aggregate. No I/O, no retries."""
    return weekly_rollup.build_rollup(events, products, weeks)


@task(
    cache_key_fn=health_cache_key,
    cache_expiration=CACHE_EXPIRATION,
    persist_result=True,
    result_storage=RESULTS_DIR,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
)
def compute_collection_health(
    events: pd.DataFrame,
    exclusions: pd.DataFrame,
    order_counts: pd.DataFrame,
    weeks: tuple[date, ...],
) -> collection_health.CollectionHealth:
    """Coverage, lag, gaps, bursts and clock skew. Observability, not the KPIs."""
    return collection_health.compute(events, exclusions, order_counts, weeks)


@task(cache_policy=NO_CACHE, timeout_seconds=TASK_TIMEOUT_SECONDS)
def validate_weekly_rows(
    rows: pd.DataFrame, weeks: tuple[date, ...], newest_closed_week: date
) -> pd.DataFrame:
    """The gate before the load: refuse rows the destination must never hold."""
    return weekly_rollup.validate(rows, weeks, newest_closed_week)


# 2 retries, 15 s then 60 s. The load is one transaction: a failed attempt
# leaves nothing behind, and a retry writes the same rows again.
@task(
    retries=2,
    retry_delay_seconds=[15, 60],
    retry_condition_fn=retry_on_transient_error,
    timeout_seconds=TASK_TIMEOUT_SECONDS,
    cache_policy=NO_CACHE,
)
def load_weekly_rows(
    run: run_log.RunHandle,
    weeks: tuple[date, ...],
    rows: pd.DataFrame,
    rollup: weekly_rollup.Rollup,
    health: collection_health.CollectionHealth | None,
    source: extract.SourceExtract,
) -> load.LoadResult:
    """Publish the weeks and mark the run completed, in one transaction."""
    return load.load_weekly_rows(
        reporting_db.get_pipeline_engine(),
        run_id=run.run_id,
        arrival_cutoff=run.arrival_cutoff,
        weeks=weeks,
        rows=rows,
        exclusions=rollup.exclusions,
        health=health,
        run_fields={
            "events_read": rollup.events_read,
            "events_excluded": rollup.events_excluded,
            "source_watermark": source.source_watermark,
            "late_events_outside_window": source.late_events_outside_window,
            "telemetry_dropped": source.telemetry_dropped,
        },
    )


@task(cache_policy=NO_CACHE, timeout_seconds=60)
def write_eval_snapshot(
    run: run_log.RunHandle,
    weeks: tuple[date, ...],
    rows: pd.DataFrame,
    rollup: weekly_rollup.Rollup,
    health: collection_health.CollectionHealth | None,
    loaded: load.LoadResult,
    output_dir: Path,
) -> str:
    """Write the run's validation summary to `data/eval/`. Non-critical.

    Totals per week and warehouse, the exclusions, the validation outcome, the
    load counts and the collection health: what a reviewer needs to check a
    run without database access. No per-client figures (they are commercially
    sensitive) and no event payloads.
    """
    totals = (
        rows.groupby(["week_start", "warehouse"], as_index=False)[
            list(weekly_rollup.COUNT_COLUMNS)
        ].sum()
        if len(rows)
        else pd.DataFrame(columns=["week_start", "warehouse", *weekly_rollup.COUNT_COLUMNS])
    )
    snapshot = {
        "pipeline_name": PIPELINE_NAME,
        "run_id": str(run.run_id),
        "arrival_cutoff": run.arrival_cutoff.astimezone(timezone.utc).isoformat(),
        "code_version": code_version(),
        "weeks": [week.isoformat() for week in weeks],
        "events_read": rollup.events_read,
        "events_excluded": rollup.events_excluded,
        "validation": {"passed": True, "rows": len(rows)},
        "totals_by_week_and_warehouse": [
            {
                "week_start": total["week_start"].isoformat(),
                "warehouse": total["warehouse"],
                **{name: int(total[name]) for name in weekly_rollup.COUNT_COLUMNS},
            }
            for total in totals.to_dict("records")
        ],
        "load": {**asdict(loaded), "weeks_revised": [w.isoformat() for w in loaded.weeks_revised]},
        "collection_health": None
        if health is None
        else {
            "quality_status": health.quality_status,
            "min_capture_coverage": health.min_capture_coverage,
            "stats_rows": len(health.stats),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    cutoff = run.arrival_cutoff.astimezone(timezone.utc)
    path = output_dir / f"{cutoff:%Y%m%dT%H%M%SZ}_{run.run_id}.json"
    partial = path.with_suffix(".json.partial")
    partial.write_text(json.dumps(snapshot, indent=2, default=str) + "\n")
    partial.replace(path)
    return str(path)


# --- Flow state hooks: the run log's record of an unsuccessful end --------------------


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def _write_fallback_record(record: dict[str, Any]) -> None:
    """Append one run to `RUN_LOG_FALLBACK_FILE`. Never raises: it runs inside a hook."""
    try:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        with (EVAL_DIR / RUN_LOG_FALLBACK_FILE).open("a", encoding="utf-8") as fallback:
            fallback.write(json.dumps(record, default=_iso) + "\n")
    except OSError as unwritable:
        logger.error("Could not record the run anywhere: %s", type(unwritable).__name__)


def _record_unsuccessful_end(
    flow_run: FlowRun, state: State[Any], fallback_class: str | None
) -> None:
    """Record a failed, crashed or cancelled run: in the run log, or else in a log file.

    The run log is the record. When it cannot take this one - the database is
    unreachable, or the run failed before `open_run` wrote its row - the same
    minimum metadata (start, end, records processed, status, error) goes to
    `data/eval/.../run_log_fallback.jsonl`, so no run ends without a record.
    A run that skipped itself already has its row, and gets no fallback line.
    """
    try:
        error = state.result(raise_on_failure=False)
    except Exception as unreadable:  # noqa: BLE001 - a crashed state may not hold a result
        error = unreadable
    events_read: int | None = None
    if isinstance(error, PipelineStageError):
        stage, error_class, message = error.stage, error.error_class, error.message
        events_read = error.events_read
    else:
        stage = None
        error_class = fallback_class or (
            type(error).__name__ if isinstance(error, BaseException) else str(state.name)
        )
        message = "The flow run ended before recording an outcome"

    reason: str | None = None
    try:
        outcome = run_log.fail_run(
            reporting_db.get_pipeline_engine(),
            flow_run_id=UUID(str(flow_run.id)),
            error_stage=stage,
            error_class=error_class,
            error_message=message,
            events_read=events_read,
        )
        if outcome == "missing":
            reason = "no run row"
    except Exception as unreachable:  # noqa: BLE001 - recorded in the fallback file instead
        logger.warning("Could not record the run outcome: %s", type(unreachable).__name__)
        reason = "run log unreachable"
    if reason is None:
        return

    _write_fallback_record(
        {
            "pipeline_name": PIPELINE_NAME,
            "flow_run_id": str(flow_run.id),
            "trigger": (flow_run.parameters or {}).get("trigger"),
            "started_at": flow_run.start_time,
            "finished_at": datetime.now(timezone.utc),
            "events_read": events_read,
            "status": "failed",
            "error_stage": stage,
            "error_class": error_class,
            "error_message": message,
            "recorded_here_because": reason,
        }
    )


def mark_run_failed(flow: Flow[..., Any], flow_run: FlowRun, state: State[Any]) -> None:
    """`on_failure`: retries are exhausted. Record the stage and class of the failure."""
    _record_unsuccessful_end(flow_run, state, None)


def mark_run_crashed(flow: Flow[..., Any], flow_run: FlowRun, state: State[Any]) -> None:
    """`on_crashed`: the worker or process died. If this hook cannot run, the sweep does it."""
    _record_unsuccessful_end(flow_run, state, "Crashed")


def mark_run_cancelled(flow: Flow[..., Any], flow_run: FlowRun, state: State[Any]) -> None:
    """`on_cancellation`: an operator cancelled the run. A self-skipped run is not running."""
    _record_unsuccessful_end(flow_run, state, "Cancelled")


# --- Flows ---------------------------------------------------------------------------


# Flow retries: 3, 15 minutes apart, so the 06:00 run rides out a database
# outage of up to ~45 minutes and still lands before 07:00. Prefect offers no
# retry condition for flows, so a deterministic failure (validation) also
# retries; its alert then fires after the last attempt, around 06:50. A retry
# keeps its flow run id, so it resumes its own run row, and the cached
# transform makes the recomputation cheap.
@flow(
    retries=3,
    retry_delay_seconds=900,
    timeout_seconds=1800,
    on_failure=[mark_run_failed],
    on_crashed=[mark_run_crashed],
    on_cancellation=[mark_run_cancelled],
)
def weekly_warehouse_client_performance(
    week_start: date | None = None,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    trigger: run_log.Trigger = "schedule",
    triggered_by: UUID | None = None,
) -> dict[str, Any] | State:
    """Rebuild whole closed weeks and publish them (design section 4).

    Without `week_start`, the last `lookback_weeks` closed weeks; with it, that
    one closed week. Ends `Cancelled` (not `Failed`) when another run holds the
    slot or has already published a newer snapshot of the same weeks.
    """
    run_logger = get_run_logger()
    now = datetime.now(timezone.utc)
    weeks = weekly_rollup.closed_weeks(now, week_start, lookback_weeks)
    window_start, window_end = weekly_rollup.window_bounds(weeks)

    run = open_run(weeks, trigger, triggered_by)
    if run.status == "skipped":
        return Cancelled(message=f"Skipped: run {run.blocked_by_run_id} holds the slot")
    if run.status == "completed":
        return Completed(message="This flow run already published its weeks")
    if window_end > run.arrival_cutoff:
        raise weekly_rollup.InvalidWeek("The newest week has not closed on the database clock")

    stage: run_log.ErrorStage = "extract"
    events_read: int | None = None
    try:
        source = extract_source_events(
            window_start, window_end, run.arrival_cutoff, run.previous_cutoff
        )
        events_read = len(source.events)
        products = extract_product_clients()
        order_counts = extract_inventory_order_counts(window_start, window_end)

        stage = "transform"
        rollup = transform_weekly_rollup(source.events, products, weeks)

        # Non-critical: the KPIs do not depend on it. If it fails, the figures
        # are still published and the run is marked `degraded`, because
        # withholding the numbers would be worse than publishing them with a
        # visible warning (design 3.5).
        health_state = compute_collection_health(
            source.events, rollup.exclusions, order_counts, weeks, return_state=True
        )
        health = health_state.result() if health_state.is_completed() else None
        if health is None:
            run_logger.warning(
                "Collection health failed (%s); publishing with quality_status=degraded",
                health_state.name,
            )

        stage = "validate"
        rows = validate_weekly_rows(rollup.rows, weeks, weekly_rollup.last_closed_week(now))

        stage = "load"
        loaded = load_weekly_rows(run, weeks, rows, rollup, health, source)
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, weekly_rollup.RollupValidationError)
            else STAGE_MESSAGES[stage]
        )
        raise PipelineStageError(stage, type(error).__name__, message, events_read) from error

    if loaded.status == "skipped":
        return Cancelled(
            message=f"Skipped: run {loaded.blocked_by_run_id} published a newer snapshot"
        )

    # Non-critical: a missing eval file must never fail a run whose data is
    # already committed, so its failure is handled here instead of propagating.
    snapshot_state = write_eval_snapshot(
        run, weeks, rows, rollup, health, loaded, EVAL_DIR, return_state=True
    )
    snapshot_path = snapshot_state.result() if snapshot_state.is_completed() else None
    if snapshot_path is None:
        run_logger.warning(
            "Eval snapshot not written (%s); the report is unaffected", snapshot_state.name
        )

    return {
        "pipeline_name": PIPELINE_NAME,
        "run_id": str(run.run_id),
        "status": loaded.status,
        "weeks": [week.isoformat() for week in weeks],
        "arrival_cutoff": run.arrival_cutoff.astimezone(timezone.utc).isoformat(),
        "events_read": rollup.events_read,
        "events_excluded": rollup.events_excluded,
        "rows_inserted": loaded.rows_inserted,
        "rows_updated": loaded.rows_updated,
        "rows_unchanged": loaded.rows_unchanged,
        "rows_deleted": loaded.rows_deleted,
        "weeks_revised": [week.isoformat() for week in loaded.weeks_revised],
        "quality_status": health.quality_status if health else "degraded",
        "min_capture_coverage": health.min_capture_coverage if health else None,
        "eval_snapshot": snapshot_path,
    }


@flow(timeout_seconds=6 * 3600)
def backfill_weekly_warehouse_client_performance(
    from_week: date, to_week: date
) -> list[dict[str, Any]]:
    """Rebuild a range of closed weeks, oldest first, one subflow run per week.

    Repairs history after a fix or a large late import. Each week commits on its
    own, so a backfill that stops can be resumed from the week that failed.
    """
    weeks = weekly_rollup.weeks_between(from_week, to_week)
    if weeks[-1] > weekly_rollup.last_closed_week(datetime.now(timezone.utc)):
        raise weekly_rollup.InvalidWeek("to_week must be a closed week")
    results: list[dict[str, Any]] = []
    for week in weeks:
        state = weekly_warehouse_client_performance(
            week_start=week, lookback_weeks=1, trigger="backfill", return_state=True
        )
        if not state.is_completed():
            raise RuntimeError(f"Backfill stopped at week {week.isoformat()}: {state.name}")
        results.append(cast(dict[str, Any], state.result()))
    return results


# --- Command line --------------------------------------------------------------------


def serve() -> None:
    """Serve the `weekly` deployment: the Monday schedule and the manual-trigger target.

    Needs a Prefect server (`prefect server start`) and `PREFECT_API_URL`
    pointing at it, the same URL the API uses for `POST /reporting/pipeline-runs`.
    At most one flow run executes at a time; a second one waits (ENQUEUE) instead
    of being dropped, so the Monday run is never lost to a manual click.
    """
    # Serve the flow from its importable module, not from `__main__`. The runner
    # hands each flow run to a child process with cloudpickle, which pickles
    # `__main__` objects by value; that copy fails to unpickle (a RecursionError
    # inside pydantic, seen 2026-10-22). Imported by name, it pickles by reference.
    from data.pipelines import pipeline as importable

    importable.weekly_warehouse_client_performance.serve(
        name=deployment.DEPLOYMENT_NAME,
        schedules=[Cron(SCHEDULE_CRON, timezone=SCHEDULE_TIMEZONE)],
        parameters={"lookback_weeks": DEFAULT_LOOKBACK_WEEKS, "trigger": "schedule"},
        global_limit=ConcurrencyLimitConfig(
            limit=1, collision_strategy=ConcurrencyLimitStrategy.ENQUEUE
        ),
        description="Weekly Warehouse & Client Performance Report (PIPELINE_DESIGN.md)",
        version=code_version(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly Warehouse & Client Performance pipeline (see PIPELINE_DESIGN.md)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--week-start", type=date.fromisoformat, help="rebuild one closed week (a Monday)"
    )
    mode.add_argument(
        "--backfill",
        nargs=2,
        type=date.fromisoformat,
        metavar=("FROM_WEEK", "TO_WEEK"),
        help="rebuild every closed week from FROM_WEEK to TO_WEEK (Mondays, inclusive)",
    )
    mode.add_argument("--serve", action="store_true", help="serve the scheduled deployment")
    parser.add_argument(
        "--lookback-weeks",
        type=int,
        default=DEFAULT_LOOKBACK_WEEKS,
        help=f"closed weeks to rebuild without --week-start (default {DEFAULT_LOOKBACK_WEEKS})",
    )
    parser.add_argument(
        "--flow-retries",
        type=int,
        default=None,
        help="override the flow's retries (3, 15 minutes apart) for an interactive run",
    )
    arguments = parser.parse_args(argv)

    if arguments.serve:
        serve()
        return 0

    # Checked here, before Prefect is involved: inside the flow a bad argument
    # would fail every attempt and wait out the 3 x 15 min flow retries first.
    try:
        now = datetime.now(timezone.utc)
        if arguments.backfill:
            backfill_weeks = weekly_rollup.weeks_between(*arguments.backfill)
            if backfill_weeks[-1] > weekly_rollup.last_closed_week(now):
                raise weekly_rollup.InvalidWeek("TO_WEEK must be a closed week")
        else:
            weekly_rollup.closed_weeks(now, arguments.week_start, arguments.lookback_weeks)
    except weekly_rollup.InvalidWeek as error:
        parser.error(str(error))

    state: State[Any]
    if arguments.backfill:
        state = backfill_weekly_warehouse_client_performance(*arguments.backfill, return_state=True)
    else:
        main_flow = weekly_warehouse_client_performance
        if arguments.flow_retries is not None:
            main_flow = main_flow.with_options(retries=arguments.flow_retries)
        state = main_flow(
            week_start=arguments.week_start,
            lookback_weeks=arguments.lookback_weeks,
            trigger="manual",
            return_state=True,
        )

    if state.is_completed():
        print(json.dumps(state.result(), indent=2, default=str))
        return 0
    print(f"{state.name}: {state.message}", file=sys.stderr)
    # A skipped run is expected behaviour (another run holds the slot), not a failure.
    return 0 if state.is_cancelled() else 1


if __name__ == "__main__":
    sys.exit(main())
