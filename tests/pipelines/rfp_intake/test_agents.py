"""The metadata extractor, the orchestrator and the synthesizer (fake model, real guards)."""

from data.pipelines.rfp_intake.agents import metadata, orchestrator, synthesizer, workers
from tests.pipelines.rfp_intake.support import (
    ANALYSES,
    BRIEFS,
    EXTRACTED_METADATA,
    PLANS,
    sample_markdown,
)


def extracted(kind: str, **changes) -> dict:
    return EXTRACTED_METADATA[kind].model_copy(update=changes).model_dump()


# --- Metadata --------------------------------------------------------------------------------


def test_formal_rfp_metadata_with_currency_and_departments_derived(model):
    fake = model(extracted("formal"))

    result = metadata.extract_metadata(sample_markdown("formal")).result

    assert "<<untrusted kind=rfp_document>>" in fake.user
    assert result["client_name"] == "ModaViva, S.L."
    assert result["client_country"] == "ES"
    assert result["currency"] == "EUR"
    assert result["services_requested"] == ["warehousing", "reverse_logistics"]
    assert result["departments_needed"] == ["warehouse", "reverse"]
    assert result["deadline"] == "2026-08-28"
    assert result["deadline_kind"] == "proposal_due"
    assert result["monthly_volume"] is None
    assert result["missing"] == ["monthly_volume", "budget_range"]
    assert result["client_questions"][0].startswith("Ask for the expected monthly order volume")


def test_informal_rfp_metadata(model):
    model(extracted("informal"))

    result = metadata.extract_metadata(sample_markdown("informal")).result

    assert (result["client_country"], result["currency"]) == ("US", "USD")
    assert result["departments_needed"] == ["warehouse", "lastmile"]
    assert result["monthly_volume"] == 5000
    assert result["evidence"]["monthly_volume"] == "we just crossed 5,000 orders a month"


def test_a_sku_count_is_never_taken_as_the_monthly_volume(model):
    model(extracted("formal", monthly_volume=15000, monthly_volume_quote="15.000 SKUs"))

    result = metadata.extract_metadata(sample_markdown("formal")).result

    assert result["monthly_volume"] is None
    assert result["notes"] == [
        "monthly_volume 15000 dropped: its quote does not state orders per month"
    ]


def test_a_volume_that_is_not_in_its_quote_is_dropped(model):
    model(extracted("informal", monthly_volume=8000))

    result = metadata.extract_metadata(sample_markdown("informal")).result

    assert result["monthly_volume"] is None
    assert "not in a quote from the document" in result["notes"][0]


def test_a_date_without_a_year_keeps_its_words_but_gets_no_date(model):
    model(extracted("informal", deadline="2026-08-24"))

    result = metadata.extract_metadata(sample_markdown("informal")).result

    assert result["deadline"] is None
    assert result["deadline_text"] == "around August 24th"
    assert any("does not give its full date" in note for note in result["notes"])
    assert result["client_questions"][0] == (
        'Confirm the exact date behind "around August 24th": the RFP gives no full date.'
    )


def test_a_country_without_evidence_leaves_the_currency_unset(model):
    model(extracted("formal", client_country_quote="Madrid headquarters"))

    result = metadata.extract_metadata(sample_markdown("formal")).result

    assert result["client_country"] is None
    assert result["currency"] is None
    assert "client_country" in result["missing"]


# --- Orchestrator ----------------------------------------------------------------------------


def formal_metadata() -> dict:
    return metadata.finalize(sample_markdown("formal"), EXTRACTED_METADATA["formal"])


def test_the_orchestrator_splits_modaviva_into_warehouse_and_reverse_only(model):
    fake = model(PLANS["formal"].model_dump())

    plan = orchestrator.plan_departments(sample_markdown("formal"), formal_metadata()).result

    assert "Not every RFP needs all three departments" in fake.system
    assert [task["department_id"] for task in plan["subtasks"]] == ["warehouse", "reverse"]
    assert plan["subtasks"][0]["extracts"][0] == (
        "Almacenamiento de aproximadamente 15.000 SKUs en un centro cercano a Zaragoza"
    )
    assert plan["unassigned_requirements"] == ["Integration with the client's ERP via API."]
    assert plan["notes"] == []


