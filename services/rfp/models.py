"""Response contracts of the RFP intake routes (mirrored in `uis/backoffice/lib/rfp.ts`)."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RfpTicketAccepted(BaseModel):
    ticket_id: UUID
    rfp_id: UUID
    status: str
    original_filename: str


class RfpTicketSummary(BaseModel):
    ticket_id: UUID
    rfp_id: UUID
    status: str
    current_stage: str | None
    original_filename: str
    client_name: str | None
    client_country: str | None
    departments_needed: list[str]
    created_at: datetime
    updated_at: datetime


class RfpTicketList(BaseModel):
    tickets: list[RfpTicketSummary]


class RfpReadability(BaseModel):
    language: str | None
    word_count: int
    sentence_count: int
    char_count: int
    estimated_tokens: int
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None
    gunning_fog: float | None
    processing_cost: str
    note: str | None


class RfpMetadata(BaseModel):
    client_name: str | None
    client_country: str | None
    currency: str | None
    services_requested: list[str]
    monthly_volume: int | None
    deadline: date | None
    deadline_text: str | None
    deadline_kind: str | None
    budget_range: str | None
    departments_needed: list[str]
    evidence: dict[str, Any]
    missing: list[str]
    notes: list[str]
    readability: RfpReadability


class RfpKeyAspect(BaseModel):
    aspect: str
    detail: str
    source_quote: str


class _Check(BaseModel):
    # The ticket's field name is `pass`, a Python keyword: read and served by alias.
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    passed: bool = Field(alias="pass")


class RfpReadabilityCheck(_Check):
    score: float | None
    details: dict[str, Any]


class RfpRelevanceCheck(_Check):
    missing_aspects: list[str]
    covered_aspects: list[str] = Field(default_factory=list)
    not_applicable: list[str] = Field(default_factory=list)


class RfpComplianceViolation(BaseModel):
    rule_id: str
    message: str
    quote: str | None = None
    source: str = "check"
    retryable: bool = True


class RfpComplianceCheck(_Check):
    rule_ids: list[str]
    violations: list[RfpComplianceViolation]
    checked_rule_ids: list[str] = Field(default_factory=list)
    reviewer: str = "off"


class RfpEvaluationResult(BaseModel):
    """The ticket's `EvaluationResult` for one section's current draft."""

    section_id: UUID
    department_id: str
    iteration: int
    readability: RfpReadabilityCheck
    relevance: RfpRelevanceCheck
    compliance: RfpComplianceCheck
    overall_pass: bool
    feedback_for_generator: str
    evaluated_at: datetime


class RfpPriceLine(BaseModel):
    item_id: str
    amount: float
    currency: str
    note: str = ""


class RfpDiscountTier(BaseModel):
    min_monthly_volume: int
    max_monthly_volume: int | None
    discount_pct: float


class RfpSectionDraft(BaseModel):
    """The structured draft. Department-specific fields stay as the pipeline wrote them."""

    model_config = ConfigDict(extra="allow")

    summary: str
    narrative: list[str]
    price_lines: list[RfpPriceLine]
    discount_tiers: list[RfpDiscountTier]
    on_time_sla_pct: float | None = None
    assumptions: list[str]
    open_questions: list[str]


class RfpApprovalDecision(BaseModel):
    """One decision on a section: the owner's, an arbiter's (§7) or the system's."""

    decision_id: UUID
    department_id: str
    review_round: int
    action: str
    comment: str | None
    source: str
    decided_by: str
    decided_by_name: str
    trigger_id: str | None
    status: str
    status_note: str | None
    created_at: datetime
    applied_at: datetime | None


class RfpDepartmentSection(BaseModel):
    department_id: str
    department_name: str
    owner: str
    objective: str | None
    extracts: list[str]
    key_aspects: list[RfpKeyAspect]
    needed_from_department: list[str]
    open_questions: list[str]
    validation_notes: list[str]
    # Part 2: the section's proposal draft and its evaluation (null before drafting).
    draft_status: str | None = None
    draft_iterations: int = 0
    max_iterations: int | None = None
    is_sla_section: bool = False
    needs_human_review: bool = False
    draft_content: str | None = None
    draft: RfpSectionDraft | None = None
    guard_notes: list[str] = Field(default_factory=list)
    knowledge_source: str | None = None
    rate_card_version: str | None = None
    review_note: str | None = None
    evaluation: RfpEvaluationResult | None = None
    approval_status: str | None = None
    ready_at: datetime | None = None
    # Part 3: the owner's approval (null or empty before the approvals open).
    approver: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    review_round: int = 1
    review_rounds: int = 0
    max_review_rounds: int | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    approver_configured: bool = False
    awaiting_decision: bool = False
    decision_pending: bool = False
    # Whether the signed-in account is this department's owner and may decide now.
    can_decide: bool = False
    decisions: list[RfpApprovalDecision] = Field(default_factory=list)


