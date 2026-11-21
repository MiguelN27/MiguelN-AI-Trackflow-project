"""`/agent`: the knowledge agent graph over HTTP. No business logic lives here.

- `POST /agent/query` hands the question to `runner.run_agent()` and translates
  how the run ended into a status code. Whether the question is usable, what
  to retrieve, whether to generate: the graph decides all of it.
- `GET /agent/runs/{run_id}` returns that run's trace (nodes in order, what each
  produced, the routing decisions and their reasons) to the user who ran it.
- `GET /agent/memory` lists the team memory (MEM-092): every active entry, with
  who approved it and until when it holds, so nobody's memory changes unseen.

A question may continue a conversation (`conversation_id`): that is how the
user answers a pending "Want me to remember this?" proposal. Another user's
conversation, like an unknown one, is a 404.

Sessions (SEC-114) follow the account's role (`guardrails.session`): admin and
manager accounts are TrackFlow staff, every other account is a customer, held
to its own orders and to the knowledge base. A message the harness refuses is
answered with 200 and the refusal: the refusal is the answer. What only staff
may read - the team memory, run traces (the guards' verdicts would teach a
customer how to word around them) and `GET /agent/guardrails/summary` (how
many times each guardrail was triggered) - is a 403 for a customer.

Coexists with `POST /knowledge/query`, which stays as it was. Both require a
bearer token: every question costs model calls, and questions may carry
client details.
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from services.agent import runner
from services.agent.errors import AgentRunError
from services.agent.guardrails.session import Session, audience_for_role
from services.agent.memory.store import ConversationNotFound, get_memory_store
from services.agent.models import (
    AgentAnswer,
    AgentMemoryEntry,
    AgentMemoryList,
    AgentMemoryState,
    AgentQuestion,
    AgentRunTrace,
    GuardrailSummaryResponse,
)
from services.agent.tracing import get_trace_store, utc_iso
from services.auth.dependencies import CurrentUser
from services.users.models import UserInDB

logger = logging.getLogger("api.agent")

router = APIRouter(prefix="/agent", tags=["agent"])

RUN_NOT_FOUND = "Agent run not found"
CONVERSATION_NOT_FOUND = "Conversation not found"
STAFF_ONLY = "Only TrackFlow staff can read this"


def session_of(user: UserInDB) -> Session:
    """The agent session of a signed-in account: staff or customer, by its role."""
    return Session(audience=audience_for_role(user.role), email=user.email)


def require_staff(user: UserInDB) -> None:
    if session_of(user).is_customer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, STAFF_ONLY)


@router.post("/query", response_model=AgentAnswer)
def query_agent(body: AgentQuestion, user: CurrentUser) -> AgentAnswer:
    # A sync route: FastAPI runs it in its thread pool, so the blocking Qdrant
    # and model calls inside the graph never stall the event loop.
    try:
        result = runner.run_agent(
            body.question,
            user_id=user.id,
            conversation_id=None if body.conversation_id is None else str(body.conversation_id),
            session=session_of(user),
        )
    except ConversationNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, CONVERSATION_NOT_FOUND) from error
    except AgentRunError as error:
        # The trace keeps the exception class; the caller gets the failed step
        # and the run id to quote, never the exception text or a traceback.
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if error.retryable
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise HTTPException(
            code, {"message": error.public_message, "run_id": error.run_id}
        ) from error
    if result.outcome == "rejected" or result.answer is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "message": result.error or "The question could not be processed.",
                "run_id": result.run_id,
            },
        )
    return AgentAnswer(
        run_id=result.run_id,
        conversation_id=result.conversation_id,
        answer=result.answer,
        memory=None if result.memory is None else AgentMemoryState.model_validate(result.memory),
    )


@router.get("/memory", response_model=AgentMemoryList)
def list_agent_memory(user: CurrentUser) -> AgentMemoryList:
    # Shared by every staff user of the agent; it holds client preferences, so
    # customers may not read it (SEC-114).
    require_staff(user)
    memories = get_memory_store().active_memories()
    return AgentMemoryList(memories=[AgentMemoryEntry.from_memory(m) for m in memories])


@router.get("/guardrails/summary", response_model=GuardrailSummaryResponse)
def guardrail_summary(
    user: CurrentUser,
    since: datetime | None = Query(None, description="Only events at or after this time"),
    until: datetime | None = Query(None, description="Only events before this time"),
    conversation_id: UUID | None = Query(None, description="Only this conversation's events"),
) -> GuardrailSummaryResponse:
    """How many times each guardrail was triggered: by guardrail, action and failure type."""
    require_staff(user)
    summary = get_trace_store().guardrail_summary(
        since=None if since is None else utc_iso(since),
        until=None if until is None else utc_iso(until),
        conversation_id=None if conversation_id is None else str(conversation_id),
    )
    return GuardrailSummaryResponse.from_summary(summary)


@router.get("/runs/{run_id}", response_model=AgentRunTrace)
def get_agent_run(run_id: UUID, user: CurrentUser) -> AgentRunTrace:
    require_staff(user)
    # Another user's run is reported as missing, not forbidden: whether a run
    # id exists is not something to reveal.
    trace = get_trace_store().get_trace(str(run_id), owner=user.id)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND)
    return AgentRunTrace.from_trace(trace)
