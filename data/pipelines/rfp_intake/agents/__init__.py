"""The intake agents: classifier, metadata extractor, orchestrator, workers, synthesizer.

Each agent is one structured model call (`llm.call_tool`) followed by
deterministic checks, and returns an `AgentReply`: a JSON-mode result plus the
tokens it spent. The graph receives them bundled as `IntakeAgents` through its
runtime context, so tests run the real graph with fake agents.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from data.pipelines.rfp_intake.llm import Usage


class AgentReply(NamedTuple):
    result: dict[str, Any]
    usage: Usage


@dataclass(frozen=True)
class IntakeAgents:
    classify_document: Callable[[str], AgentReply]
    extract_metadata: Callable[[str], AgentReply]
    plan_departments: Callable[[str, dict[str, Any]], AgentReply]
    analyze_department: Callable[[dict[str, Any]], AgentReply]
    synthesize_brief: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], AgentReply]


def default_agents() -> IntakeAgents:
    from data.pipelines.rfp_intake.agents import (
        classifier,
        metadata,
        orchestrator,
        synthesizer,
        workers,
    )

    return IntakeAgents(
        classify_document=classifier.classify_document,
        extract_metadata=metadata.extract_metadata,
        plan_departments=orchestrator.plan_departments,
        analyze_department=workers.analyze_department,
        synthesize_brief=synthesizer.synthesize_brief,
    )
