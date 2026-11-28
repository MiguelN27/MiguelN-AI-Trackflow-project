"""The department workers (ticket: "Each worker receives metadata + department-relevant extracts").

Through the real `llm.call_tool` with a fake model.
"""

import pytest

from data.pipelines.rfp_intake.agents import metadata, orchestrator, workers
from tests.pipelines.rfp_intake.support import (
    EXTRACTED_METADATA,
    PLANS,
    sample_markdown,
)


def shared_metadata(kind: str) -> dict:
    return metadata.finalize(sample_markdown(kind), EXTRACTED_METADATA[kind])


def task_for(kind: str, department_id: str) -> dict:
    meta = shared_metadata(kind)
    plan = orchestrator.reconcile(sample_markdown(kind), meta, PLANS[kind])
    (subtask,) = [t for t in plan["subtasks"] if t["department_id"] == department_id]
    return {"ticket_id": "t-1", **subtask, "metadata": meta}


def analysis(**fields) -> dict:
    return {"key_aspects": [], "needed_from_department": [], "open_questions": [], **fields}


ASPECT = {
    "aspect": "Storage volume",
    "detail": "Storage for approximately 15,000 SKUs near Zaragoza.",
    "source_quote": "Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano a Zaragoza",
}


def test_the_warehouse_worker_reads_only_metadata_and_its_own_extracts(model):
    fake = model(analysis(key_aspects=[ASPECT]))
    task = task_for("formal", "warehouse")

    workers.analyze_department(task)

    message = fake.user
    assert "Almacenamiento de aproximadamente 15.000 SKUs" in message
    assert "Client: ModaViva, S.L." in message
    # Not the whole document, and not the returns department's extract.
    assert "Evaluation Criteria" not in message
    assert "logistica@modaviva.es" not in message
    assert "inspección, reacondicionamiento" not in message
    assert "<<untrusted kind=rfp_extracts>>" in message


def test_each_worker_prompt_is_its_department_from_the_briefing(model):
    fake = model(analysis(), analysis())
    workers.analyze_department(task_for("formal", "warehouse"))
    assert "Warehouse Operations analyst" in fake.system
    assert "Ana Whitfield" in fake.system
    assert "Storage capacity, cost per pallet/SKU, onboarding time" in fake.system

    workers.analyze_department(task_for("formal", "reverse"))
    assert "Reverse Logistics analyst" in fake.system
    assert "Sofía Ramos" in fake.system


def test_the_section_keeps_grounded_key_aspects_and_the_owner_from_the_briefing(model):
    model(
        analysis(
            key_aspects=[ASPECT],
            needed_from_department=["Cost per pallet and per SKU in EUR"],
            open_questions=["What units per SKU should we plan for?"],
        )
    )

    section = workers.analyze_department(task_for("formal", "warehouse")).result

    assert section["department_id"] == "warehouse"
    assert section["owner"] == "Ana Whitfield"
    assert section["key_aspects"] == [ASPECT]
    assert section["needed_from_department"] == ["Cost per pallet and per SKU in EUR"]
    assert section["validation_notes"] == []


def test_an_invented_volume_is_dropped(model):
    invented = {**ASPECT, "aspect": "Pallets", "detail": "About 2,000 pallets in storage."}
    model(
        analysis(
            key_aspects=[ASPECT, invented],
            needed_from_department=["Quote 2,000 pallet positions"],
            open_questions=["Can you confirm 3,000 orders a month?"],
        )
    )

    section = workers.analyze_department(task_for("formal", "warehouse")).result

    assert section["key_aspects"] == [ASPECT]
    assert section["needed_from_department"] == []
    assert all("3,000" not in question for question in section["open_questions"])
    assert any("2000 is not in the RFP" in note for note in section["validation_notes"])
    assert any("3000 is not in the RFP" in note for note in section["validation_notes"])


def test_an_aspect_whose_quote_is_not_in_the_extracts_is_dropped(model):
    unquoted = {**ASPECT, "aspect": "Locations", "source_quote": "Warehouses in Madrid"}
    model(analysis(key_aspects=[unquoted]))

    section = workers.analyze_department(task_for("formal", "warehouse")).result

    assert section["key_aspects"] == []
    assert section["validation_notes"] == [
        "key aspect 'Locations' dropped: its source quote is not in the extracts"
    ]


def test_a_copied_list_number_does_not_hide_a_real_quote(model):
    numbered = {**ASPECT, "source_quote": "1. " + ASPECT["source_quote"]}
    model(analysis(key_aspects=[numbered]))

    section = workers.analyze_department(task_for("formal", "warehouse")).result

    assert section["key_aspects"] == [ASPECT]


@pytest.mark.parametrize("department_id", ["warehouse", "reverse"])
def test_a_missing_monthly_volume_is_an_open_question_never_a_guess(model, department_id):
    model(analysis())

    section = workers.analyze_department(task_for("formal", department_id)).result

    if department_id in workers.VOLUME_DEPENDENT:
        assert section["open_questions"] == [workers.MISSING_VOLUME_QUESTION]
    else:
        assert section["open_questions"] == []


def test_a_stated_volume_needs_no_volume_question(model):
    model(analysis())

    section = workers.analyze_department(task_for("informal", "lastmile")).result

    assert section["open_questions"] == []


def test_the_workers_own_volume_question_is_not_doubled(model):
    model(analysis(open_questions=["What is the monthly order volume?"]))

    section = workers.analyze_department(task_for("formal", "warehouse")).result

    assert section["open_questions"] == ["What is the monthly order volume?"]
