"""Guardrail observability: every block, refusal, redirect or cleaning is logged and counted.

The guard nodes write their events into the run's state (`guardrail_events`),
never to a store: nodes stay free of I/O and an event is recorded with the run
it belongs to. When a run ends - completed or failed - the runner calls
`record()`, which:

- writes one log line per event on the `api.agent.guardrails` logger, naming the
  guardrail, the action, the failure type (structural, content or security),
  the rule, the source and the run - never the message text;
- appends the events to the trace store's `guardrail_events` table, which the
  summary reads (`npm run agent -- guardrails`, `GET /agent/guardrails/summary`);
- adds them to this process's counter, which the harness test suite prints at
  the end of its session.

An event is a dict: `guardrail`, `action`, `failure_type`, `rule`, `source`.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Final

logger = logging.getLogger("api.agent.guardrails")

GUARDRAILS: Final = (
    "input_structure",
    "injection_filter",
    "scope_filter",
    "order_authorization",
    "policy_country",
    "tool_policy",
    "external_content",
    "model_scope",
    "output_validation",
    "memory_policy",
)
FAILURE_TYPES: Final = ("structural", "content", "security")
# Actions that only steer the answer; everything else stops or alters it.
_INFORMATIONAL: Final = frozenset({"redirect", "constrain"})

_SESSION_COUNTS: Counter[tuple[str, str, str]] = Counter()


def event(guardrail: str, action: str, failure_type: str, rule: str, source: str) -> dict[str, Any]:
    return {
        "guardrail": guardrail,
        "action": action,
        "failure_type": failure_type,
        "rule": rule,
        "source": source,
    }


def log_line(item: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return (
        f"guardrail={item.get('guardrail')} action={item.get('action')} "
        f"failure_type={item.get('failure_type')} rule={item.get('rule')} "
        f"source={item.get('source')} run_id={context.get('run_id')} "
        f"conversation_id={context.get('conversation_id')} audience={context.get('audience')}"
    )


def record(
    events: Iterable[Mapping[str, Any]],
    *,
    store: Any,
    run_id: str,
    conversation_id: str | None,
    user_id: str | None,
    audience: str | None,
) -> int:
    """Log, store and count a run's events; returns how many there were."""
    items = list(events)
    if not items:
        return 0
    context = {"run_id": run_id, "conversation_id": conversation_id, "audience": audience}
    for item in items:
        level = logging.INFO if item.get("action") in _INFORMATIONAL else logging.WARNING
        logger.log(level, log_line(item, context))
    added = store.add_guardrail_events(
        run_id, items, conversation_id=conversation_id, user_id=user_id, audience=audience
    )
    for item in items[len(items) - added :]:
        _SESSION_COUNTS[
            (str(item["guardrail"]), str(item["action"]), str(item["failure_type"]))
        ] += 1
    return len(items)


def session_counts() -> Counter[tuple[str, str, str]]:
    """What this process recorded so far: (guardrail, action, failure type) -> count."""
    return Counter(_SESSION_COUNTS)


def reset_session_counts() -> None:
    _SESSION_COUNTS.clear()


def format_counts(counts: Mapping[tuple[str, str, str], int]) -> list[str]:
    """A plain table: one line per guardrail, action and failure type, then the totals."""
    if not counts:
        return ["No guardrail was triggered."]
    lines = [f"{'guardrail':22} {'action':11} {'failure type':13} {'count':>5}"]
    for (guardrail, action, failure_type), count in sorted(counts.items()):
        lines.append(f"{guardrail:22} {action:11} {failure_type:13} {count:>5}")
    by_type: Counter[str] = Counter()
    for (_, _, failure_type), count in counts.items():
        by_type[failure_type] += count
    totals = ", ".join(f"{name} {by_type[name]}" for name in FAILURE_TYPES if by_type[name])
    lines.append(f"total {sum(counts.values())} ({totals})")
    return lines
