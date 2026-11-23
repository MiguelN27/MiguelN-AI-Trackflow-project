"""Department ids, owners, contributions and statuses must match the briefing verbatim.

The ticket: "Department names, RFP format, and classification criteria must
match your company-BRIEFING.md." The briefing is the root
`agentic-workflow-context.md`; this test parses its tables.
"""

import re
from pathlib import Path

import pytest

from data.pipelines.rfp_intake import briefing

BRIEFING = Path(__file__).resolve().parents[3] / "agentic-workflow-context.md"


@pytest.fixture(scope="module")
def briefing_text() -> str:
    if not BRIEFING.is_file():
        pytest.skip(f"{BRIEFING.name} is not in the repository")
    return BRIEFING.read_text(encoding="utf-8")


def table_rows(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|") and header in line)
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_departments_match_briefing_section_2_1(briefing_text):
    rows = table_rows(briefing_text, "`department_id`")
    parsed = [(row[0].strip("`"), row[1], row[2], row[3]) for row in rows]
    expected = [(d.id, d.name, d.owner, d.contributes) for d in briefing.DEPARTMENTS]
    assert parsed == expected


def test_statuses_match_the_briefing_status_table(briefing_text):
    rows = table_rows(briefing_text, "| Status")
    statuses = {row[0].strip("`") for row in rows}
    assert statuses == set(briefing.BRIEFING_STATUSES)
    # `failed` is this implementation's addition, documented in briefing.py.
    assert {status.value for status in briefing.TicketStatus} - statuses == {"failed"}


def test_the_currency_rule_matches_section_5(briefing_text):
    assert re.search(r"USD for US operations and in EUR for Spain", briefing_text)
    assert briefing.CURRENCY_BY_COUNTRY == {"US": "USD", "ES": "EUR"}


def test_every_requested_service_maps_to_one_department():
    assert briefing.departments_for(["reverse_logistics", "warehousing"]) == [
        "warehouse",
        "reverse",
    ]
    assert briefing.departments_for(["last_mile", "unknown"]) == ["lastmile"]
    assert briefing.departments_for([]) == []
