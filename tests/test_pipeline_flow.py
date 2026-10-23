"""The Prefect flow (`data/pipelines/pipeline.py`): task wiring, failure handling, caching.

Runs under `prefect_test_harness` with every database call mocked, so nothing
here needs PostgreSQL; `tests/test_reporting_postgres.py` covers the SQL.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest
from prefect.states import Completed, Failed
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy.exc import OperationalError, ProgrammingError

from data.pipelines import pipeline, run_log
from data.pipelines.weekly_warehouse_client_performance import extract, load, schema
from data.process import collection_health, weekly_rollup
from tests.test_weekly_rollup import PRODUCTS, event

CUTOFF = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
WEEKS = (date(2026, 10, 5), date(2026, 10, 12))


@pytest.fixture(scope="module")
def harness():
    with prefect_test_harness(server_startup_timeout=120):
        yield


class Database:
    """What the flow would read and write, with every call recorded."""

    def __init__(self):
        self.events = pd.DataFrame(
            [
                event(weekly_rollup.INBOUND, "2026-10-12T09:00:00Z", quantity="7"),
                event(weekly_rollup.OUTBOUND, "2026-10-13T09:00:00Z"),
            ]
        )
        self.reads = {"events": 0, "products": 0, "orders": 0}
        self.read_errors: list[Exception] = []
        self.loads: list[dict] = []
        self.failures: list[dict] = []
        self.run_status = "running"
        self.open_errors: list[Exception] = []
        self.opens = 0
        self.fail_outcome = "failed"  # what run_log.fail_run reports back

    def open_run(self, engine, **kwargs):
        self.opens += 1
        if self.open_errors:
            raise self.open_errors.pop(0)
        return run_log.RunHandle(
            run_id=uuid4(),
            status=self.run_status,
            arrival_cutoff=CUTOFF,
            previous_cutoff=None,
            blocked_by_run_id=uuid4() if self.run_status == "skipped" else None,
        )

    def read_source_events(self, engine, **window):
        self.reads["events"] += 1
        if self.read_errors:
            raise self.read_errors.pop(0)
        return extract.SourceExtract(
            events=self.events.copy(),
            source_watermark=CUTOFF,
            late_events_outside_window=None,
            telemetry_dropped=0,
        )

    def read_product_clients(self, engine):
        self.reads["products"] += 1
        return PRODUCTS.copy()

    def read_inventory_order_counts(self, engine, **window):
        self.reads["orders"] += 1
        return pd.DataFrame(
            [
                {
                    "warehouse": "Monterrey",
                    "day": date(2026, 10, 12),
                    "event_type": weekly_rollup.INBOUND,
                    "inventory_orders": 1,
                },
                {
                    "warehouse": "Monterrey",
                    "day": date(2026, 10, 13),
                    "event_type": weekly_rollup.OUTBOUND,
                    "inventory_orders": 1,
                },
            ]
        )

    def load_weekly_rows(self, engine, **kwargs):
        self.loads.append(kwargs)
        return load.LoadResult(status="completed", rows_inserted=len(kwargs["rows"]))

    def fail_run(self, engine, **kwargs):
        self.failures.append(kwargs)
        if isinstance(self.fail_outcome, Exception):
            raise self.fail_outcome
        return self.fail_outcome


@pytest.fixture
def database(harness, monkeypatch, tmp_path):
    db = Database()
    monkeypatch.setattr(pipeline.reporting_db, "get_pipeline_engine", lambda: "engine")
    monkeypatch.setattr(schema, "ensure_schema", lambda engine: None)
    monkeypatch.setattr(run_log, "open_run", db.open_run)
    monkeypatch.setattr(run_log, "fail_run", db.fail_run)
    monkeypatch.setattr(extract, "read_source_events", db.read_source_events)
    monkeypatch.setattr(extract, "read_product_clients", db.read_product_clients)
    monkeypatch.setattr(extract, "read_inventory_order_counts", db.read_inventory_order_counts)
    monkeypatch.setattr(load, "load_weekly_rows", db.load_weekly_rows)
    monkeypatch.setattr(weekly_rollup, "closed_weeks", lambda now, week, lookback: WEEKS)
    monkeypatch.setattr(weekly_rollup, "last_closed_week", lambda now: WEEKS[-1])
    # Results and snapshots go to this test's own folders, never data/raw or data/eval.
    for name in ("transform_weekly_rollup", "compute_collection_health"):
        task = getattr(pipeline, name)
        monkeypatch.setattr(pipeline, name, task.with_options(result_storage=tmp_path / "results"))
    for name in ("open_run", "extract_source_events", "load_weekly_rows"):
        task = getattr(pipeline, name)
        monkeypatch.setattr(pipeline, name, task.with_options(retry_delay_seconds=0))
    monkeypatch.setattr(pipeline, "EVAL_DIR", tmp_path / "eval")
    return db


def run(**parameters):
    flow = pipeline.weekly_warehouse_client_performance.with_options(retries=0)
    return flow(**parameters, return_state=True)


def test_every_stage_runs_and_the_load_gets_validated_rows(database, tmp_path):
    state = run(trigger="manual")

    assert state.is_completed()
    summary = state.result()
    assert summary["status"] == "completed"
    assert summary["weeks"] == ["2026-10-05", "2026-10-12"]
    assert summary["events_read"] == 2
    assert database.reads == {"events": 1, "products": 1, "orders": 1}

    (loaded,) = database.loads
    rows = loaded["rows"]
    assert tuple(rows.columns) == weekly_rollup.ROW_COLUMNS
    stride = rows[(rows["client_id"] == "stride-athletics") & (rows["week_start"] == WEEKS[1])]
    assert stride[["inbound_units_count", "outbound_orders_count"]].values.tolist() == [[7, 1]]
    assert loaded["health"].quality_status == "ok"
    assert loaded["run_fields"]["events_read"] == 2

    snapshot = json.loads(Path(summary["eval_snapshot"]).read_text())
    assert snapshot["validation"] == {"passed": True, "rows": len(rows)}
    assert "stride-athletics" not in json.dumps(snapshot)  # no per-client figures


def test_a_failed_eval_snapshot_does_not_fail_the_run(database, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setattr(pipeline, "EVAL_DIR", blocker / "eval")

    state = run()

    assert state.is_completed()
    assert state.result()["eval_snapshot"] is None
    assert len(database.loads) == 1


def test_failed_collection_health_publishes_the_figures_as_degraded(database, monkeypatch):
    def broken(*args):
        raise ValueError("unexpected shape")

    monkeypatch.setattr(collection_health, "compute", broken)

    state = run()

    assert state.is_completed()
    assert state.result()["quality_status"] == "degraded"
    (loaded,) = database.loads
    assert loaded["health"] is None
    assert not loaded["rows"].empty


def test_transient_read_errors_are_retried(database):
    lost = OperationalError("SELECT", {}, Exception("server closed the connection"))
    database.read_errors = [lost, lost]

    state = run()

    assert state.is_completed()
    assert database.reads["events"] == 3


def test_a_broken_query_is_not_retried_and_the_stage_is_recorded(database):
    database.read_errors = [ProgrammingError("SELECT", {}, Exception("column does not exist"))]

    state = run()

    assert state.is_failed()
    assert database.reads["events"] == 1
    assert database.loads == []
    (failure,) = database.failures
    assert failure["error_stage"] == "extract"
    assert failure["error_class"] == "ProgrammingError"
    assert failure["error_message"] == "Reading the source tables failed"
    assert failure["events_read"] is None  # it failed before reading anything


def test_validation_failure_blocks_the_load(database, monkeypatch):
    def refuse(rows, weeks, newest):
        raise weekly_rollup.RollupValidationError("Rollup refused: 1 row(s) have a negative count")

    monkeypatch.setattr(weekly_rollup, "validate", refuse)

    state = run()

    assert state.is_failed()
    assert database.loads == []
    (failure,) = database.failures
    assert (failure["error_stage"], failure["error_class"]) == ("validate", "RollupValidationError")
    assert "negative count" in failure["error_message"]
    assert failure["events_read"] == 2  # records processed, kept for a failed run too
    assert not fallback_file().exists()  # the run log took the record


def fallback_file():
    return pipeline.EVAL_DIR / pipeline.RUN_LOG_FALLBACK_FILE


def fallback_lines():
    return [json.loads(line) for line in fallback_file().read_text().splitlines()]


def test_a_run_that_never_got_its_row_is_recorded_in_the_fallback_file(database):
    lost = OperationalError("INSERT", {}, Exception("could not connect"))
    database.open_errors = [lost, lost, lost]
    database.fail_outcome = "missing"

    state = run(trigger="manual")

    assert state.is_failed()
    assert database.opens == 3  # open_run's 2 retries were used
    (line,) = fallback_lines()
    assert line["status"] == "failed"
    assert (line["error_stage"], line["error_class"]) == (None, "OperationalError")
    assert line["events_read"] is None
    assert line["trigger"] == "manual"
    assert line["started_at"] and line["finished_at"]
    assert line["recorded_here_because"] == "no run row"


def test_an_unreachable_run_log_is_recorded_in_the_fallback_file(database, monkeypatch):
    def refuse(rows, weeks, newest):
        raise weekly_rollup.RollupValidationError("Rollup refused: 1 row(s) have a negative count")

    monkeypatch.setattr(weekly_rollup, "validate", refuse)
    database.fail_outcome = OperationalError("UPDATE", {}, Exception("server closed"))

    state = run()

    assert state.is_failed()
    (line,) = fallback_lines()
    assert (line["error_stage"], line["error_class"]) == ("validate", "RollupValidationError")
    assert line["events_read"] == 2
    assert line["recorded_here_because"] == "run log unreachable"


def test_a_run_that_lost_the_race_ends_cancelled_without_reading(database):
    database.run_status = "skipped"

    state = run()

    assert state.is_cancelled()
    assert database.reads == {"events": 0, "products": 0, "orders": 0}
    assert database.failures == []
    assert not fallback_file().exists()  # its skipped row is its record


def test_a_flow_retry_after_commit_does_not_load_again(database):
    database.run_status = "completed"

    state = run()

    assert state.is_completed()
    assert database.loads == []


def test_the_transform_is_reused_within_the_hour_for_the_same_input(database, monkeypatch):
    calls = []
    real = weekly_rollup.build_rollup

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(weekly_rollup, "build_rollup", counting)

    assert run().is_completed()
    assert run().is_completed()
    assert len(calls) == 1  # the second run hit the cache

    database.events = pd.concat(
        [database.events, pd.DataFrame([event(weekly_rollup.OUTBOUND, "2026-10-14T09:00:00Z")])],
        ignore_index=True,
    )
    assert run().is_completed()
    assert len(calls) == 2  # a new event is a new key


# --- Pieces that need no flow run ----------------------------------------------------


def test_cache_key_follows_content_not_identity():
    events = pd.DataFrame([event(weekly_rollup.INBOUND, "2026-10-12T09:00:00Z", quantity="1")])
    base = {"events": events, "products": PRODUCTS, "weeks": WEEKS}
    key = pipeline.rollup_cache_key(None, base)

    assert pipeline.rollup_cache_key(None, {**base, "events": events.copy()}) == key
    changed = events.assign(quantity="2")
    assert pipeline.rollup_cache_key(None, {**base, "events": changed}) != key
    assert pipeline.rollup_cache_key(None, {**base, "weeks": WEEKS[1:]}) != key
    owners = PRODUCTS.assign(client_id="coco-beauty")
    assert pipeline.rollup_cache_key(None, {**base, "products": owners}) != key


@pytest.mark.parametrize(
    ("error", "retry"),
    [
        (OperationalError("SELECT", {}, Exception("timeout")), True),
        (TimeoutError(), True),
        (ConnectionError(), True),
        (ProgrammingError("SELECT", {}, Exception("syntax")), False),
        (weekly_rollup.RollupValidationError("refused"), False),
        (ValueError(), False),
    ],
)
def test_only_transient_errors_are_retried(error, retry):
    state = Failed(data=error)
    assert pipeline.retry_on_transient_error(None, None, state) is retry


@pytest.fixture
def hook_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.reporting_db, "get_pipeline_engine", lambda: "engine")
    monkeypatch.setattr(pipeline, "EVAL_DIR", tmp_path / "eval")
    return SimpleNamespace(id=uuid4(), parameters={"trigger": "schedule"}, start_time=CUTOFF)


def test_failure_hook_records_stage_errors_in_the_run_log(monkeypatch, hook_run):
    recorded = []

    def fail_run(engine, **kw):
        recorded.append(kw)
        return "failed"

    monkeypatch.setattr(run_log, "fail_run", fail_run)

    error = pipeline.PipelineStageError("load", "OperationalError", "Publishing failed", 12)
    pipeline.mark_run_failed(None, hook_run, Failed(data=error))
    assert (recorded[-1]["error_stage"], recorded[-1]["events_read"]) == ("load", 12)
    assert recorded[-1]["flow_run_id"] == hook_run.id

    pipeline.mark_run_crashed(None, hook_run, Completed())
    assert (recorded[-1]["error_stage"], recorded[-1]["error_class"]) == (None, "Crashed")
    assert not fallback_file().exists()


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("missing", "no run row"),
        (OperationalError("UPDATE", {}, Exception("down")), "run log unreachable"),
        ("unchanged", None),  # already final, e.g. a run that skipped itself
    ],
)
def test_failure_hook_falls_back_to_a_log_file_only_when_needed(
    monkeypatch, hook_run, outcome, reason
):
    def fail_run(engine, **kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(run_log, "fail_run", fail_run)
    error = pipeline.PipelineStageError("load", "OperationalError", "Publishing failed", 7)

    pipeline.mark_run_failed(None, hook_run, Failed(data=error))  # never raises

    if reason is None:
        assert not fallback_file().exists()
    else:
        (line,) = fallback_lines()
        assert line["flow_run_id"] == str(hook_run.id)
        assert (line["trigger"], line["events_read"], line["status"]) == ("schedule", 7, "failed")
        assert line["started_at"].startswith("2026-10-22")
        assert line["recorded_here_because"] == reason


def test_failure_hook_survives_an_unwritable_fallback(monkeypatch, hook_run, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(pipeline, "EVAL_DIR", blocker / "eval")
    monkeypatch.setattr(run_log, "fail_run", lambda engine, **kw: "missing")
    pipeline.mark_run_failed(None, hook_run, Failed(data=ValueError()))  # logs, never raises


@pytest.mark.parametrize(
    "argv",
    [
        ["--week-start", "2026-10-13"],  # not a Monday
        ["--week-start", "2999-01-05"],  # not closed
        ["--lookback-weeks", "0"],
        ["--backfill", "2026-10-12", "2026-09-28"],
    ],
)
def test_cli_rejects_bad_arguments_before_starting_a_flow(argv, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(
        pipeline, "weekly_warehouse_client_performance", lambda **kw: started.append(kw)
    )
    with pytest.raises(SystemExit) as exited:
        pipeline.main(argv)
    assert exited.value.code == 2
    assert started == []
