"""The RFP intake tables on a real PostgreSQL, opt-in.

Set `INVENTORY_TEST_DATABASE_URL` to run them, as for the other PostgreSQL
suites. Each test works in its own disposable `rfp_test_*` schema, selected
with `SET LOCAL search_path` at the start of every transaction, so the
unqualified `rfp_*` tables resolve there, also behind Supabase's pooler.

They cover what SQLite cannot: JSONB columns, RLS, the check and unique
constraints enforced by the server, a whole intake and a whole proposal
drafting persisted through the real engine, the Part 2 column upgrade of a
Part 1 database, and both queues' `FOR UPDATE SKIP LOCKED` between two
connections.
"""

import os
from datetime import timedelta
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema
from sqlmodel import Session

from data.pipelines.rfp_intake import handoff, runner, store
from data.pipelines.rfp_intake.models import PART_2_SECTION_COLUMNS, RfpHandoff, RfpProposalHandoff
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from data.pipelines.rfp_intake.proposal import store as proposal_store
from tests.pipelines.rfp_intake.proposal import support as proposal_support
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket

REAL_CONNECT = psycopg2.connect
TABLES = (
    "rfp_tickets",
    "rfp_metadata",
    "rfp_department_sections",
    "rfp_handoffs",
    "rfp_ticket_events",
    "rfp_section_drafts",
    "rfp_proposal_handoffs",
)


@pytest.fixture
def postgres(monkeypatch):
    value = os.getenv("INVENTORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "Set INVENTORY_TEST_DATABASE_URL explicitly for disposable-schema PostgreSQL tests"
        )
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        pytest.fail("PostgreSQL integration tests require a PostgreSQL URL")
    url = url.set(drivername="postgresql+psycopg2")
    monkeypatch.setattr(psycopg2, "connect", REAL_CONNECT)
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=4,
        max_overflow=0,
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args={"sslmode": url.query.get("sslmode", "require"), "connect_timeout": 10},
    )
    schema = f"rfp_test_{uuid4().hex}"

    def use_test_schema(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')

    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            created = True
        event.listen(engine, "begin", use_test_schema)
        store.initialize_schema(engine)
        yield engine, schema
    finally:
        if event.contains(engine, "begin", use_test_schema):
            event.remove(engine, "begin", use_test_schema)
        if created:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


def test_the_schema_uses_jsonb_and_rls_and_initializing_twice_is_harmless(postgres):
    engine, schema = postgres
    store.initialize_schema(engine)

    with engine.connect() as connection:
        rls = dict(
            connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema "
                    "AND c.relkind = 'r'"
                ),
                {"schema": schema},
            ).all()
        )
        json_types = set(
            connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns WHERE table_schema = "
                    ":schema AND column_name IN ('classification', 'key_aspects', 'payload', "
                    "'draft_data', 'evaluation_results', 'evaluation')"
                ),
                {"schema": schema},
            ).scalars()
        )
    assert rls == dict.fromkeys(TABLES, True)
    assert json_types == {"jsonb"}


def test_the_server_enforces_status_and_department_constraints(postgres, tmp_path):
    engine, _ = postgres
    ticket_id = new_ticket(engine, tmp_path, "formal")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE rfp_tickets SET status = 'approved' WHERE id = :id"), {"id": ticket_id}
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO rfp_department_sections (id, ticket_id, department_id, owner, "
                "extracts, key_aspects, needed_from_department, open_questions, "
                "validation_notes, created_at) VALUES (:id, :ticket, 'finance', 'x', '[]', "
                "'[]', '[]', '[]', '[]', now())"
            ),
            {"id": uuid4(), "ticket": ticket_id},
        )


def test_a_whole_intake_is_persisted_and_read_back(postgres, tmp_path):
    engine, _ = postgres
    ticket_id = new_ticket(engine, tmp_path, "formal")

    status = runner.run_intake(ticket_id, engine, agents=ScriptedAgents("formal").bundle())

    detail = store.get_ticket_detail(engine, ticket_id)
    assert status == "intake_complete"
    assert detail["metadata"]["currency"] == "EUR"
    assert detail["metadata"]["deadline"].isoformat() == "2026-08-28"
    assert detail["sections"][1]["key_aspects"][0]["source_quote"].startswith("Gestión")
    assert detail["created_at"].tzinfo is not None
    assert handoff.load_handoff(engine, ticket_id).client.country == "ES"


