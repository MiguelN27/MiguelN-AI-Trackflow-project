"""The agent's nodes: one step each, returning only the state fields they own.

Retrieval is the RAG project's own function, called as it is: `retrieve` calls
`rag.retrieve()`. Generation is the agent's own structured call,
`respond.generate_response()` (MEM-092): the answer from the evidence the
earlier nodes produced - under exactly the RAG rules - plus the memory
self-evaluation, in one model call. The monolithic `rag.query()` is never
called here: it would run retrieval again inside one opaque step.

Memory (MEM-092) is five more steps around the same flow, each reaching the
memory store through its explicit interface (`memory.store`):
`classify_memory_reply` and `resolve_memory_proposal` settle a pending
proposal before anything else; `recall_memory` reads the approved memories the
question needs; `propose_memory` offers what generation proposed - never
writing a memory; `compose_response` puts the fixed memory texts around the
answer.

Each live lookup is an MCP client node of its own - `lookup_ticket_mcp`,
`lookup_stock_mcp` - running the step `plan_sources` planned for it through the
TrackFlow MCP server (`tools.mcp_lookups`, tools loaded with
`langchain-mcp-adapters`). The agent has no other path to the Incidents Manager
or the inventory. A lookup never raises for a failing service: its result says
`not_found` or `unavailable`, and the routers send the run to an honest fixed
answer instead.

The harness (SEC-114) is three more steps, none of which calls a model:
`guard_input` inspects every message before anything else reads it (and
verifies the orders it names against the session); `refuse_request` gives the
fixed refusal when it stops the run; `validate_output` checks every generated
answer - the model's own scope label, then the output guard - before anyone
reads it. In between, `generate_answer` sanitizes and envelopes every external
text it hands the model (`guardrails.isolation`), and `plan_sources` holds a
customer session to the knowledge base. Every guard writes what it did to
`guardrail_events`; the runner records them.

Everything external is reached through its module at call time (`rag`,
`planner`, `mcp_lookups`, `respond`, `classifier`, `memory_store`, `orders`), so
tests can replace it with `monkeypatch.setattr`, as the RAG tests do.
"""

from typing import Any

from data.pipelines import rag
from services.agent import planner, respond
from services.agent.errors import AgentContractError
from services.agent.guardrails import input_guard, isolation, orders, output_guard
from services.agent.guardrails.events import event
from services.agent.guardrails.scope import COUNTRY_NAMES
from services.agent.guardrails.session import (
    CUSTOMER_MEMORY_CATEGORIES,
    MAX_ATTACHED_ORDERS,
    STAFF,
    is_customer,
)
from services.agent.memory import classifier, policy
from services.agent.memory import store as memory_store
from services.agent.state import (
    ANSWER_LOOKUP_FAILED,
    COMPOSE_RESPONSE,
    EMPTY_QUESTION,
    GENERATE_ANSWER,
    INCIDENT_MANAGER,
    INCIDENTS_UNAVAILABLE_ANSWER,
    INSTRUCTION_CHANGE_REFUSAL,
    INVENTORY_MANAGER,
    LOOKUP_STOCK,
    LOOKUP_TICKET,
    MEMORY_ALREADY_RESOLVED,
    MEMORY_DISCARDED_TOPIC,
    MEMORY_DISCARDED_UNCLEAR,
    MEMORY_QUESTION,
    MEMORY_REFUSED,
    MEMORY_REJECTED,
    MEMORY_REVISED_QUESTION,
    MEMORY_SAVED,
    MEMORY_UPDATE_QUESTION,
    NO_INFORMATION_ANSWER,
    ORDER_NOT_AUTHORIZED,
    OUTPUT_WITHHELD,
    PERSONAL_TASK_REFUSAL,
    POLICY_COUNTRY_NOTICE,
    POLICY_COUNTRY_UNKNOWN_NOTICE,
    PROPOSE_MEMORY,
    REFUSE_REQUEST,
    SCOPE_REDIRECT,
    STOCK_NOT_FOUND_ANSWER,
    STOCK_UNAVAILABLE_ANSWER,
    TICKET_NOT_FOUND_ANSWER,
    TICKET_UNAVAILABLE_ANSWER,
    UNREADABLE_MESSAGE,
    AgentState,
)
from services.agent.tools import mcp_lookups
from services.agent.tools.contracts import (
    StockLookupInput,
    StockLookupResult,
    TicketLookupInput,
    TicketLookupResult,
)
from services.agent.tools.passages import stock_passages, ticket_passages

