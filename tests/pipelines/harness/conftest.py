"""The SEC-114 harness suite: shared corpus and fixtures, and the session's guardrail summary.

`npm run agent:harness` runs this folder. No test here reaches a live model:
the input guard, the isolation of external text and the output guard are
deterministic, and the graph runs with the fake models of
`tests/pipelines/test_agent_graph.py`. At the end of the session the suite
prints how many times each guardrail was triggered by the runs it made - the
same table `npm run agent -- guardrails` prints for real traffic.
"""

import json
from pathlib import Path
from typing import Any

from services.agent.guardrails import events
from services.agent.guardrails.events import format_counts

ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = ROOT / "data" / "eval" / "agent" / "harness-cases.json"
CORPUS: dict[str, Any] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = CORPUS["cases"]
CUSTOMER_EMAIL: str = CORPUS["customer"]["email"]
OWNED: dict[str, str] = CORPUS["customer"]["owns"]


def cases(*, actions: tuple[str, ...] = (), groups: tuple[str, ...] = ()) -> list[Any]:
    """Corpus cases as pytest params, filtered by expected action and/or group."""
    import pytest

    chosen = [
        case
        for case in CASES
        if (not actions or case["action"] in actions) and (not groups or case["group"] in groups)
    ]
    return [pytest.param(case, id=case["id"]) for case in chosen]


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    counts = events.session_counts()
    if not counts:
        return
    terminalreporter.section("SEC-114 guardrail triggers in this test session")
    for line in format_counts(counts):
        terminalreporter.write_line(line)
