"""Part 2 persistence: the schema upgrade, the sweep, the detail, the KPI and the Part 3 queue."""

from datetime import timedelta

from sqlalchemy import inspect, text, update
from sqlmodel import Session

from data.pipelines.rfp_intake import handoff, runner, store
from data.pipelines.rfp_intake.models import ADDED_SECTION_COLUMNS, RfpTicket, utcnow
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from data.pipelines.rfp_intake.proposal import store as proposal_store
from scripts import rfp_intake as rfp_cli
from tests.pipelines.rfp_intake.proposal import support
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket


def drafted(engine, tmp_path, kind="informal", **agents_kwargs):
    ticket_id = support.intake_complete(engine, tmp_path, kind)
    proposal_runner.run_pending(
        engine,
        agents=support.ScriptedProposalAgents(**agents_kwargs).bundle(),
        settings=support.settings(),
    )
    return ticket_id


def test_a_database_from_part_1_gets_the_later_section_columns(engine, tmp_path):
    complete = new_ticket(engine, tmp_path / "pdf", "formal")
    runner.run_intake(complete, engine, agents=ScriptedAgents("formal").bundle())
    part_1_columns = [
        c["name"]
        for c in inspect(engine).get_columns("rfp_department_sections")
        if c["name"] not in ADDED_SECTION_COLUMNS
    ]
    with engine.begin() as connection:  # back to the Part 1 table, with its rows
        connection.execute(
            text(
                f"CREATE TABLE part_1_sections AS SELECT {', '.join(part_1_columns)} "
                "FROM rfp_department_sections"
            )
        )
        connection.execute(text("DROP TABLE rfp_department_sections"))
        connection.execute(text("ALTER TABLE part_1_sections RENAME TO rfp_department_sections"))

    store.initialize_schema(engine)
    store.initialize_schema(engine)  # and again: harmless

    columns = {c["name"] for c in inspect(engine).get_columns("rfp_department_sections")}
    assert set(ADDED_SECTION_COLUMNS) <= columns
    rows = proposal_store.load_sections(engine, complete)
    assert [row.draft_iterations for row in rows] == [0, 0]
    assert [row.review_rounds for row in rows] == [0, 0]
    assert all(row.draft_status is None for row in rows)


def test_the_documented_proposal_schema_matches_the_model():
    document = rfp_cli.PROPOSAL_DOC.read_text(encoding="utf-8")
    assert rfp_cli.proposal_schema_block() in document, (
        "run: npm run rfp -- proposal-schema --write"
    )


def test_the_proposal_schema_uses_the_tickets_field_names():
    schema = proposal_handoff.json_schema()
    readability = schema["$defs"]["ReadabilityResult"]["properties"]
    assert set(readability) == {"pass", "score", "details"}
    assert "feedback_for_generator" in schema["$defs"]["EvaluationResult"]["properties"]


def test_part_3_claims_each_proposal_once_oldest_first(engine, tmp_path):
    first = drafted(engine, tmp_path, "informal")
    second = drafted(engine, tmp_path, "formal")

    claims = [
        proposal_handoff.claim_next(engine, claimed_by="approval-1"),
        proposal_handoff.claim_next(engine, claimed_by="approval-2"),
        proposal_handoff.claim_next(engine, claimed_by="approval-3"),
    ]

    assert [c.proposal.ticket_id for c in claims[:2]] == [first, second]
    assert claims[2] is None
    assert store.get_ticket(engine, first).status == "waiting_for_approval"  # Part 3's to move


def test_the_detail_carries_each_sections_draft_and_evaluation(engine, tmp_path):
    ticket_id = drafted(engine, tmp_path, "formal")

    detail = store.get_ticket_detail(engine, ticket_id)

    assert detail["status"] == "waiting_for_approval"
    assert detail["proposal"]["contract_version"] == "rfp-proposal/1"
    assert detail["proposal"]["sla_department"] == "warehouse"
    assert detail["proposal"]["average_iterations_per_section"] == 1.0
    warehouse, reverse = detail["sections"]
    assert warehouse["is_sla_section"] is True and reverse["is_sla_section"] is False
    for section in (warehouse, reverse):
        assert section["draft_status"] == "passed" and not section["needs_human_review"]
        assert section["draft_iterations"] == 1 and section["max_iterations"] == 3
        assert section["draft_content"].startswith("## ")
        assert section["draft"]["price_lines"]
        assert section["evaluation"]["overall_pass"] is True
        assert section["knowledge_source"] == "local"
        assert section["approval_status"] == "pending" and section["ready_at"] is not None


def test_a_part_1_ticket_has_no_draft_fields_yet(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")

    detail = store.get_ticket_detail(engine, ticket_id)

    assert detail["proposal"] is None
    assert all(s["draft_status"] is None and s["evaluation"] is None for s in detail["sections"])


def test_the_sweep_closes_stalled_drafting_but_not_finished_proposals(engine, tmp_path):
    stalled = support.intake_complete(engine, tmp_path, "informal")
    handoff.claim(engine, stalled, claimed_by="dead-worker")  # drafting, then the process died
    done = drafted(engine, tmp_path, "formal")
    later = utcnow() + timedelta(hours=2)

    closed = store.sweep_abandoned(engine, older_than=timedelta(minutes=30), now=later)

    assert closed == [stalled]
    ticket = store.get_ticket(engine, stalled)
    assert ticket.status == "failed" and ticket.error_stage == "abandoned"
    assert ticket.error_message.startswith("the proposal drafting stopped making progress")
    assert store.get_ticket(engine, done).status == "waiting_for_approval"


def test_a_reprocessed_ticket_loses_its_part_2_rows(engine, tmp_path):
    ticket_id = drafted(engine, tmp_path, "informal")
    with Session(engine) as session:
        session.execute(
            update(RfpTicket).where(RfpTicket.id == ticket_id).values(status="failed")  # type: ignore[arg-type]
        )
        session.commit()

    store.reset_ticket(engine, ticket_id)

    assert proposal_handoff.load_proposal_handoff(engine, ticket_id) is None
    assert proposal_store.load_sections(engine, ticket_id) == []


def test_the_iterations_kpi_counts_finished_sections(engine, tmp_path):
    drafted(engine, tmp_path, "informal")
    drafted(
        engine,
        tmp_path,
        "formal",
        drafts={
            "warehouse": [support.good("warehouse")],
            "reverse": [support.bad_returns, support.good("reverse")],
        },
    )

    kpi = proposal_store.iteration_kpi(engine)

    assert kpi["sections"] == 4
    assert kpi["average_iterations_per_section"] == 1.25
    assert kpi["passed_first_attempt"] == 3
    assert kpi["needs_human_review"] == 0