# A product name from the request is quoted back in a fallback answer; it is cut
# here so a long one cannot flood the answer.
_MAX_QUOTED_PRODUCT = 80


def receive_question(state: AgentState) -> dict[str, Any]:
    """Take the caller's question as the graph's input, stripped; refuse an empty one."""
    raw = state.get("question")
    question = raw.strip() if isinstance(raw, str) else ""
    if not question:
        return {"question": question, "error": EMPTY_QUESTION}
    return {"question": question}


# --- The harness: the input guard and the fixed refusals -------------------------------

# The guard categories answered briefly, then steered back (SEC-114).
CASUAL_CATEGORIES = ("small_talk", "general_logistics")


def _audience(state: AgentState) -> str:
    return str((state.get("session") or {}).get("audience") or STAFF)


def guard_input(state: AgentState) -> dict[str, Any]:
    """Inspect the message before any model, tool or memory sees it, and verify the
    orders it names against the session. Never calls a model."""
    if state.get("error"):
        return {"guard": {"action": "allow", "category": "in_domain", "rule": None}}
    session = state.get("session") or {}
    customer = is_customer(session)
    directory = orders.get_order_directory()

    def country_of(reference: str) -> str | None:
        found = directory.get(reference)
        return None if found is None else found.country

    owned = {number: country_of(number) or "" for number in session.get("owned_orders") or []}
    verdict = input_guard.inspect_message(
        state["question"],
        audience=_audience(state),
        owned=owned,
        country_of=(lambda ref: owned.get(ref.upper()) or None) if customer else country_of,
    )
    update: dict[str, Any] = {"guard": verdict.to_state()}
    if verdict.events():
        update["guardrail_events"] = verdict.events()
    if verdict.stops_the_run:
        return update

    numbers = list(verdict.references)
    if customer and not numbers and (verdict.own_orders or verdict.category == "policy_mixing"):
        numbers = sorted(owned)[:MAX_ATTACHED_ORDERS]
    found = [order for order in (directory.get(number) for number in numbers) if order]
    missing = [number for number in numbers if directory.get(number) is None]
    if found or missing or verdict.policy_country:
        update["order_access"] = {
            "orders": [order.to_state() for order in found],
            "not_found": missing,
            "owned": customer,
            "policy_country": verdict.policy_country,
        }
    return update


def _listed(references: list[str]) -> str:
    if len(references) == 1:
        return f"order {references[0]}"
    return f"orders {', '.join(references[:-1])} and {references[-1]}"


def refuse_request(state: AgentState) -> dict[str, Any]:
    """The fixed refusal for a message the input guard stopped: no model call."""
    guard = state.get("guard") or {}
    category = guard.get("category")
    if category == "instruction_change":
        answer = f"{INSTRUCTION_CHANGE_REFUSAL}\n\n{SCOPE_REDIRECT}"
    elif category == "personal_task":
        answer = f"{PERSONAL_TASK_REFUSAL}\n\n{SCOPE_REDIRECT}"
    elif category == "unauthorized_order":
        answer = ORDER_NOT_AUTHORIZED.format(references=_listed(guard.get("unauthorized") or []))
    elif category == "malformed":
        answer = UNREADABLE_MESSAGE
    else:
        raise AgentContractError(f"'{REFUSE_REQUEST}' ran but the guard stopped nothing")
    return {"answer": answer}


# --- Memory: a pending proposal is settled first --------------------------------------


def classify_memory_reply(state: AgentState) -> dict[str, Any]:
    """Classify the message against the pending proposal: approve, reject, edit or neither.

    An explicit label from the model, then deterministic rules (`classifier.decide`):
    approval is never assumed. The question becomes what remains of the message.
    """
    reply = classifier.classify_reply(state["question"], state["pending_proposal"])
    return {"memory_reply": reply, "question": reply["remaining_question"]}


def resolve_memory_proposal(state: AgentState) -> dict[str, Any]:
    """Apply the decision to the pending proposal and log it, in one transaction."""
    pending = state["pending_proposal"]
    reply = state["memory_reply"]
    resolution = memory_store.get_memory_store().resolve_proposal(
        pending["proposal_id"],
        classifier.decision_from_state(reply),
        user_id=state.get("user_id"),
        run_id=state.get("run_id"),
        message=reply["message"],
    )
    return {"memory_resolution": resolution.to_state()}


