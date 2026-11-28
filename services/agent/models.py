"""Request and response contracts for `/agent`."""

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from services.agent.memory.store import Memory
from services.agent.tracing import GuardrailSummary, RunTrace, TraceEvent
from services.knowledge.models import MAX_QUESTION_LENGTH

# Only the transport limit: whether a question is usable (not empty) is the
# graph's decision, made in its `receive_question` node.
AgentQuestionText = Annotated[str, StringConstraints(max_length=MAX_QUESTION_LENGTH)]


class AgentQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: AgentQuestionText
    # The conversation to continue, as an earlier answer returned it; omitted, a new
    # one starts. A pending memory proposal lives in its conversation, so the reply
    # to "Want me to remember this?" must be sent with the same id.
    conversation_id: UUID | None = None


class MemoryProposalView(BaseModel):
    """A memory proposal waiting for the user's answer in this conversation."""

    proposal_id: str
    category: str
    content: str
    # The memory it would replace (same carrier and country, or client), if any.
    replaces_content: str | None = None


class MemoryDecisionView(BaseModel):
    """What this message did to the proposal that was pending."""

    proposal_id: str
    outcome: str
    reason: str
    memory_id: str | None = None


class AgentMemoryState(BaseModel):
    decision: MemoryDecisionView | None = None
    pending_proposal: MemoryProposalView | None = None


class AgentAnswer(BaseModel):
    """The answer, the run whose trace explains it, and the conversation to continue."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    conversation_id: str | None = None
    answer: str
    # Present when the message settled a proposal or a new one is waiting.
    memory: AgentMemoryState | None = None


class AgentMemoryEntry(BaseModel):
    """An approved memory, with who approved it and until when it holds."""

    memory_id: str
    category: str
    scope: str
    content: str
    approved_by: str
    approved_at: str
    valid_until: str

    @classmethod
    def from_memory(cls, memory: Memory) -> "AgentMemoryEntry":
        return cls(
            memory_id=memory.memory_id,
            category=memory.category,
            scope=memory.scope(),
            content=memory.content,
            approved_by=memory.approved_by,
            approved_at=memory.approved_at,
            valid_until=memory.valid_until,
        )


class AgentMemoryList(BaseModel):
    """The agent's team memory: every active entry, shared by all its users."""

    memories: list[AgentMemoryEntry]


class AgentTraceEvent(BaseModel):
    seq: int
    attempt: int
    kind: str
    name: str
    status: str
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    input: Any
    output: Any
    error_type: str | None
    error_message: str | None

    @classmethod
    def from_event(cls, event: TraceEvent) -> "AgentTraceEvent":
        return cls(
            seq=event.seq,
            attempt=event.attempt,
            kind=event.kind,
            name=event.name,
            status=event.status,
            started_at=event.started_at,
            finished_at=event.finished_at,
            duration_ms=event.duration_ms,
            input=event.input,
            output=event.output,
            error_type=event.error_type,
            error_message=event.error_message,
        )


class AgentRunTrace(BaseModel):
    """One run: how it ended and every node and routing decision, in order."""

    run_id: str
    conversation_id: str | None
    graph_version: str
    question: str
    status: str
    outcome: str | None
    answer: str | None
    attempts: int
    started_at: str
    finished_at: str | None
    error_node: str | None
    error_type: str | None
    # The sources consulted, in order: incident_manager, inventory_manager,
    # knowledge_base. Derived from `events`, so it can never disagree with them.
    sources: list[str]
    events: list[AgentTraceEvent]

    @classmethod
    def from_trace(cls, trace: RunTrace) -> "AgentRunTrace":
        return cls(
            run_id=trace.run_id,
            conversation_id=trace.conversation_id,
            graph_version=trace.graph_version,
            question=trace.question,
            status=trace.status,
            outcome=trace.outcome,
            answer=trace.answer,
            attempts=trace.attempts,
            started_at=trace.started_at,
            finished_at=trace.finished_at,
            error_node=trace.error_node,
            error_type=trace.error_type,
            sources=trace.sources(),
            events=[AgentTraceEvent.from_event(event) for event in trace.events],
        )


class GuardrailCountView(BaseModel):
    guardrail: str
    action: str
    failure_type: str
    count: int
    # In how many runs.
    runs: int


class GuardrailSummaryResponse(BaseModel):
    """How many times each guardrail was triggered in the window (SEC-114)."""

    since: str | None
    until: str | None
    conversation_id: str | None
    # Runs with at least one guardrail event, and the events in all.
    runs: int
    total: int
    by_failure_type: dict[str, int]
    by_guardrail: dict[str, int]
    counts: list[GuardrailCountView]

    @classmethod
    def from_summary(cls, summary: GuardrailSummary) -> "GuardrailSummaryResponse":
        return cls(
            since=summary.since,
            until=summary.until,
            conversation_id=summary.conversation_id,
            runs=summary.runs,
            total=summary.total,
            by_failure_type=summary.by("failure_type"),
            by_guardrail=summary.by("guardrail"),
            counts=[
                GuardrailCountView(
                    guardrail=c.guardrail,
                    action=c.action,
                    failure_type=c.failure_type,
                    count=c.count,
                    runs=c.runs,
                )
                for c in summary.counts
            ],
        )