class RfpBriefDepartment(BaseModel):
    department_id: str
    department_name: str
    owner: str
    ask: str
    needed_from_department: list[str]
    open_questions: list[str]


class RfpSalesBrief(BaseModel):
    summary: str
    client_name: str | None
    client_country: str | None
    currency: str | None
    commercial_owner: str
    departments: list[RfpBriefDepartment]
    client_questions: list[str]
    risks: list[str]
    unassigned_requirements: list[str]
    validation_notes: list[str]


class RfpHandoffStatus(BaseModel):
    contract_version: str
    next_stage: str
    status: str
    created_at: datetime
    claimed_at: datetime | None


class RfpProposalStatus(BaseModel):
    """The Part 3 handoff (`rfp-proposal/1`), once Part 2 has finished."""

    contract_version: str
    next_stage: str
    status: str
    needs_human_review: bool
    sla_department: str | None
    currency: str | None
    rate_card_version: str | None
    max_iterations: int | None
    average_iterations_per_section: float | None
    passed_first_attempt: int | None
    created_at: datetime
    claimed_at: datetime | None


class RfpRuling(BaseModel):
    """A §7 arbitration ruling, applied by rule in the named arbiter's name."""

    model_config = ConfigDict(extra="allow")

    trigger_id: str
    arbiter: str
    arbiter_basis: str
    rule: str
    round: int
    departments: list[str]
    action: str
    instruction: str
    volume_cap: int | None = None
    evidence: list[str]
    ruled_at: datetime


class RfpApprovalStatus(BaseModel):
    """The Part 3 run: its checkpoint thread, status, rulings and any failure."""

    status: str
    thread_id: str
    started_at: datetime
    finished_at: datetime | None
    arbitration_rounds: int
    max_arbitration_rounds: int
    rulings: list[RfpRuling]
    error_stage: str | None
    error_message: str | None
    approved: int
    sections: int
    viewer_departments: list[str]


class RfpFinalDocumentSummary(BaseModel):
    generated_at: datetime
    currency: str
    content_sha256: str
    sections: list[str]


class RfpTicketEvent(BaseModel):
    node: str
    outcome: str
    detail: dict[str, Any] | None
    duration_ms: int | None
    created_at: datetime


class RfpTicketDetail(BaseModel):
    ticket_id: UUID
    rfp_id: UUID
    status: str
    current_stage: str | None
    original_filename: str
    pdf_bytes: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    classification: dict[str, Any] | None
    discard_reason: str | None
    error_stage: str | None
    error_message: str | None
    llm_tokens_used: int
    markdown_chars: int
    markdown_truncated: bool
    metadata: RfpMetadata | None
    sections: list[RfpDepartmentSection]
    sales_brief: RfpSalesBrief | None
    handoff: RfpHandoffStatus | None
    proposal: RfpProposalStatus | None = None
    approval: RfpApprovalStatus | None = None
    final_document: RfpFinalDocumentSummary | None = None
    events: list[RfpTicketEvent]


class RfpDecisionRequest(BaseModel):
    """An owner's decision on their department's section."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "request_changes", "reject"]
    comment: str | None = Field(default=None, max_length=2000)
    # The round the owner reviewed: a decision never applies to a newer draft.
    review_round: int = Field(ge=1)


class RfpFinalSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    department_id: str
    department_name: str
    owner: str
    approved_by: str
    approved_at: datetime
    review_rounds: int
    is_sla_section: bool
    content: str


class RfpFinalDocument(BaseModel):
    """Briefing §2.3 FinalDocument (`ticket_id`, `sections`, `currency`, `generated_at`)."""

    ticket_id: UUID
    rfp_id: UUID
    client_name: str | None
    currency: str
    sections: list[RfpFinalSection]
    generated_at: datetime
    content: str
    content_sha256: str
    approval_thread_id: str


class RfpTraceStep(BaseModel):
    position: int
    node: str
    part: int
    step: str
    department: str | None
    number: int | None
    agent: str
    outcome: str
    duration_ms: int | None
    at: datetime
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    detail: dict[str, Any] | None


class RfpTrace(BaseModel):
    ticket_id: UUID
    steps: list[RfpTraceStep]