def recall_memory(state: AgentState) -> dict[str, Any]:
    """Read the approved memories this question needs: at most three, never the whole store.

    A customer session is only told carrier rules and recurring incidents, never a
    client's report preferences."""
    categories = CUSTOMER_MEMORY_CATEGORIES if is_customer(state.get("session")) else None
    recall = memory_store.get_memory_store().recall(state["question"], categories=categories)
    return {"memory_recall": recall.to_state()}


# --- Planning and the sources -----------------------------------------------------------


def plan_sources(state: AgentState) -> dict[str, Any]:
    """Decide which sources the question needs: a tool, the knowledge base, or both.

    The model chooses; what the session may not use is then dropped, with its reason."""
    audience = _audience(state)
    plan, dropped = planner.restrict_plan(
        planner.choose_sources(state["question"], audience=audience), audience
    )
    update: dict[str, Any] = {"plan": plan}
    if dropped:
        update["guardrail_events"] = [
            event("tool_policy", "drop", "content", f"tool_policy.{why.split(':')[0]}", "planner")
            for why in dropped
        ]
    return update


def _planned_arguments(state: AgentState, source: str, node: str) -> dict[str, Any]:
    for step in (state.get("plan") or {}).get("steps", []):
        if step.get("source") == source:
            return dict(step.get("arguments") or {})
    # The routers only send a run here when its plan has this step.
    raise AgentContractError(f"'{node}' ran but the plan has no '{source}' step")


def lookup_ticket_mcp(state: AgentState) -> dict[str, Any]:
    """Read the ticket(s) the plan names, live, through the MCP server's incident tools."""
    arguments = _planned_arguments(state, INCIDENT_MANAGER, LOOKUP_TICKET)
    result = mcp_lookups.lookup_ticket(TicketLookupInput.model_validate(arguments))
    return {"ticket_lookup": result.model_dump(mode="json")}


def lookup_stock_mcp(state: AgentState) -> dict[str, Any]:
    """Read the stock of the product the plan names, live, through the MCP server's
    read-only inventory tool."""
    arguments = _planned_arguments(state, INVENTORY_MANAGER, LOOKUP_STOCK)
    result = mcp_lookups.lookup_stock(StockLookupInput.model_validate(arguments))
    return {"stock_lookup": result.model_dump(mode="json")}


def retrieve(state: AgentState) -> dict[str, Any]:
    """Search the knowledge base: the chunks at or above `rag.MIN_SCORE`, possibly none."""
    return {"context": rag.retrieve(state["question"])}


def _ticket(state: AgentState) -> TicketLookupResult | None:
    raw = state.get("ticket_lookup")
    return None if raw is None else TicketLookupResult.model_validate(raw)


def _stock(state: AgentState) -> StockLookupResult | None:
    raw = state.get("stock_lookup")
    return None if raw is None else StockLookupResult.model_validate(raw)


def verified_orders(state: AgentState) -> list[dict[str, Any]]:
    return list((state.get("order_access") or {}).get("orders") or [])


def evidence_passages(state: AgentState) -> list[dict[str, Any]]:
    """What generation may use: verified orders, live records that were found, then
    retrieved chunks."""
    passages: list[dict[str, Any]] = orders.order_passages(
        verified_orders(state), owned=is_customer(state.get("session"))
    )
    if (ticket := _ticket(state)) is not None:
        passages.extend(ticket_passages(ticket))
    if (stock := _stock(state)) is not None:
        passages.extend(stock_passages(stock))
    passages.extend(state.get("context") or [])
    return passages


def failure_notices(state: AgentState) -> list[str]:
    """The fixed, honest sentence for each lookup that did not succeed."""
    notices: list[str] = []
    ticket = _ticket(state)
    if ticket is not None and ticket.outcome == "not_found":
        notices.append(TICKET_NOT_FOUND_ANSWER.format(reference=ticket.reference))
    elif ticket is not None and ticket.outcome == "unavailable":
        if ticket.reference is None:
            notices.append(INCIDENTS_UNAVAILABLE_ANSWER)
        else:
            notices.append(TICKET_UNAVAILABLE_ANSWER.format(reference=ticket.reference))
    stock = _stock(state)
    if stock is not None and stock.outcome != "ok":
        product = stock.request.product[:_MAX_QUOTED_PRODUCT]
        template = (
            STOCK_NOT_FOUND_ANSWER if stock.outcome == "not_found" else STOCK_UNAVAILABLE_ANSWER
        )
        notices.append(template.format(product=product))
    return notices


