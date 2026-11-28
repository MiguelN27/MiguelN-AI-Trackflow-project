"""The agent's domain is the briefing's (SEC-114 checklist: "allowed topics, domain
boundaries and company-specific rules must match what is specified in your
BRIEFING.md"). The declaration in `guardrails/scope.py` is parsed against
`engineering-briefing.md`, and the briefing's mandatory test cases are in the corpus."""

import re

import pytest

from services.agent import respond
from services.agent.guardrails import scope
from tests.pipelines.harness.conftest import CASES

BRIEFING = scope.BRIEFING_PATH.read_text(encoding="utf-8")


def _plain(text: str) -> str:
    return " ".join(text.replace("**", "").split())


def bullets_after(heading: str) -> list[str]:
    """The `- ` bullets right after the line that starts with `heading`."""
    lines = BRIEFING.splitlines()
    start = next(i for i, line in enumerate(lines) if _plain(line).startswith(heading))
    found: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("- "):
            found.append(_plain(line[2:]))
        elif found and line.strip():
            break
    return found


@pytest.mark.parametrize(
    ("heading", "declared"),
    [
        ("In-domain", scope.IN_DOMAIN),
        ("Out-of-domain but allowed", scope.ALLOWED_WITH_REDIRECT),
        ("Forbidden — personal chatbot use", scope.FORBIDDEN_USE),
        ("## 3. Sensitive Data", scope.NEVER_REVEAL),
    ],
)
def test_the_declared_domain_is_the_briefings_verbatim(heading, declared):
    assert [_plain(item) for item in declared] == bullets_after(heading)


def test_the_agent_named_in_the_briefing_is_the_one_hardened():
    assert "first-line CX agent" in BRIEFING
    assert scope.CX_MANAGER in BRIEFING
    assert scope.CX_MANAGER in respond.AGENT_IDENTITY
    assert "B2B" in respond.AGENT_IDENTITY and "B2C" in respond.AGENT_IDENTITY


def test_the_system_prompt_declares_the_whole_domain():
    prompt = respond.system_prompt()
    for item in (*scope.IN_DOMAIN, *scope.ALLOWED_WITH_REDIRECT, *scope.FORBIDDEN_USE):
        assert item in prompt
    for item in scope.NEVER_REVEAL:
        assert item in prompt


def test_every_mandatory_test_case_of_the_briefing_is_in_the_corpus():
    section = BRIEFING.split("## 4.", 1)[1].split("## 5.", 1)[0]
    mandatory = re.findall(r"`\"(.+?)\"`", section)
    messages = {case["message"]: case for case in CASES}

    assert len(mandatory) == 4
    for text in mandatory:
        assert text in messages, f"briefing case not in the corpus: {text}"
        assert messages[text]["group"] == "briefing"
