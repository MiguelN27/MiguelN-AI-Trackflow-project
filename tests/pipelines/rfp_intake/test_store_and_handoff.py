"""Persistence rules (status changes, the stale sweep) and the Part 2 handoff queue."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, update
from sqlmodel import Session

from data.pipelines.rfp_intake import handoff, runner, store
from data.pipelines.rfp_intake.models import RfpTicket, utcnow
from scripts import rfp_intake as rfp_cli
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket


def complete(engine, tmp_path, kind="formal"):
    ticket_id = new_ticket(engine, tmp_path / "pdf", kind)
    runner.run_intake(ticket_id, engine, agents=ScriptedAgents(kind).bundle())
    return ticket_id


def age(engine, ticket_id, minutes):
    with Session(engine) as session:
        session.execute(
            update(RfpTicket)
            .where(RfpTicket.id == ticket_id)  # type: ignore[arg-type]
            .values(updated_at=utcnow() - timedelta(minutes=minutes))
        )
        session.commit()


# --- Store ---------------------------------------------------------------------------


def test_the_schema_is_the_eleven_rfp_tables_and_initializing_twice_is_harmless(engine):
    store.initialize_schema(engine)

    assert set(inspect(engine).get_table_names()) == {
        "rfp_tickets",
        "rfp_metadata",
        "rfp_department_sections",
        "rfp_handoffs",
        "rfp_ticket_events",
        # Part 2
        "rfp_section_drafts",
        "rfp_proposal_handoffs",
        # Part 3
        "rfp_approval_runs",
        "rfp_approval_decisions",
        "rfp_final_documents",
        # Milestone 10: the live notification stream's feed
        "rfp_ticket_notifications",
    }


def test_a_new_ticket_is_analyzing_and_queued(engine, tmp_path):
    ticket_id = new_ticket(engine, tmp_path, "formal")

    detail = store.get_ticket_detail(engine, ticket_id)

    assert detail["status"] == "analyzing"
    assert detail["current_stage"] == "queued"
    assert detail["metadata"] is None
    assert store.get_ticket_detail(engine, uuid4()) is None


def test_the_list_is_newest_first_with_the_client(engine, tmp_path):
    first = complete(engine, tmp_path, "formal")
    second = new_ticket(engine, tmp_path, "informal")

    rows = store.list_tickets(engine)

    assert [row["ticket_id"] for row in rows] == [second, first]
    assert rows[1]["client_name"] == "ModaViva, S.L."
    assert rows[1]["departments_needed"] == ["warehouse", "reverse"]
    assert rows[0]["client_name"] is None


def test_the_sweep_closes_only_stalled_analyses(engine, tmp_path):
    stalled = new_ticket(engine, tmp_path, "formal")
    fresh = new_ticket(engine, tmp_path, "formal")
    done = complete(engine, tmp_path, "invalid")
    age(engine, stalled, 45)
    age(engine, done, 45)

    closed = store.sweep_abandoned(engine, older_than=timedelta(minutes=30))

    assert closed == [stalled]
    stalled_detail = store.get_ticket_detail(engine, stalled)
    assert stalled_detail["status"] == "failed"
    assert stalled_detail["error_stage"] == "abandoned"
    assert "30 minutes" in stalled_detail["error_message"]
    assert store.get_ticket_detail(engine, fresh)["status"] == "analyzing"
    assert store.get_ticket_detail(engine, done)["status"] == "discarded"


def test_a_finished_ticket_cannot_be_overwritten_by_a_late_step(engine, tmp_path):
    ticket_id = complete(engine, tmp_path, "invalid")

    with pytest.raises(store.TicketStateError):
        store.set_stage(engine, ticket_id, "classify_document")
    with pytest.raises(store.TicketStateError):
        store.complete_intake(
            engine,
            ticket_id,
            sections=[{"department_id": "warehouse", "owner": "Ana Whitfield"}],
            sales_brief={},
            handoff_payload={},
            contract_version=handoff.CONTRACT_VERSION,
            next_stage=handoff.NEXT_STAGE,
        )
    detail = store.get_ticket_detail(engine, ticket_id)
    # The failed transaction left nothing behind.
    assert detail["status"] == "discarded"
    assert detail["sections"] == []
    assert detail["handoff"] is None
    assert store.fail(engine, ticket_id, stage="x", message="y") is False


# --- Handoff -------------------------------------------------------------------------


def test_claim_next_hands_out_each_ticket_once_and_moves_it_to_drafting(engine, tmp_path):
    first = complete(engine, tmp_path, "formal")
    second = complete(engine, tmp_path, "informal")
    complete(engine, tmp_path, "invalid")  # discarded: never queued

    claims = [handoff.claim_next(engine, claimed_by="drafting-worker") for _ in range(3)]

    assert [claim.handoff.ticket_id for claim in claims[:2]] == [first, second]
    assert claims[2] is None
    assert store.get_ticket_detail(engine, first)["status"] == "drafting"
    assert store.get_ticket_detail(engine, first)["handoff"]["status"] == "claimed"
    assert claims[0].claimed_by == "drafting-worker"


def test_the_payload_validates_against_the_contract(engine, tmp_path):
    ticket_id = complete(engine, tmp_path, "informal")

    payload = handoff.load_handoff(engine, ticket_id)

    assert payload is not None
    round_trip = handoff.IntakeHandoff.model_validate_json(payload.model_dump_json())
    assert round_trip == payload
    assert payload.client.model_dump() == {
        "name": "Luna Cosmetics",
        "country": "US",
        "currency": "USD",
    }
    assert payload.monthly_volume == 5000
    assert payload.deadline is None
    assert payload.deadline_text == "around August 24th"


def test_the_documented_schema_matches_the_model():
    document = rfp_cli.CONTRACT_DOC.read_text(encoding="utf-8")
    assert rfp_cli.schema_block() in document, "run: npm run rfp -- handoff-schema --write"


def test_the_rfp_tables_stay_out_of_the_shared_sqlmodel_metadata():
    # Inventory initializes and tests `SQLModel.metadata` as exactly its own tables.
    from sqlmodel import SQLModel

    assert not [name for name in SQLModel.metadata.tables if name.startswith("rfp_")]