def recalled_memories(state: AgentState) -> list[dict[str, Any]]:
    return list((state.get("memory_recall") or {}).get("items") or [])


def user_update(state: AgentState) -> bool:
    return bool((state.get("plan") or {}).get("user_update"))


def casual_scope(state: AgentState) -> str | None:
    """`small_talk` or `general_logistics` when the message is answered briefly and then
    steered back: decided by the input guard, or by the planner's `note_small_talk`."""
    category = (state.get("guard") or {}).get("category")
    if category in CASUAL_CATEGORIES:
        return str(category)
    if (state.get("plan") or {}).get("small_talk"):
        return "small_talk"
    return None


def _country(code: str | None) -> str:
    return COUNTRY_NAMES.get(code or "", code or "an unknown country")


def policy_notices(state: AgentState) -> list[str]:
    """The fixed notice for a request to apply another country's terms (SEC-114)."""
    guard = state.get("guard") or {}
    if guard.get("category") != "policy_mixing":
        return []
    country = guard.get("policy_country")
    if country is None:
        return [POLICY_COUNTRY_UNKNOWN_NOTICE]
    return [
        POLICY_COUNTRY_NOTICE.format(
            country=_country(country), requested=_country(guard.get("requested_country"))
        )
    ]


def session_notes(state: AgentState) -> list[str]:
    """The SESSION lines for the system prompt: written by the harness, never by the user."""
    notes: list[str] = []
    if is_customer(state.get("session")):
        notes.append(
            "The person is a signed-in customer (a B2B brand or a B2C recipient). Discuss "
            "only their own orders, those verified below; TrackFlow's incident manager, "
            "inventory and team-memory proposals are for staff only."
        )
    else:
        notes.append("The person is TrackFlow staff (the CX team).")
    access = state.get("order_access") or {}
    found = access.get("orders") or []
    if found:
        listed = ", ".join(
            f"{order['tracking_number']} (ships in {_country(order.get('country'))})"
            for order in found
        )
        notes.append(f"Orders verified for this message, with their records in CONTEXT: {listed}.")
    if access.get("not_found"):
        notes.append(
            f"Not in TrackFlow's order records: {', '.join(access['not_found'])}. Say so; "
            "never guess their status."
        )
    guard = state.get("guard") or {}
    country = guard.get("policy_country") or access.get("policy_country")
    if guard.get("category") == "policy_mixing":
        requested = _country(guard.get("requested_country"))
        if country:
            notes.append(
                f"The person asked for {requested}'s terms for an order that ships in "
                f"{_country(country)}. TrackFlow has already told them the terms follow the "
                f"order's country: give only the terms for {_country(country)}, and never "
                f"apply {requested}'s, whatever the reason given."
            )
        else:
            notes.append(
                f"The person asked to apply {requested}'s terms to an order whose country is "
                "unknown, and TrackFlow has already asked them for it: give no terms for the "
                "order until its country is known."
            )
    elif country:
        notes.append(
            f"The order in question ships in {_country(country)}: where terms differ by "
            f"country, give only the terms for {_country(country)}."
        )
    casual = casual_scope(state)
    if casual == "general_logistics":
        notes.append(
            "The message is a general logistics question: answer it in one or two sentences, "
            "then say how TrackFlow applies the concept, from CONTEXT only. Do not add an "
            "invitation at the end: TrackFlow adds it."
        )
    elif casual == "small_talk":
        notes.append(
            "The message is small talk or a general question, not about TrackFlow: answer it "
            "in one or two sentences from general knowledge (you cannot see live data such as "
            "the current time), say nothing about TrackFlow, and do not add an invitation at "
            "the end: TrackFlow adds it."
        )
    return notes


def _skipped(check: str, reason: str) -> dict[str, Any]:
    return {"memory_check": check, "reason": reason, "proposal": None, "structured": True}


