"""Response contracts of the RFP intake routes (mirrored in `uis/backoffice/lib/rfp.ts`)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


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
    events: list[RfpTicketEvent]
