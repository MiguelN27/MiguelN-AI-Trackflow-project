"""The Part 2 agents: a generator per department and the model-based evaluators.

Bundled as `ProposalAgents` and passed through the graph's runtime context,
like Part 1's `IntakeAgents`, so tests run the real graphs with scripted
agents. The readability evaluator uses no model and is called directly.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from data.pipelines.rfp_intake.agents import AgentReply

Generator = Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None], AgentReply]
Knowledge = Callable[[dict[str, Any], int], tuple[list[dict[str, Any]], str]]


@dataclass(frozen=True)
class ProposalAgents:
    # One generator agent per department id.
    generators: Mapping[str, Generator]
    judge_relevance: Callable[[dict[str, Any], dict[str, Any], str], AgentReply]
    review_compliance: Callable[[dict[str, Any], dict[str, Any], str, bool], AgentReply]
    retrieve_knowledge: Knowledge


def default_agents() -> ProposalAgents:
    from data.pipelines.rfp_intake.briefing import DEPARTMENT_IDS
    from data.pipelines.rfp_intake.proposal import knowledge
    from data.pipelines.rfp_intake.proposal.agents import compliance, generators, relevance

    return ProposalAgents(
        generators={dept: generators.generator_for(dept) for dept in DEPARTMENT_IDS},
        judge_relevance=relevance.judge,
        review_compliance=compliance.evaluate,
        retrieve_knowledge=knowledge.retrieve_for,
    )