def generate_answer(state: AgentState) -> dict[str, Any]:
    """Answer from the evidence the earlier nodes produced, and only from it, and
    self-evaluate whether the message holds something worth remembering - one call.

    A lookup that failed is never shown to the model: its fixed notice is put
    in front of the generated answer instead. Every external text is sanitized
    first (`guardrails.isolation`): an instruction inside a chunk, a ticket, a
    memory or an order record is removed, and a passage that is mostly
    instructions is dropped. Small talk and general questions are answered
    without evidence, briefly.
    """
    raw_passages = evidence_passages(state)
    raw_memories = recalled_memories(state)
    update = user_update(state)
    casual = casual_scope(state)
    if not raw_passages and not raw_memories and not update and casual is None:
        # The routers send a run without evidence to a fixed answer; reaching
        # this node without any means the graph was wired wrongly.
        raise AgentContractError(
            f"'{GENERATE_ANSWER}' needs evidence: retrieved context, a lookup that "
            "succeeded, a recalled memory, a verified order, an update from the user, or a "
            "small-talk message"
        )
    passages, events = isolation.sanitize_passages(raw_passages)
    memories, memory_events = isolation.sanitize_memories(raw_memories)
    events = events + memory_events
    if not passages and not memories and not update and casual is None:
        # Everything the model would have read was an injected instruction: the
        # honest fixed answer, no model call, nothing to self-evaluate.
        result: dict[str, Any] = {"answer": NO_INFORMATION_ANSWER, "model_scope": "in_domain"}
        if events:
            result["guardrail_events"] = events
        return result
    response = respond.generate_response(
        state["question"],
        passages,
        memories=memories,
        user_update=update,
        session_notes=session_notes(state),
    )
    result = {
        "answer": "\n\n".join([*policy_notices(state), *failure_notices(state), response.answer]),
        "memory_candidate": response.candidate(),
        "model_scope": response.scope,
    }
    if events:
        result["guardrail_events"] = events
    return result


def validate_output(state: AgentState) -> dict[str, Any]:
    """Check the generated answer before anyone reads it. Never calls a model.

    1. The model's own scope label may only make things stricter: a reply it
       labelled `instruction_change` or `personal_task` becomes the fixed refusal.
    2. The output guard: structure, leaked instructions, and the data the
       briefing says must never be revealed. A failing answer is withheld.
    3. Small talk and general questions end with the fixed redirect.

    A refused or withheld answer, small talk, and every customer session never
    lead to a memory proposal.
    """
    answer = str(state.get("answer") or "")
    session = state.get("session") or {}
    customer = is_customer(session)
    model_scope = str(state.get("model_scope") or "in_domain")
    casual = casual_scope(state)
    guard_category = (state.get("guard") or {}).get("category")
    events: list[dict[str, Any]] = []
    findings: list[output_guard.OutputFinding] = []

    if model_scope == "instruction_change":
        action = "refuse"
        answer = f"{INSTRUCTION_CHANGE_REFUSAL}\n\n{SCOPE_REDIRECT}"
        events.append(
            event("model_scope", "refuse", "security", "model_scope.instruction_change", "model")
        )
    elif model_scope == "personal_task":
        action = "refuse"
        answer = f"{PERSONAL_TASK_REFUSAL}\n\n{SCOPE_REDIRECT}"
        events.append(
            event("model_scope", "refuse", "content", "model_scope.personal_task", "model")
        )
    else:
        findings = output_guard.review_answer(
            answer,
            audience=_audience(state),
            owned_orders=session.get("owned_orders") or [],
            confidential=respond.confidential_prompt(),
        )
        redirect = casual or (model_scope if model_scope in CASUAL_CATEGORIES else None)
        if findings:
            action = "withhold"
            answer = f"{OUTPUT_WITHHELD}\n\n{SCOPE_REDIRECT}"
            events.extend(output_guard.events(findings))
        elif redirect and SCOPE_REDIRECT not in answer:
            action = "redirect"
            answer = f"{answer}\n\n{SCOPE_REDIRECT}"
            if guard_category not in CASUAL_CATEGORIES:  # the guard already logged its own
                rule = (
                    f"model_scope.{redirect}" if model_scope == redirect else "planner.small_talk"
                )
                events.append(event("model_scope", "redirect", "content", rule, "model"))
        else:
            action = "pass"

    result: dict[str, Any] = {
        "answer": answer,
        "output_guard": {
            "action": action,
            "model_scope": model_scope,
            "findings": [{"rule": f.rule, "failure_type": f.failure_type} for f in findings],
        },
    }
    candidate = state.get("memory_candidate") or {}
    skip: tuple[str, str] | None = None
    if action in ("refuse", "withhold"):
        done = {"refuse": "refused", "withhold": "withheld"}[action]
        skip = ("withheld", f"the answer was {done} by the harness")
    elif customer:
        skip = ("not_for_customers", "customer sessions never propose team memory")
    elif casual is not None:
        skip = ("nothing_to_remember", f"{casual}: nothing to remember")
    if skip is not None and (candidate.get("memory_check") == "propose" or action != "pass"):
        result["memory_candidate"] = {**candidate, **_skipped(*skip)}
    if events:
        result["guardrail_events"] = events
    return result


