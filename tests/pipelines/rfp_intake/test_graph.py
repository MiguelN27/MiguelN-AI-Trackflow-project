"""The whole `rfp_intake` graph on SQLite, with scripted agents (canned model outputs).

A run must always end `intake_complete`, `discarded` or `failed` - never
silently. The agents' own behaviour is tested in the other files; here it is
the routing, the fan-out, the persistence and the failure handling.
"""

import pytest

from data.pipelines.rfp_intake import graph, handoff, runner, store
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.llm import IntakeModelError, InvalidModelReply
from data.pipelines.rfp_intake.models import RfpHandoff
from tests.pipelines.rfp_intake.support import ScriptedAgents, new_ticket

SUCCESS_NODES = [
    "convert_to_markdown",
    "classify_document",
    "measure_readability",
    "extract_metadata",
    "plan_departments",
    "analyze_department:warehouse",
    "analyze_department:reverse",
    "synthesize_brief",
    "complete_intake",
]


@pytest.fixture
def run(engine, tmp_path):
    def start(kind: str = "formal", agents: ScriptedAgents | None = None, **settings):
        script = agents or ScriptedAgents(kind if kind != "garbage" else "formal")
        ticket_id = new_ticket(engine, tmp_path / "pdf", kind)
        status = runner.run_intake(
            ticket_id,
            engine,
            agents=script.bundle(),
            settings=RfpIntakeSettings(_env_file=None, **settings),  # type: ignore[call-arg]
        )
        return ticket_id, status, store.get_ticket_detail(engine, ticket_id), script

    return start


def completed_nodes(detail) -> list[str]:
    return [event["node"] for event in detail["events"] if event["outcome"] == "completed"]


def test_the_formal_rfp_reaches_intake_complete(run):
    ticket_id, status, detail, _ = run("formal")

    assert status == "intake_complete"
    assert detail["current_stage"] is None
    assert detail["completed_at"] is not None
    assert detail["markdown_chars"] > 1000
    assert detail["classification"]["document_kind"] == "formal_rfp"
    meta = detail["metadata"]
    assert (meta["client_name"], meta["client_country"], meta["currency"]) == (
        "ModaViva, S.L.",
        "ES",
        "EUR",
    )
    assert meta["monthly_volume"] is None
    assert meta["readability"]["word_count"] > 150
    assert [s["department_id"] for s in detail["sections"]] == ["warehouse", "reverse"]
    assert detail["sections"][0]["owner"] == "Ana Whitfield"
    assert detail["sections"][0]["key_aspects"][0]["aspect"] == "Storage volume"
    assert detail["sales_brief"]["departments"][1]["owner"] == "Sofía Ramos"
    assert detail["handoff"]["status"] == "pending"
    assert sorted(completed_nodes(detail)) == sorted(SUCCESS_NODES)
    # 5 agent calls before the fan-out... 1 classify + 1 metadata + 1 plan + 2 workers + 1 brief.
    assert detail["llm_tokens_used"] == 6 * 120


def test_the_informal_rfp_goes_to_warehouse_and_lastmile(run):
    _, status, detail, _ = run("informal")

    assert status == "intake_complete"
    assert [s["department_id"] for s in detail["sections"]] == ["warehouse", "lastmile"]
    assert detail["metadata"]["currency"] == "USD"
    assert detail["metadata"]["monthly_volume"] == 5000


def test_the_carrier_pitch_is_discarded_and_the_flow_stops(run):
    _, status, detail, script = run("invalid")

    assert status == "discarded"
    assert "carrier or vendor" in detail["discard_reason"]
    assert detail["metadata"] is None
    assert detail["sections"] == []
    assert detail["handoff"] is None
    assert set(script.calls) == {"classify_document"}
    assert completed_nodes(detail) == ["convert_to_markdown", "classify_document", "discard_ticket"]


def test_each_worker_receives_metadata_and_its_own_extracts_only(run):
    _, _, _, script = run("formal")

    tasks = {task["department_id"]: task for task in script.calls["analyze_department"]}
    assert set(tasks) == {"warehouse", "reverse"}
    for task in tasks.values():
        assert set(task) == {"ticket_id", "department_id", "objective", "extracts", "metadata"}
    assert tasks["warehouse"]["extracts"][0].startswith("Almacenamiento de aproximadamente")
    assert all("devoluciones" not in extract for extract in tasks["warehouse"]["extracts"])
    assert tasks["reverse"]["extracts"] == [
        "Gestión de devoluciones: inspección, reacondicionamiento y restock"
    ]


def test_the_handoff_carries_everything_part_2_needs(run, engine):
    ticket_id, _, _, _ = run("formal")

    payload = handoff.load_handoff(engine, ticket_id)

    assert payload is not None
    assert payload.ticket_id == ticket_id
    assert payload.next_stage == "drafting"
    assert payload.client.currency == "EUR"
    assert [d.department_id for d in payload.departments] == ["warehouse", "reverse"]
    assert payload.departments[0].key_aspects[0].source_quote.startswith("Almacenamiento")
    assert payload.departments[0].ask
    assert payload.source.markdown_chars > 1000


