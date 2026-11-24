"""Traceability: for any run, which agent did what, with what input and output, and when."""

from data.pipelines.rfp_intake.approval import trace
from tests.pipelines.rfp_intake.approval import support

ENTRY_FIELDS = {
    "part",
    "node",
    "agent",
    "department",
    "input",
    "output",
    "outcome",
    "started_at",
    "finished_at",
}


def test_every_part_3_node_execution_is_in_the_state_with_agent_input_output_and_times(
    engine, tmp_path
):
    approval = support.opened(engine, tmp_path, "formal")
    approval.decide("reverse", "request_changes", "Name the return window.")
    approval.approve_all()

    entries = approval.trace()
    assert entries
    for entry in entries:
        assert set(entry) == ENTRY_FIELDS
        assert entry["agent"]
        assert entry["started_at"] <= entry["finished_at"]
    nodes = [entry["node"] for entry in entries]
    for expected in (
        "open_approval",
        "arbitrate",
        "open_review:reverse#1",
        "await_decision:reverse#1",
        "validate_decision:reverse#1",
        "start_revision:reverse#1",
        "revise_section:reverse#1",
        "open_review:reverse#2",
        "sign_off:reverse#2",
        "sign_off:warehouse#1",
        "compose_final_document",
        "complete_approval",
    ):
        assert expected in nodes
    # The human step names the owner as its agent, and what they answered.
    human = next(e for e in entries if e["node"] == "await_decision:reverse#1")
    assert human["agent"] == "Sofía Ramos (owner of Reverse Logistics)"
    assert human["output"]["action"] == "request_changes"
    # Every node that runs inside `_run` is also an event row (await_decision is state-only).
    events = set(approval.events())
    assert {n for n in nodes if not n.startswith("await_decision")} <= events


def test_the_ticket_trace_covers_the_three_parts_in_order(engine, tmp_path):
    approval = support.opened(engine, tmp_path, "formal")
    approval.decide("reverse", "request_changes", "Name the return window.")
    approval.approve_all()

    steps = trace.ticket_trace(engine, approval.ticket_id)

    assert steps[0]["step"] == "convert_to_markdown"
    assert steps[0]["agent"] == "PDF converter (MarkItDown)"
    parts = [step["part"] for step in steps]
    assert parts == sorted(parts)  # 1..1, 2..2, 3..3: one process, no going back
    assert {1, 2, 3} <= set(parts)
    worker = next(s for s in steps if s["step"] == "analyze_department")
    assert worker["agent"] == f"{worker['department']} worker agent"
    revision = [s for s in steps if s["part"] == 3 and s["step"] == "generate_section"]
    assert revision and revision[0]["agent"] == "reverse generator agent (revision)"
    assert steps[-1]["step"] == "complete_approval"
    assert [s["position"] for s in steps] == list(range(1, len(steps) + 1))