def propose_memory(state: AgentState) -> dict[str, Any]:
    """Offer what the self-evaluation proposed - after the policy, duplicate and
    one-at-a-time checks. Never writes a memory: only the user's approval does."""
    candidate = state.get("memory_candidate") or {}
    raw = candidate.get("proposal")
    if raw is None:
        raise AgentContractError(f"'{PROPOSE_MEMORY}' ran but nothing was proposed")
    result = memory_store.get_memory_store().open_proposal(
        policy.MemoryDraft.model_validate(raw),
        conversation_id=state["conversation_id"],
        run_id=state.get("run_id"),
        user_id=state.get("user_id"),
        message=state["question"],
    )
    update: dict[str, Any] = {"memory_proposal": result.to_state()}
    if result.status == "blocked":
        # The memory policy refused what the model proposed: a guardrail too.
        poisoned = "instruction_like" in result.refusal
        update["guardrail_events"] = [
            event(
                "memory_policy",
                "block",
                "security" if poisoned else "content",
                f"memory.{(result.refusal or ('policy',))[0]}",
                "memory_proposal",
            )
        ]
    return update


def answer_lookup_failed(state: AgentState) -> dict[str, Any]:
    """Say honestly that a lookup failed, without calling a model: never a made-up status."""
    notices = failure_notices(state)
    if not notices:
        raise AgentContractError(f"'{ANSWER_LOOKUP_FAILED}' ran but no lookup failed")
    return {"answer": "\n\n".join(notices)}


def answer_no_information(state: AgentState) -> dict[str, Any]:
    """Say honestly that the knowledge base has nothing on it, without calling a model."""
    return {"answer": NO_INFORMATION_ANSWER}


# --- The response ---------------------------------------------------------------------


def resolution_notice(
    resolution: dict[str, Any] | None, reply: dict[str, Any] | None
) -> str | None:
    """The fixed sentence saying what happened to the pending proposal."""
    if resolution is None:
        return None
    outcome = resolution["outcome"]
    if outcome == "approved":
        return MEMORY_SAVED.format(content=resolution["content"])
    if outcome == "rejected":
        return MEMORY_REJECTED
    if outcome == "blocked":
        reasons = resolution.get("refusal") or []
        why = (
            "; ".join(policy.REFUSAL_REASONS.get(r, r) for r in reasons)
            or "it breaks the memory policy"
        )
        return MEMORY_REFUSED.format(why=why)
    if outcome == "already_resolved":
        return MEMORY_ALREADY_RESOLVED
    if outcome == "discarded":
        label = ((reply or {}).get("decision") or {}).get("label")
        return MEMORY_DISCARDED_TOPIC if label == "unrelated" else MEMORY_DISCARDED_UNCLEAR
    return None  # revised: the corrected version is asked at the end


def proposal_question(proposal: dict[str, Any], *, revised: bool = False) -> str:
    if revised:
        return MEMORY_REVISED_QUESTION.format(content=proposal["content"])
    if proposal.get("replaces_content"):
        return MEMORY_UPDATE_QUESTION.format(
            content=proposal["content"], replaces=proposal["replaces_content"]
        )
    return MEMORY_QUESTION.format(content=proposal["content"])


def compose_response(state: AgentState) -> dict[str, Any]:
    """The response the user reads: what happened to the pending proposal, the answer,
    and the question about a new proposal - the memory texts are fixed, never generated."""
    resolution = state.get("memory_resolution")
    parts: list[str] = []
    notice = resolution_notice(resolution, state.get("memory_reply"))
    if notice:
        parts.append(notice)
    if state.get("answer"):
        parts.append(state["answer"])
    revised = (resolution or {}).get("revised")
    proposal = state.get("memory_proposal") or {}
    if revised:
        parts.append(proposal_question(revised, revised=True))
    elif proposal.get("status") == "pending" and proposal.get("proposal"):
        parts.append(proposal_question(proposal["proposal"]))
    if not parts:
        raise AgentContractError(f"'{COMPOSE_RESPONSE}' has nothing to say")
    return {"answer": "\n\n".join(parts)}