def test_claim_next_skips_a_handoff_another_worker_holds(postgres, tmp_path):
    engine, _ = postgres
    first = new_ticket(engine, tmp_path, "formal")
    second = new_ticket(engine, tmp_path, "informal")
    runner.run_intake(first, engine, agents=ScriptedAgents("formal").bundle())
    runner.run_intake(second, engine, agents=ScriptedAgents("informal").bundle())

    with Session(engine) as holder:
        held = holder.execute(
            select(RfpHandoff).where(RfpHandoff.ticket_id == first).with_for_update()  # type: ignore[arg-type]
        ).scalar_one()
        claimed = handoff.claim_next(engine, claimed_by="worker-b")
        assert held.ticket_id == first
        holder.rollback()

    assert claimed is not None
    assert claimed.handoff.ticket_id == second
    assert store.get_ticket_detail(engine, second)["status"] == "drafting"
    assert handoff.claim_next(engine, claimed_by="worker-a").handoff.ticket_id == first


def test_the_sweep_uses_aware_timestamps(postgres, tmp_path):
    engine, _ = postgres
    ticket_id = new_ticket(engine, tmp_path, "formal")

    assert store.sweep_abandoned(engine, older_than=timedelta(minutes=30)) == []
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE rfp_tickets SET updated_at = now() - interval '2 hours' WHERE id = :id"),
            {"id": ticket_id},
        )
    assert store.sweep_abandoned(engine, older_than=timedelta(minutes=30)) == [ticket_id]


# --- Part 2 -------------------------------------------------------------------------------


def drafted(engine, tmp_path, kind):
    ticket_id = new_ticket(engine, tmp_path, kind)
    runner.run_intake(ticket_id, engine, agents=ScriptedAgents(kind).bundle())
    proposal_runner.run_pending(
        engine,
        agents=proposal_support.ScriptedProposalAgents().bundle(),
        settings=proposal_support.settings(),
    )
    return ticket_id


def test_a_part_1_database_gets_the_part_2_columns_and_their_check(postgres, tmp_path):
    engine, schema = postgres
    with engine.begin() as connection:
        for column in PART_2_SECTION_COLUMNS:
            connection.execute(text(f"ALTER TABLE rfp_department_sections DROP COLUMN {column}"))

    store.initialize_schema(engine)
    store.initialize_schema(engine)

    with engine.connect() as connection:
        columns = dict(
            connection.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns WHERE "
                    "table_schema = :schema AND table_name = 'rfp_department_sections'"
                ),
                {"schema": schema},
            ).all()
        )
    assert columns["draft_data"] == "jsonb" and columns["draft_run_id"] == "uuid"
    assert columns["draft_iterations"] == "integer"
    ticket_id = new_ticket(engine, tmp_path, "formal")
    runner.run_intake(ticket_id, engine, agents=ScriptedAgents("formal").bundle())
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE rfp_department_sections SET draft_status = 'approved' WHERE ticket_id = :id"
            ),
            {"id": ticket_id},
        )


def test_a_whole_drafting_is_persisted_and_read_back(postgres, tmp_path):
    engine, _ = postgres

    ticket_id = drafted(engine, tmp_path, "formal")

    detail = store.get_ticket_detail(engine, ticket_id)
    assert detail["status"] == "waiting_for_approval"
    assert detail["proposal"]["sla_department"] == "warehouse"
    assert [s["evaluation"]["readability"]["pass"] for s in detail["sections"]] == [True, True]
    assert all(s["ready_at"].tzinfo is not None for s in detail["sections"])
    proposal = proposal_handoff.load_proposal_handoff(engine, ticket_id)
    assert proposal.currency == "EUR" and len(proposal.sections) == 2
    assert proposal_store.iteration_kpi(engine)["average_iterations_per_section"] == 1.0


def test_part_3_claims_skip_a_proposal_another_worker_holds(postgres, tmp_path):
    engine, _ = postgres
    first = drafted(engine, tmp_path, "informal")
    second = drafted(engine, tmp_path, "formal")

    with Session(engine) as holder:
        holder.execute(
            select(RfpProposalHandoff)
            .where(RfpProposalHandoff.ticket_id == first)  # type: ignore[arg-type]
            .with_for_update()
        ).scalar_one()
        claimed = proposal_handoff.claim_next(engine, claimed_by="approval-b")
        holder.rollback()

    assert claimed is not None and claimed.proposal.ticket_id == second
    assert proposal_handoff.claim_next(engine, claimed_by="approval-a").proposal.ticket_id == first