def test_a_department_the_client_did_not_request_is_dropped():
    plan = PLANS["formal"].model_copy(deep=True)
    plan.subtasks.append(
        orchestrator.DepartmentSubtask(department_id="lastmile", objective="Quote shipping.")
    )

    result = orchestrator.reconcile(sample_markdown("formal"), formal_metadata(), plan)

    assert [task["department_id"] for task in result["subtasks"]] == ["warehouse", "reverse"]
    assert result["notes"] == [
        "lastmile dropped: the client did not request last mile delivery and carrier management"
    ]


def test_a_requested_department_the_orchestrator_forgot_is_added():
    plan = orchestrator.DepartmentPlan(subtasks=[PLANS["formal"].subtasks[0]])

    result = orchestrator.reconcile(sample_markdown("formal"), formal_metadata(), plan)

    assert [task["department_id"] for task in result["subtasks"]] == ["warehouse", "reverse"]
    reverse = result["subtasks"][1]
    assert reverse["extracts"] == []
    assert reverse["objective"].startswith("Work out what Reverse Logistics must quote")
    assert result["notes"] == ["reverse added: the client requested its service"]


def test_extracts_not_in_the_document_and_unknown_departments_are_dropped():
    plan = orchestrator.DepartmentPlan(
        subtasks=[
            orchestrator.DepartmentSubtask(
                department_id="warehouse",
                relevant_extracts=["Store 40,000 pallets in Madrid"],
            ),
            orchestrator.DepartmentSubtask(department_id="finance", objective="Price it."),
        ]
    )

    result = orchestrator.reconcile(sample_markdown("formal"), formal_metadata(), plan)

    assert result["subtasks"][0]["extracts"] == []
    assert "extract not found in the document dropped" in result["notes"][0]
    assert "unknown department 'finance' dropped" in result["notes"][1]


def test_no_requested_service_means_no_subtask():
    no_scope = {**formal_metadata(), "services_requested": []}

    result = orchestrator.reconcile(sample_markdown("formal"), no_scope, PLANS["formal"])

    assert result["subtasks"] == []


# --- Synthesizer ------------------------------------------------------------------------------


def formal_sections() -> tuple[dict, list[dict], dict]:
    markdown = sample_markdown("formal")
    meta = formal_metadata()
    plan = orchestrator.reconcile(markdown, meta, PLANS["formal"])
    sections = [
        workers.check({**task, "ticket_id": "t", "metadata": meta}, ANALYSES[task["department_id"]])
        for task in plan["subtasks"]
    ]
    return meta, sections, plan


def test_the_brief_says_what_to_ask_whom(model):
    meta, sections, plan = formal_sections()
    fake = model(BRIEFS["formal"].model_dump())

    brief = synthesizer.synthesize_brief(meta, list(reversed(sections)), plan).result

    # The synthesizer reads the sections, never the document.
    assert "Evaluation Criteria" not in fake.user
    assert "ALREADY ASKED" in fake.user
    assert [(d["department_id"], d["owner"]) for d in brief["departments"]] == [
        ("warehouse", "Ana Whitfield"),
        ("reverse", "Sofía Ramos"),
    ]
    # The model named Sofía Ramos instead of `reverse`: still matched.
    assert brief["departments"][1]["ask"] == "Quote returns processing cost and turnaround."
    assert brief["currency"] == "EUR"
    assert brief["commercial_owner"] == "Miguel Torres"
    assert brief["client_questions"][0].startswith("Ask for the expected monthly order volume")
    assert brief["unassigned_requirements"] == ["Integration with the client's ERP via API."]


def test_a_skipped_department_still_gets_an_ask_from_its_needs():
    meta, sections, plan = formal_sections()
    brief = BRIEFS["formal"].model_copy(update={"department_asks": []})

    result = synthesizer.finalize(meta, sections, plan, brief)

    assert result["departments"][0]["ask"] == (
        "Please provide: Cost per pallet and per SKU in EUR; Onboarding time."
    )


def test_invented_figures_in_the_brief_are_removed():
    meta, sections, plan = formal_sections()
    brief = BRIEFS["formal"].model_copy(
        update={
            "summary": "ModaViva ships 12,000 orders a month.",
            "risks": ["Returns must be processed within 24 hours.", "The volume is unknown."],
            "client_questions": ["Can you confirm 12,000 orders a month?"],
        }
    )

    result = synthesizer.finalize(meta, sections, plan, brief)

    assert result["summary"] == synthesizer.plain_summary(meta)
    assert result["summary"].startswith("ModaViva, S.L. requests warehousing")
    assert result["risks"] == ["The volume is unknown."]
    assert all("12,000" not in question for question in result["client_questions"])
    assert len(result["validation_notes"]) == 3