def test_a_model_failure_marks_the_ticket_failed_at_its_stage(run):
    script = ScriptedAgents(
        "formal",
        fail={"extract_metadata": IntakeModelError("the model is unavailable (APITimeoutError)")},
    )

    _, status, detail, _ = run("formal", agents=script)

    assert status == "failed"
    assert detail["error_stage"] == "extract_metadata"
    assert detail["error_message"] == "the model is unavailable (APITimeoutError)"
    assert detail["sections"] == []
    failed = [e for e in detail["events"] if e["outcome"] == "failed"]
    assert failed[0]["node"] == "extract_metadata"
    assert failed[0]["detail"]["error"] == "IntakeModelError"


def test_a_worker_failure_names_its_department(run):
    script = ScriptedAgents("formal", fail={"analyze_department": IntakeModelError("boom")})

    _, status, detail, _ = run("formal", agents=script)

    assert status == "failed"
    assert detail["error_stage"].startswith("analyze_department:")
    assert detail["handoff"] is None


def test_an_unexpected_error_is_recorded_without_its_message(run):
    script = ScriptedAgents(
        "formal", fail={"synthesize_brief": KeyError("postgresql://user:secret@host")}
    )

    _, status, detail, _ = run("formal", agents=script)

    assert status == "failed"
    assert detail["error_stage"] == "synthesize_brief"
    assert detail["error_message"] == "unexpected KeyError"
    assert "secret" not in str(detail["events"])


def test_an_invalid_model_reply_gets_one_more_attempt(run):
    script = ScriptedAgents(
        "formal", fail={"classify_document": InvalidModelReply("the model did not call it")}
    )

    _, status, detail, _ = run("formal", agents=script)

    assert status == "intake_complete"
    assert len(script.calls["classify_document"]) == 2
    assert [e["outcome"] for e in detail["events"] if e["node"] == "classify_document"] == [
        "failed",
        "completed",
    ]


def test_an_unreadable_pdf_fails_at_conversion(run):
    _, status, detail, script = run("garbage")

    assert status == "failed"
    assert detail["error_stage"] == "convert_to_markdown"
    assert detail["error_message"].startswith("the PDF could not be converted")
    assert script.calls == {}


def test_an_rfp_for_no_trackflow_service_is_discarded_after_planning(run, monkeypatch):
    from tests.pipelines.rfp_intake import support

    monkeypatch.setitem(
        support.EXTRACTED_METADATA,
        "formal",
        support.EXTRACTED_METADATA["formal"].model_copy(update={"services_requested": []}),
    )

    _, status, detail, script = run("formal")

    assert status == "discarded"
    assert detail["discard_reason"] == graph.NO_SCOPE_REASON
    assert "analyze_department" not in script.calls


def test_a_long_document_is_truncated_before_the_agents_read_it(run):
    _, status, detail, script = run("formal", max_document_chars=1000)

    assert status == "intake_complete"
    assert detail["markdown_truncated"] is True
    assert "truncated by TrackFlow" in script.calls["classify_document"][0]


def test_a_ticket_closed_mid_run_stops_the_run(run, engine):
    script = ScriptedAgents("formal")
    original = script.classify_document

    def classify_then_get_swept(markdown):
        reply = original(markdown)
        ticket = store.list_tickets(engine)[0]
        store.fail(engine, ticket["ticket_id"], stage="abandoned", message="swept")
        return reply

    script.classify_document = classify_then_get_swept  # type: ignore[method-assign]

    _, status, detail, _ = run("formal", agents=script)

    assert status == "failed"
    assert detail["error_stage"] == "abandoned"
    assert "extract_metadata" not in script.calls


def test_reprocess_runs_a_failed_ticket_again_from_the_pdf(run, engine):
    script = ScriptedAgents("formal", fail={"plan_departments": IntakeModelError("down")})
    ticket_id, status, _, _ = run("formal", agents=script)
    assert status == "failed"

    status = runner.reprocess(ticket_id, engine, agents=script.bundle())

    detail = store.get_ticket_detail(engine, ticket_id)
    assert status == "intake_complete"
    assert detail["error_stage"] is None
    assert [s["department_id"] for s in detail["sections"]] == ["warehouse", "reverse"]
    assert "reprocess" in [event["node"] for event in detail["events"]]


def test_a_completed_ticket_cannot_be_reprocessed(run, engine):
    ticket_id, _, _, _ = run("formal")

    with pytest.raises(store.TicketStateError, match="cannot be reprocessed"):
        runner.reprocess(ticket_id, engine)
    with pytest.raises(store.TicketNotFound):
        store.reset_ticket(engine, "00000000-0000-4000-8000-000000000000")
    from sqlmodel import Session, select

    with Session(engine) as session:
        assert len(session.exec(select(RfpHandoff)).all()) == 1


def test_the_graph_is_the_dedicated_rfp_intake_graph():
    compiled = graph.build_intake_graph()

    assert compiled.name == "rfp_intake"
    nodes = set(compiled.get_graph().nodes)
    assert {"plan_departments", "analyze_department", "synthesize_brief"} <= nodes
    # No CX / knowledge-agent node was bolted on.
    assert not nodes & {"retrieve", "generate_answer", "guard_input", "recall_memory"}
