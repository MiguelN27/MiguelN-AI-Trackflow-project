"""Shared fixtures for the Part 3 tests: a ticket through Parts 1 and 2, and simulated owners.

No test reaches a model, Qdrant or PostgreSQL. A ticket goes through Part 1's
and Part 2's own graphs with their scripted agents (real guards, real §5
rules), so Part 3 starts from a real `rfp-proposal/1` handoff. Human decisions
are simulated through the same path the API uses: `runner.submit_decision`
records the decision, `runner.apply_decisions` resumes the graph with it
(`Command(resume=...)`). The checkpointer is LangGraph's `SqliteSaver`, in a
file next to the test's database.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from sqlalchemy.engine import Engine

from data.pipelines.rfp_intake import store
from data.pipelines.rfp_intake.approval import checkpoint
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import store as approval_store
from data.pipelines.rfp_intake.config import RfpIntakeSettings
from data.pipelines.rfp_intake.models import RfpDepartmentSection
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from tests.pipelines.rfp_intake.proposal import support as proposal_support
from tests.pipelines.rfp_intake.proposal.support import Draft, ScriptedProposalAgents

# The same accounts `tests/conftest.py` puts in RFP_APPROVERS.
OWNERS = {
    "warehouse": "ana.whitfield@trackflow.com",
    "lastmile": "carlos.vega@trackflow.com",
    "reverse": "sofia.ramos@trackflow.com",
}


def settings(**overrides: Any) -> RfpIntakeSettings:
    return proposal_support.settings(**overrides)


@dataclass
class Approval:
    """One ticket in Part 3, with its scripted agents and simulated owners."""

    engine: Engine
    ticket_id: UUID
    agents: ScriptedProposalAgents
    settings: RfpIntakeSettings
    statuses: list[str] = field(default_factory=list)

    def start(self) -> str | None:
        started = approval_runner.start_pending(
            self.engine, agents=self.agents.bundle(), settings=self.settings
        )
        assert [ticket for ticket, _ in started] == [self.ticket_id]
        return started[0][1]

    def requests(self) -> dict[str, dict[str, Any]]:
        """The open approval requests, by department."""
        return {
            request["department_id"]: request
            for _, request in approval_runner.open_requests(
                self.engine, self.ticket_id, self.settings
            )
        }

    def interrupt_ids(self) -> dict[str, str]:
        return {
            request["department_id"]: interrupt_id
            for interrupt_id, request in approval_runner.open_requests(
                self.engine, self.ticket_id, self.settings
            )
        }

    def submit(
        self,
        dept: str,
        action: str,
        comment: str | None = None,
        *,
        by: str | None = None,
        review_round: int | None = None,
    ) -> dict[str, Any]:
        current = self.requests().get(dept)
        round_ = review_round or (current["review_round"] if current else 1)
        return approval_runner.submit_decision(
            self.engine,
            self.ticket_id,
            dept,
            action=action,
            comment=comment,
            review_round=round_,
            decided_by_email=by or OWNERS[dept],
            settings=self.settings,
        )

    def apply(self) -> str | None:
        return approval_runner.apply_decisions(
            self.ticket_id, self.engine, agents=self.agents.bundle(), settings=self.settings
        )

    def decide(self, dept: str, action: str, comment: str | None = None) -> str | None:
        """A simulated owner decides, and the graph is resumed with it."""
        self.submit(dept, action, comment)
        return self.apply()

    def approve_all(self) -> str | None:
        for dept in self.requests():
            self.submit(dept, "approve")
        return self.apply()

    def sections(self) -> dict[str, RfpDepartmentSection]:
        return {
            row.department_id: row
            for row in approval_store.load_sections(self.engine, self.ticket_id)
        }

    def status(self) -> str:
        ticket = store.get_ticket(self.engine, self.ticket_id)
        assert ticket is not None
        return ticket.status

    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": checkpoint.thread_id(self.ticket_id)}}

    def snapshot(self) -> Any:
        return approval_runner.graph_for(self.engine, self.settings).get_state(self.config())

    def trace(self) -> list[dict[str, Any]]:
        """The graph state's trace, paused branches included."""
        return approval_runner.state_trace(self.engine, self.ticket_id, self.settings)

    def events(self, prefix: str = "") -> list[str]:
        detail = store.get_ticket_detail(self.engine, self.ticket_id)
        assert detail is not None
        return [event["node"] for event in detail["events"] if event["node"].startswith(prefix)]


def drafted(
    engine: Engine,
    tmp_path: Path,
    kind: str = "formal",
    *,
    drafts: dict[str, list[Draft]] | None = None,
    **overrides: Any,
) -> Approval:
    """A ticket through Parts 1 and 2: its proposal is queued for approval."""
    config = settings(**overrides)
    agents = ScriptedProposalAgents(drafts=drafts or proposal_support.all_good())
    ticket_id = proposal_support.intake_complete(engine, tmp_path, kind)
    proposal_runner.run_pending(engine, agents=agents.bundle(), settings=config)
    return Approval(engine=engine, ticket_id=ticket_id, agents=agents, settings=config)


def opened(
    engine: Engine,
    tmp_path: Path,
    kind: str = "formal",
    *,
    drafts: dict[str, list[Draft]] | None = None,
    **overrides: Any,
) -> Approval:
    """A ticket through Parts 1 and 2, with its approvals open (every branch paused)."""
    approval = drafted(engine, tmp_path, kind, drafts=drafts, **overrides)
    approval.start()
    return approval
