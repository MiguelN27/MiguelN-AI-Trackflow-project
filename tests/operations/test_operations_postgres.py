"""The operational monitor on a real PostgreSQL, opt-in (`INVENTORY_TEST_DATABASE_URL`).

Each test works in its own disposable schemas, selected with `SET LOCAL search_path`
like the other PostgreSQL suites. They cover what SQLite cannot: the advisory lock
that keeps two concurrent evaluations from opening the same alert, the weekly
report's run log in its own schema, RLS and JSONB on the two new tables.
"""

import os
import threading
from datetime import timedelta
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema
from sqlmodel import SQLModel

from data.notifications import outbox
from data.pipelines.rfp_intake import store as rfp_store
from services.inventory.models import InboundOrder, OutboundOrder, Product
from services.job_runner import models as job_models
from services.operations import models as ops_models
from services.operations import monitor, sources
from tests.operations.support import at, product, rules

REAL_CONNECT = psycopg2.connect
ZARAGOZA = rules(
    {
        "rule_id": "zaragoza-outbound",
        "source": "warehouse_orders",
        "warehouse": "Zaragoza",
        "activity": "outbound_order_created",
        "expected_within_minutes": 120,
    }
)


@pytest.fixture
def postgres(monkeypatch):
    value = os.getenv("INVENTORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "Set INVENTORY_TEST_DATABASE_URL explicitly for disposable-schema PostgreSQL tests"
        )
    url = make_url(value).set(drivername="postgresql+psycopg2")
    monkeypatch.setattr(psycopg2, "connect", REAL_CONNECT)
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=4,
        max_overflow=0,
        isolation_level="READ COMMITTED",
        connect_args={"sslmode": url.query.get("sslmode", "require"), "connect_timeout": 10},
    )
    schema = f"ops_test_{uuid4().hex}"
    reporting = f"ops_reporting_test_{uuid4().hex}"

    def use_test_schema(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')

    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(CreateSchema(reporting))
            created = True
        event.listen(engine, "begin", use_test_schema)
        SQLModel.metadata.create_all(
            engine,
            tables=[
                SQLModel.metadata.tables[str(model.__tablename__)]
                for model in (Product, InboundOrder, OutboundOrder)
            ],
        )
        job_models.metadata.create_all(engine)
        rfp_store.initialize_schema(engine)
        ops_models.initialize_schema(engine)
        monkeypatch.setattr(sources, "PIPELINE_SCHEMA", reporting)
        yield engine, schema, reporting
    finally:
        if event.contains(engine, "begin", use_test_schema):
            event.remove(engine, "begin", use_test_schema)
        if created:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
                connection.exec_driver_sql(f'DROP SCHEMA "{reporting}" CASCADE')
        engine.dispose()


def test_two_concurrent_evaluations_open_a_single_alert(postgres):
    engine, _, _ = postgres
    product(engine, "Zaragoza")
    start = threading.Barrier(2)
    results: list[int] = []
    failures: list[BaseException] = []

    def evaluate():
        start.wait()
        try:
            results.append(len(monitor.evaluate(engine, ZARAGOZA, now=at("2026-11-30 10:00"))))
        except BaseException as error:  # noqa: BLE001 - asserted below
            failures.append(error)

    threads = [threading.Thread(target=evaluate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert failures == []
    assert sorted(results) == [0, 1]
    rows = outbox.notifications_after(engine, 0, limit=10)
    assert [row["event"] for row in rows] == ["operational_inactivity_detected"]


def test_the_weekly_report_rule_reads_the_pipeline_run_log(postgres):
    engine, _, reporting = postgres
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{reporting}".pipeline_runs (pipeline_name text, status text, '
            "started_at timestamptz, finished_at timestamptz)"
        )
        connection.execute(
            text(
                f'INSERT INTO "{reporting}".pipeline_runs VALUES '
                "('weekly_warehouse_client_performance', 'completed', :started, :finished), "
                "('weekly_warehouse_client_performance', 'failed', :later, :later)"
            ),
            {
                "started": at("2026-11-16 12:00"),
                "finished": at("2026-11-16 12:05"),
                "later": at("2026-11-23 12:00"),
            },
        )
    weekly = rules(
        {
            "rule_id": "weekly-report",
            "source": "scheduled_job",
            "job_name": "weekly_warehouse_client_performance",
            "expected_within_minutes": 10260,
        }
    )

    assert monitor.evaluate(engine, weekly, now=at("2026-11-16 12:05") + timedelta(days=7)) == []
    [change] = monitor.evaluate(engine, weekly, now=at("2026-11-24 12:00"))

    assert change.alert.last_activity_at == at("2026-11-16 12:05")
    assert change.alert.details["last_run_status"] == "failed"


def test_rls_and_jsonb_on_the_new_tables(postgres):
    engine, schema, _ = postgres
    with engine.connect() as connection:
        rls = dict(
            connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = :schema "
                    "AND c.relname IN ('live_notifications', 'operational_alerts')"
                ),
                {"schema": schema},
            ).all()
        )
        json_types = set(
            connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns WHERE table_schema = :schema "
                    "AND table_name IN ('live_notifications', 'operational_alerts') "
                    "AND column_name IN ('payload', 'subject', 'details')"
                ),
                {"schema": schema},
            ).scalars()
        )
    assert rls == {"live_notifications": True, "operational_alerts": True}
    assert json_types == {"jsonb", "character varying"}  # subject is the outbox's text key
