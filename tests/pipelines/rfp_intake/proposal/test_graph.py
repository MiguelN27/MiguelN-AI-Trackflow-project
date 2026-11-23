"""The Part 2 graphs end to end: scripted agents, real guards, rules, store and handoff (SQLite)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlmodel import Session, col

from data.pipelines.rfp_intake import conversion, handoff, store
from data.pipelines.rfp_intake.llm import IntakeModelError, InvalidModelReply
from data.pipelines.rfp_intake.models import RfpSectionDraft, RfpTicket, utcnow
from data.pipelines.rfp_intake.proposal import graph as proposal_graph
from data.pipelines.rfp_intake.proposal import handoff as proposal_handoff
from data.pipelines.rfp_intake.proposal import knowledge, runner
from data.pipelines.rfp_intake.proposal import store as proposal_store
from data.process.knowledge_base import KnowledgeBaseError
from tests.pipelines.rfp_intake.proposal import support


def draft(engine, agents, **settings):
    return runner.run_pending(engine, agents=agents.bundle(), settings=support.settings(**settings))


def sections_by_dept(engine, ticket_id):
    return {row.department_id: row for row in proposal_store.load_sections(engine, ticket_id)}


def attempts(engine, ticket_id, dept):
    with Session(engine) as session:
        return list(
            session.execute(
                select(RfpSectionDraft)
                .where(
                    col(RfpSectionDraft.ticket_id) == ticket_id,
                    col(RfpSectionDraft.department_id) == dept,
                )
                .order_by(col(RfpSectionDraft.iteration))
            ).scalars()
        )


# --- The happy path -----------------------------------------------------------------------


def test_luna_passes_first_time_and_waits_for_approval(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    seen: dict[str, set[str]] = {"generate": set(), "evaluate": set()}
    agents = support.ScriptedProposalAgents()
    original = agents.judge_relevance

    def watch_generate(dept):
        inner = agents._generator(dept)

        def generate(assignment, passages, previous):
            seen["generate"].add(support.ticket_status(engine, ticket_id))
            return inner(assignment, passages, previous)

        return generate

    def watch_relevance(assignment, draft_, markdown):
        seen["evaluate"].add(support.ticket_status(engine, ticket_id))
        return original(assignment, draft_, markdown)

    bundle = agents.bundle()
    bundle = type(bundle)(
        generators={dept: watch_generate(dept) for dept in ("warehouse", "lastmile")},
        judge_relevance=watch_relevance,
        review_compliance=bundle.review_compliance,
        retrieve_knowledge=bundle.retrieve_knowledge,
    )

    done = runner.run_pending(engine, agents=bundle, settings=support.settings())

    assert done == [(ticket_id, "waiting_for_approval")]
    assert "drafting" in seen["generate"]
    assert seen["evaluate"] == {"under_evaluation"}
    rows = sections_by_dept(engine, ticket_id)
    assert {dept: row.draft_status for dept, row in rows.items()} == {
        "warehouse": "passed",
        "lastmile": "passed",
    }
    assert all(
        row.draft_iterations == 1 and row.approval_status == "pending" for row in rows.values()
    )
    assert rows["lastmile"].draft_data["is_sla_section"] is True
    ticket = store.get_ticket(engine, ticket_id)
    assert ticket.current_stage is None and ticket.llm_tokens_used > 0


def test_the_part_3_handoff_has_every_departments_content_and_evaluation(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    draft(engine, support.ScriptedProposalAgents())

    proposal = proposal_handoff.load_proposal_handoff(engine, ticket_id)

    assert proposal is not None and proposal.status == "waiting_for_approval"
    assert proposal.currency == "USD" and proposal.sla_department == "lastmile"
    assert [s.department_id for s in proposal.sections] == ["warehouse", "lastmile"]
    for section in proposal.sections:
        assert section.draft_content.startswith(f"## {section.department_name}")
        assert section.evaluation.overall_pass is True
        assert section.approval_status == "pending"
        assert section.facts.currencies == ["USD"]
    lastmile = proposal.sections[1]
    assert lastmile.owner == "Carlos Vega" and lastmile.facts.on_time_sla_pct == 90
    assert "states the 90% on-time SLA" in lastmile.assignment_summary
    assert proposal.metrics.average_iterations_per_section == 1.0


def test_the_generators_never_touch_the_pdf_or_its_markdown(engine, tmp_path, monkeypatch):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    ticket = store.get_ticket(engine, ticket_id)
    (tmp_path / "pdf" / f"{ticket_id}.pdf").unlink()
    with Session(engine) as session:
        session.execute(update(RfpTicket).where(RfpTicket.id == ticket.id).values(markdown=None))  # type: ignore[arg-type]
        session.commit()

    def no_pdf(path):
        raise AssertionError("Part 2 must not re-ingest the PDF")

    monkeypatch.setattr(conversion, "pdf_to_markdown", no_pdf)

    assert draft(engine, support.ScriptedProposalAgents()) == [(ticket_id, "waiting_for_approval")]


# --- The generator-evaluator loop ---------------------------------------------------------


def test_a_failing_section_goes_back_with_the_feedback_and_then_passes(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    agents = support.ScriptedProposalAgents(
        drafts={
            "warehouse": [support.good("warehouse")],
            "reverse": [support.bad_returns, support.good("reverse")],
        }
    )

    assert draft(engine, agents) == [(ticket_id, "waiting_for_approval")]

    second = agents.calls["generate:reverse#2"][0]["previous"]
    assert second["iteration"] == 1
    assert "[returns-min-48h]" in second["feedback"]
    assert support.BAD_RETURNS_SENTENCE in second["feedback"]
    assert [a.overall_pass for a in attempts(engine, ticket_id, "reverse")] == [False, True]
    proposal = proposal_handoff.load_proposal_handoff(engine, ticket_id)
    assert proposal.metrics.average_iterations_per_section == 1.5
    assert proposal.sla_department == "warehouse" and proposal.currency == "EUR"


def test_at_the_iteration_limit_the_section_ships_for_human_review(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    agents = support.ScriptedProposalAgents(
        drafts={"warehouse": [support.good("warehouse")], "reverse": [support.bad_returns]}
    )

    assert draft(engine, agents, max_draft_iterations=2) == [(ticket_id, "needs_human_review")]

    reverse = sections_by_dept(engine, ticket_id)["reverse"]
    assert reverse.draft_status == "needs_human_review"
    assert reverse.draft_iterations == 2
    assert support.BAD_RETURNS_SENTENCE in reverse.draft_content  # the last draft is kept
    assert reverse.evaluation_results["compliance"]["pass"] is False
    assert reverse.evaluation_results["iteration"] == 2
    assert [a.overall_pass for a in attempts(engine, ticket_id, "reverse")] == [False, False]
    proposal = proposal_handoff.load_proposal_handoff(engine, ticket_id)
    shipped = {s.department_id: s for s in proposal.sections}
    assert set(shipped) == {"warehouse", "reverse"}  # never dropped
    assert shipped["reverse"].needs_human_review is True
    assert shipped["reverse"].evaluation.compliance.rule_ids == ["returns-min-48h"]
    assert shipped["warehouse"].draft_status == "passed"
    events = store.get_ticket_detail(engine, ticket_id)["events"]
    assert any(
        e["node"] == "escalate_section:reverse" and e["outcome"] == "warning" for e in events
    )


# --- Knowledge base -----------------------------------------------------------------------


def test_without_the_vector_database_the_generators_read_the_documents(
    engine, tmp_path, monkeypatch
):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")

    def qdrant_down(queries, top_k):
        raise KnowledgeBaseError("The vector database could not be searched")

    monkeypatch.setattr(knowledge, "search", qdrant_down)
    agents = support.ScriptedProposalAgents(knowledge_error=True)

    assert draft(engine, agents) == [(ticket_id, "waiting_for_approval")]
    lastmile = sections_by_dept(engine, ticket_id)["lastmile"]
    assert lastmile.draft_data["knowledge_source"] == "local"
    assert lastmile.draft_data["knowledge_documents"] == [
        "trackflow-carrier-coverage.en.md",
        "trackflow-sla-delivery.en.md",
    ]
    passages = agents.calls["generate:lastmile#1"][0]["passages"]
    assert any("UPS" in passage["text"] for passage in passages)


# --- Failures -----------------------------------------------------------------------------


def test_a_gateway_failure_before_any_evaluated_draft_fails_the_ticket_then_redraft_works(
    engine, tmp_path
):
    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    agents = support.ScriptedProposalAgents(
        fail={"generate:reverse#1": IntakeModelError("the model is unavailable (APITimeoutError)")}
    )

    assert draft(engine, agents) == [(ticket_id, "failed")]
    ticket = store.get_ticket(engine, ticket_id)
    assert ticket.error_stage == "generate_section:reverse#1"
    assert ticket.error_message == "the model is unavailable (APITimeoutError)"
    assert proposal_handoff.load_proposal_handoff(engine, ticket_id) is None
    assert sections_by_dept(engine, ticket_id)["reverse"].draft_status == "failed"

    status = runner.redraft(
        ticket_id,
        engine,
        agents=support.ScriptedProposalAgents().bundle(),
        settings=support.settings(),
    )

    assert status == "waiting_for_approval"
    assert store.get_ticket(engine, ticket_id).error_stage is None


def test_a_failure_after_an_evaluated_draft_ships_that_draft_for_review(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "formal")
    agents = support.ScriptedProposalAgents(
        drafts={"warehouse": [support.good("warehouse")], "reverse": [support.bad_returns]},
        fail={"generate:reverse#2": IntakeModelError("the model is unavailable (APITimeoutError)")},
    )

    assert draft(engine, agents) == [(ticket_id, "needs_human_review")]
    reverse = sections_by_dept(engine, ticket_id)["reverse"]
    assert reverse.draft_status == "needs_human_review" and reverse.draft_iterations == 1
    assert "Attempt 2 could not be completed" in reverse.draft_data["review_note"]
    shipped = proposal_handoff.load_proposal_handoff(engine, ticket_id).sections[1]
    assert shipped.review_note and shipped.evaluation.iteration == 1


def test_a_reply_that_breaks_the_schema_is_retried_once(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    agents = support.ScriptedProposalAgents(
        fail={"generate:warehouse#1": InvalidModelReply("the model did not call the tool")}
    )

    assert draft(engine, agents) == [(ticket_id, "waiting_for_approval")]
    assert sections_by_dept(engine, ticket_id)["warehouse"].draft_iterations == 1


def test_a_ticket_swept_mid_run_is_not_overwritten(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    agents = support.ScriptedProposalAgents()
    inner = agents._generator("lastmile")

    def sweep_then_generate(assignment, passages, previous):
        store.sweep_abandoned(engine, older_than=timedelta(0), now=utcnow() + timedelta(hours=1))
        return inner(assignment, passages, previous)

    bundle = agents.bundle()
    bundle = type(bundle)(
        generators={**bundle.generators, "lastmile": sweep_then_generate},
        judge_relevance=bundle.judge_relevance,
        review_compliance=bundle.review_compliance,
        retrieve_knowledge=bundle.retrieve_knowledge,
    )

    runner.run_pending(engine, agents=bundle, settings=support.settings())

    ticket = store.get_ticket(engine, ticket_id)
    assert ticket.status == "failed" and ticket.error_stage == "abandoned"
    assert "proposal" in ticket.error_message
    assert proposal_handoff.load_proposal_handoff(engine, ticket_id) is None


# --- The queue and the runner -------------------------------------------------------------


def test_the_queue_is_drained_oldest_first(engine, tmp_path):
    first = support.intake_complete(engine, tmp_path, "informal")
    second = support.intake_complete(engine, tmp_path, "formal")

    done = draft(engine, support.ScriptedProposalAgents())

    assert done == [(first, "waiting_for_approval"), (second, "waiting_for_approval")]
    assert draft(engine, support.ScriptedProposalAgents()) == []
    assert handoff.load_handoff(engine, first) is not None


def test_drafting_one_ticket_needs_a_pending_handoff(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    agents = support.ScriptedProposalAgents().bundle()

    assert runner.draft_ticket(ticket_id, engine, agents=agents, settings=support.settings()) == (
        "waiting_for_approval"
    )
    with pytest.raises(store.TicketStateError, match="no pending handoff"):
        runner.draft_ticket(ticket_id, engine, agents=agents, settings=support.settings())


def test_a_redraft_is_refused_once_part_3_has_claimed_the_proposal(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")
    draft(engine, support.ScriptedProposalAgents())
    assert proposal_handoff.claim_next(engine, claimed_by="approval-1") is not None

    with pytest.raises(store.TicketStateError, match="claimed for approval"):
        proposal_store.reset_for_redraft(engine, ticket_id, claimed_by="cli")


def test_a_ticket_still_in_intake_cannot_be_redrafted(engine, tmp_path):
    ticket_id = support.intake_complete(engine, tmp_path, "informal")

    with pytest.raises(store.TicketStateError, match="cannot be redrafted"):
        proposal_store.reset_for_redraft(engine, ticket_id, claimed_by="cli")


# --- Structure ----------------------------------------------------------------------------


def test_the_graphs_are_the_orchestrator_and_the_parallel_evaluator_loop():
    drawn = proposal_graph.draw_mermaid()

    outer, loop = drawn["rfp_proposal"], drawn["rfp_proposal_section"]
    assert "plan_assignments -.-> draft_section;" in outer  # Send, one per department
    assert "draft_section --> synthesize_proposal;" in outer
    for evaluator in ("check_readability", "check_relevance", "check_compliance"):
        assert f"generate_section --> {evaluator};" in loop  # in parallel
        assert f"{evaluator} --> merge_evaluation;" in loop
    for decision, target in (
        ("revise", "generate_section"),
        ("accept", "accept_section"),
        ("escalate", "escalate_section"),
    ):
        assert f"merge_evaluation -. &nbsp;{decision}&nbsp; .-> {target};" in loop
