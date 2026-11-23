/**
 * RFP intake and proposal drafting, as the backoffice reads them (Milestone 9 Parts 1-2).
 *
 * Three calls, all with the session token and staff-only on the API:
 * `POST /rfp/tickets` (multipart PDF upload, answers 202 at once),
 * `GET /rfp/tickets` and `GET /rfp/tickets/{id}`. The analysis and then the
 * proposal drafting run in the background, so the pages poll while a ticket
 * is being worked on (`isWorking`). Every reply is validated before it is
 * rendered: a reply the page cannot read is an error, never an empty ticket.
 * Shapes mirror `services/rfp/models.py`.
 */

import { ApiError, parseResponseJson, requestAuthenticatedApi } from "@/lib/api-client";
import { describeError, type ErrorCopy } from "@/lib/friendly-error";

export const RFP_PATH = "/rfp";
const TICKETS_API = "/rfp/tickets";

/** Matches `RFP_MAX_UPLOAD_MB`'s default in `data/pipelines/rfp_intake/config.py`. */
export const MAX_UPLOAD_MB = 20;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
/** How often a ticket being worked on is re-read. */
export const POLL_INTERVAL_MS = 3000;
/** How often a ticket waiting in the drafting queue is re-read. */
export const QUEUED_POLL_INTERVAL_MS = 10000;

export type RfpStatus =
  | "analyzing"
  | "discarded"
  | "intake_complete"
  | "drafting"
  | "under_evaluation"
  | "needs_human_review"
  | "waiting_for_approval"
  | "done"
  | "failed";

export const STATUS_LABELS: Record<RfpStatus, string> = {
  analyzing: "Analyzing",
  discarded: "Discarded",
  intake_complete: "Intake complete",
  drafting: "Drafting",
  under_evaluation: "Under evaluation",
  needs_human_review: "Needs human review",
  waiting_for_approval: "Waiting for approval",
  done: "Done",
  failed: "Failed",
};

export const DEPARTMENT_NAMES: Record<string, string> = {
  warehouse: "Warehouse Operations",
  lastmile: "Last Mile and Carrier Management",
  reverse: "Reverse Logistics",
};

export const SERVICE_LABELS: Record<string, string> = {
  warehousing: "Warehousing",
  last_mile: "Last mile",
  reverse_logistics: "Returns",
};

export const COUNTRY_NAMES: Record<string, string> = { US: "United States", ES: "Spain" };

/** Part 2's stages (`data/pipelines/rfp_intake/proposal/graph.py`), as Sales reads them. */
export const PROPOSAL_STAGES: readonly { key: string; label: string }[] = [
  { key: "plan_assignments", label: "Assign the sections" },
  { key: "retrieve_knowledge", label: "Look up TrackFlow's policies" },
  { key: "generate_section", label: "Write the draft" },
  { key: "check_readability", label: "Check readability" },
  { key: "check_relevance", label: "Check it answers the RFP" },
  { key: "check_compliance", label: "Check the company guidelines" },
  { key: "merge_evaluation", label: "Combine the checks" },
  { key: "accept_section", label: "Section passed" },
  { key: "escalate_section", label: "Sent for human review" },
  { key: "synthesize_proposal", label: "Assemble the approval tickets" },
  { key: "complete_drafting", label: "Hand off for approval" },
];

/** A section's place in the generator-evaluator loop. */
export const SECTION_STATUS_LABELS: Record<string, string> = {
  pending: "Waiting to be drafted",
  drafting: "Writing the draft",
  under_evaluation: "Checking the draft",
  passed: "Passed every check",
  needs_human_review: "Needs human review",
  failed: "Could not be drafted",
};

/** Briefing §5, one plain sentence per rule id (`proposal/guidelines.py`). */
export const RULE_LABELS: Record<string, string> = {
  "currency-by-country": "Prices in the client's currency (USD for the US, EUR for Spain)",
  "on-time-sla-stated": "States the on-time delivery SLA (%)",
  "returns-min-48h": "Never promises returns processing in under 48 hours",
  "volume-discount-table": "Includes a volume discount tier table",
  "no-carrier-rates": "Never discloses rates negotiated with carriers",
};

/** The graph's stages, in run order, as Sales reads them (`data/pipelines/rfp_intake/graph.py`). */
export const STAGES: readonly { key: string; label: string }[] = [
  { key: "convert_to_markdown", label: "Convert the PDF to text" },
  { key: "classify_document", label: "Check it is an RFP" },
  { key: "measure_readability", label: "Measure readability" },
  { key: "extract_metadata", label: "Extract the client's details" },
  { key: "plan_departments", label: "Split the work by department" },
  { key: "analyze_department", label: "Department analysis" },
  { key: "synthesize_brief", label: "Write the Sales brief" },
  { key: "complete_intake", label: "Hand off to proposal drafting" },
];

export type RfpTicketAccepted = {
  ticket_id: string;
  rfp_id: string;
  status: RfpStatus;
  original_filename: string;
};

export type RfpTicketSummary = {
  ticket_id: string;
  rfp_id: string;
  status: RfpStatus;
  current_stage: string | null;
  original_filename: string;
  client_name: string | null;
  client_country: string | null;
  departments_needed: string[];
  created_at: string;
  updated_at: string;
};

export type RfpReadability = {
  language: string | null;
  word_count: number;
  sentence_count: number;
  char_count: number;
  estimated_tokens: number;
  flesch_reading_ease: number | null;
  flesch_kincaid_grade: number | null;
  gunning_fog: number | null;
  processing_cost: "low" | "medium" | "high";
  note: string | null;
};

export type RfpMetadata = {
  client_name: string | null;
  client_country: string | null;
  currency: string | null;
  services_requested: string[];
  monthly_volume: number | null;
  deadline: string | null;
  deadline_text: string | null;
  deadline_kind: string | null;
  budget_range: string | null;
  departments_needed: string[];
  evidence: Record<string, unknown>;
  missing: string[];
  notes: string[];
  readability: RfpReadability;
};

export type RfpKeyAspect = { aspect: string; detail: string; source_quote: string };

export type RfpReadabilityCheck = {
  pass: boolean;
  score: number | null;
  details: {
    scored?: boolean;
    flesch_reading_ease?: number | null;
    flesch_kincaid_grade?: number | null;
    word_count?: number;
    words_per_sentence?: number | null;
    min_ease?: number;
    max_grade?: number;
    note?: string | null;
  };
};

export type RfpRelevanceCheck = {
  pass: boolean;
  missing_aspects: string[];
  covered_aspects: string[];
  not_applicable: string[];
};

export type RfpViolation = { rule_id: string; message: string; quote: string | null; source: string };

export type RfpComplianceCheck = {
  pass: boolean;
  rule_ids: string[];
  violations: RfpViolation[];
  checked_rule_ids: string[];
  reviewer: string;
};

/** The ticket's `EvaluationResult` for a section's current draft. */
export type RfpEvaluation = {
  section_id: string;
  department_id: string;
  iteration: number;
  readability: RfpReadabilityCheck;
  relevance: RfpRelevanceCheck;
  compliance: RfpComplianceCheck;
  overall_pass: boolean;
  feedback_for_generator: string;
  evaluated_at: string;
};

export type RfpPriceLine = { item_id: string; amount: number; currency: string; note: string };

export type RfpSectionDraft = {
  summary: string;
  narrative: string[];
  price_lines: RfpPriceLine[];
  discount_tiers: { min_monthly_volume: number; max_monthly_volume: number | null; discount_pct: number }[];
  on_time_sla_pct: number | null;
  assumptions: string[];
  open_questions: string[];
};

export type RfpSection = {
  department_id: string;
  department_name: string;
  owner: string;
  objective: string | null;
  extracts: string[];
  key_aspects: RfpKeyAspect[];
  needed_from_department: string[];
  open_questions: string[];
  validation_notes: string[];
  // Part 2: null until the section is drafted.
  draft_status: string | null;
  draft_iterations: number;
  max_iterations: number | null;
  is_sla_section: boolean;
  needs_human_review: boolean;
  draft_content: string | null;
  draft: RfpSectionDraft | null;
  guard_notes: string[];
  knowledge_source: string | null;
  rate_card_version: string | null;
  review_note: string | null;
  evaluation: RfpEvaluation | null;
  approval_status: string | null;
  ready_at: string | null;
};

/** The Part 3 handoff (`rfp-proposal/1`), once Part 2 has finished. */
export type RfpProposal = {
  contract_version: string;
  next_stage: string;
  status: string;
  needs_human_review: boolean;
  sla_department: string | null;
  currency: string | null;
  rate_card_version: string | null;
  max_iterations: number | null;
  average_iterations_per_section: number | null;
  passed_first_attempt: number | null;
  created_at: string;
  claimed_at: string | null;
};

export type RfpBriefDepartment = {
  department_id: string;
  department_name: string;
  owner: string;
  ask: string;
  needed_from_department: string[];
  open_questions: string[];
};

export type RfpSalesBrief = {
  summary: string;
  client_name: string | null;
  client_country: string | null;
  currency: string | null;
  commercial_owner: string;
  departments: RfpBriefDepartment[];
  client_questions: string[];
  risks: string[];
  unassigned_requirements: string[];
  validation_notes: string[];
};

export type RfpClassification = {
  decision?: string;
  document_kind?: string;
  confidence?: number;
  language?: string;
  reasons?: string[];
  flags?: string[];
  review_note?: string;
};

export type RfpEvent = {
  node: string;
  outcome: "completed" | "failed" | "decision" | "warning";
  detail: Record<string, unknown> | null;
  duration_ms: number | null;
  created_at: string;
};

export type RfpTicketDetail = Omit<RfpTicketSummary, "client_name" | "client_country" | "departments_needed"> & {
  pdf_bytes: number;
  completed_at: string | null;
  classification: RfpClassification | null;
  discard_reason: string | null;
  error_stage: string | null;
  error_message: string | null;
  llm_tokens_used: number;
  markdown_chars: number;
  markdown_truncated: boolean;
  metadata: RfpMetadata | null;
  sections: RfpSection[];
  sales_brief: RfpSalesBrief | null;
  handoff: { contract_version: string; next_stage: string; status: string; created_at: string; claimed_at: string | null } | null;
  proposal: RfpProposal | null;
  events: RfpEvent[];
};

// --- Validation -------------------------------------------------------------------------

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

export function isRfpStatus(value: unknown): value is RfpStatus {
  return isString(value) && Object.hasOwn(STATUS_LABELS, value);
}

export function isRfpTicketSummary(value: unknown): value is RfpTicketSummary {
  return (
    isRecord(value) &&
    isString(value.ticket_id) &&
    isString(value.rfp_id) &&
    isRfpStatus(value.status) &&
    isNullableString(value.current_stage) &&
    isString(value.original_filename) &&
    isNullableString(value.client_name) &&
    isNullableString(value.client_country) &&
    isStringArray(value.departments_needed) &&
    isString(value.created_at) &&
    isString(value.updated_at)
  );
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || typeof value === "number";
}

export function isEvaluation(value: unknown): value is RfpEvaluation {
  if (!isRecord(value)) return false;
  const { readability, relevance, compliance } = value;
  return (
    isString(value.section_id) &&
    isString(value.department_id) &&
    typeof value.iteration === "number" &&
    isRecord(readability) &&
    typeof readability.pass === "boolean" &&
    isNullableNumber(readability.score) &&
    isRecord(readability.details) &&
    isRecord(relevance) &&
    typeof relevance.pass === "boolean" &&
    isStringArray(relevance.missing_aspects) &&
    isRecord(compliance) &&
    typeof compliance.pass === "boolean" &&
    isStringArray(compliance.rule_ids) &&
    Array.isArray(compliance.violations) &&
    compliance.violations.every((item) => isRecord(item) && isString(item.rule_id) && isString(item.message)) &&
    typeof value.overall_pass === "boolean" &&
    isString(value.feedback_for_generator)
  );
}

export function isDraft(value: unknown): value is RfpSectionDraft {
  return (
    isRecord(value) &&
    isString(value.summary) &&
    isStringArray(value.narrative) &&
    Array.isArray(value.price_lines) &&
    value.price_lines.every(
      (line) => isRecord(line) && isString(line.item_id) && typeof line.amount === "number" && isString(line.currency),
    ) &&
    Array.isArray(value.discount_tiers) &&
    isStringArray(value.assumptions) &&
    isStringArray(value.open_questions)
  );
}

function isSection(value: unknown): value is RfpSection {
  return (
    isRecord(value) &&
    isString(value.department_id) &&
    isString(value.department_name) &&
    isString(value.owner) &&
    Array.isArray(value.key_aspects) &&
    value.key_aspects.every(
      (aspect) => isRecord(aspect) && isString(aspect.aspect) && isString(aspect.detail) && isString(aspect.source_quote),
    ) &&
    isStringArray(value.needed_from_department) &&
    isStringArray(value.open_questions) &&
    isNullableString(value.draft_status) &&
    typeof value.draft_iterations === "number" &&
    typeof value.is_sla_section === "boolean" &&
    typeof value.needs_human_review === "boolean" &&
    isNullableString(value.draft_content) &&
    (value.draft === null || isDraft(value.draft)) &&
    (value.evaluation === null || isEvaluation(value.evaluation))
  );
}

function isProposal(value: unknown): value is RfpProposal {
  return (
    isRecord(value) &&
    isString(value.contract_version) &&
    isString(value.status) &&
    typeof value.needs_human_review === "boolean" &&
    isNullableNumber(value.average_iterations_per_section)
  );
}

function isBrief(value: unknown): value is RfpSalesBrief {
  return (
    isRecord(value) &&
    isString(value.summary) &&
    Array.isArray(value.departments) &&
    value.departments.every(
      (dept) => isRecord(dept) && isString(dept.department_id) && isString(dept.owner) && isString(dept.ask) && isStringArray(dept.open_questions),
    ) &&
    isStringArray(value.client_questions) &&
    isStringArray(value.risks) &&
    isStringArray(value.unassigned_requirements)
  );
}

function isMetadata(value: unknown): value is RfpMetadata {
  return (
    isRecord(value) &&
    isNullableString(value.client_name) &&
    isNullableString(value.currency) &&
    isStringArray(value.services_requested) &&
    (value.monthly_volume === null || typeof value.monthly_volume === "number") &&
    isStringArray(value.missing) &&
    isRecord(value.readability) &&
    typeof value.readability.word_count === "number" &&
    typeof value.readability.estimated_tokens === "number"
  );
}

export function isRfpTicketDetail(value: unknown): value is RfpTicketDetail {
  return (
    isRecord(value) &&
    isString(value.ticket_id) &&
    isRfpStatus(value.status) &&
    isString(value.original_filename) &&
    isNullableString(value.current_stage) &&
    isNullableString(value.discard_reason) &&
    isNullableString(value.error_stage) &&
    typeof value.llm_tokens_used === "number" &&
    (value.metadata === null || isMetadata(value.metadata)) &&
    Array.isArray(value.sections) &&
    value.sections.every(isSection) &&
    (value.sales_brief === null || isBrief(value.sales_brief)) &&
    (value.proposal === null || isProposal(value.proposal)) &&
    Array.isArray(value.events) &&
    value.events.every((event) => isRecord(event) && isString(event.node) && isString(event.outcome))
  );
}

// --- Helpers ------------------------------------------------------------------------------

export function isAnalyzing(status: RfpStatus): boolean {
  return status === "analyzing";
}

/** Part 2 is writing or checking drafts. */
export function isDrafting(status: RfpStatus): boolean {
  return status === "drafting" || status === "under_evaluation";
}

/** A background run is working on the ticket (Part 1 or Part 2): keep polling. */
export function isWorking(status: RfpStatus): boolean {
  return isAnalyzing(status) || isDrafting(status);
}

/** The intake is done and its handoff waits in the drafting queue. */
export function isQueuedForDrafting(detail: Pick<RfpTicketDetail, "status" | "handoff">): boolean {
  return detail.status === "intake_complete" && detail.handoff?.status === "pending";
}

/** How long after an intake completes the list keeps watching for drafting to start. */
const DRAFTING_START_WINDOW_MS = 5 * 60 * 1000;

/** The list's re-read delay: fast while a run works, slower just after an intake; else null. */
export function listPollDelay(tickets: Pick<RfpTicketSummary, "status" | "updated_at">[], now = Date.now()): number | null {
  if (tickets.some((ticket) => isWorking(ticket.status))) return POLL_INTERVAL_MS;
  const justCompleted = tickets.some(
    (ticket) => ticket.status === "intake_complete" && now - Date.parse(ticket.updated_at) < DRAFTING_START_WINDOW_MS,
  );
  return justCompleted ? QUEUED_POLL_INTERVAL_MS : null;
}

/** A failed ticket whose intake had finished: it failed while drafting (Part 2). */
export function failedWhileDrafting(detail: Pick<RfpTicketDetail, "status" | "handoff">): boolean {
  return detail.status === "failed" && detail.handoff?.status === "claimed";
}

/** How long to wait before re-reading a ticket; null when nothing will change by itself. */
export function pollDelay(detail: Pick<RfpTicketDetail, "status" | "handoff">): number | null {
  if (isWorking(detail.status)) return POLL_INTERVAL_MS;
  if (isQueuedForDrafting(detail)) return QUEUED_POLL_INTERVAL_MS;
  return null;
}

/** Plain-language check before a file is sent; null when it can be uploaded. */
export function checkPdf(file: { name: string; type: string; size: number }): string | null {
  const looksLikePdf = file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf";
  if (!looksLikePdf) return "Choose a PDF file: RFPs are uploaded as PDFs.";
  if (file.size === 0) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return `That PDF is larger than ${MAX_UPLOAD_MB} MB.`;
  return null;
}

/**
 * "analyze_department:warehouse" -> "Department analysis: Warehouse Operations";
 * "generate_section:reverse#2" -> "Write the draft: Reverse Logistics (attempt 2)".
 */
export function stageLabel(stage: string | null): string {
  if (!stage || stage === "queued") return "Queued";
  const [key, target = ""] = stage.split(":");
  const [department, attempt] = target.split("#");
  const label =
    STAGES.find((item) => item.key === key)?.label ??
    PROPOSAL_STAGES.find((item) => item.key === key)?.label ??
    key.replaceAll("_", " ");
  if (!department) return label;
  const suffix = attempt ? ` (attempt ${attempt})` : "";
  return `${label}: ${DEPARTMENT_NAMES[department] ?? department}${suffix}`;
}

/** A section's progress through the loop, in one plain line. */
export function sectionStatusLine(section: Pick<RfpSection, "draft_status" | "draft_iterations" | "max_iterations">): string {
  const status = section.draft_status ?? "pending";
  const label = SECTION_STATUS_LABELS[status] ?? status;
  if (!section.draft_iterations) return label;
  const of = section.max_iterations ? ` of ${section.max_iterations}` : "";
  return `${label} · attempt ${section.draft_iterations}${of}`;
}

export type StageProgress = { key: string; label: string; state: "done" | "current" | "pending" | "failed" };

/** The run so far, stage by stage, from the ticket's events. */
export function stageProgress(detail: Pick<RfpTicketDetail, "status" | "current_stage" | "events" | "error_stage">): StageProgress[] {
  const base = (node: string) => node.split(":")[0];
  const done = new Set(detail.events.filter((event) => event.outcome === "completed").map((event) => base(event.node)));
  const failedStage = detail.status === "failed" && detail.error_stage ? base(detail.error_stage) : null;
  const current = detail.status === "analyzing" && detail.current_stage ? base(detail.current_stage) : null;
  // The stage running now wins over "done": one department's worker can finish
  // while another is still analysing.
  return STAGES.map(({ key, label }) => {
    if (key === failedStage) return { key, label, state: "failed" };
    if (key === current) return { key, label, state: "current" };
    return { key, label, state: done.has(key) ? "done" : "pending" };
  });
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// --- Calls ---------------------------------------------------------------------------------

class RfpResponseError extends Error {}

const UNREADABLE =
  "The RFP service returned a reply this page cannot read. Please refresh, and contact TrackFlow Tech if it keeps happening.";

export async function uploadRfp(file: File): Promise<RfpTicketAccepted> {
  const form = new FormData();
  form.append("file", file, file.name);
  // No Content-Type: the browser sets multipart/form-data with its boundary.
  const response = await requestAuthenticatedApi(TICKETS_API, { method: "POST", body: form, cache: "no-store" });
  const value = await parseResponseJson(response);
  if (!isRecord(value) || !isString(value.ticket_id) || !isRfpStatus(value.status)) {
    throw new RfpResponseError(UNREADABLE);
  }
  return value as RfpTicketAccepted;
}

export async function listRfpTickets(): Promise<RfpTicketSummary[]> {
  const response = await requestAuthenticatedApi(TICKETS_API, { cache: "no-store" });
  const value = await parseResponseJson(response);
  if (!isRecord(value) || !Array.isArray(value.tickets) || !value.tickets.every(isRfpTicketSummary)) {
    throw new RfpResponseError(UNREADABLE);
  }
  return value.tickets;
}

export async function getRfpTicket(ticketId: string): Promise<RfpTicketDetail> {
  const response = await requestAuthenticatedApi(`${TICKETS_API}/${encodeURIComponent(ticketId)}`, { cache: "no-store" });
  const value = await parseResponseJson(response);
  if (!isRfpTicketDetail(value)) throw new RfpResponseError(UNREADABLE);
  return value;
}

// --- Errors ----------------------------------------------------------------------------------

export const RFP_ERROR_COPY: ErrorCopy = {
  offline: "Could not reach the RFP service. Check your connection and try again.",
  server: "The RFP service is having trouble right now. Please try again in a moment, and contact TrackFlow Tech if it keeps happening.",
  notFound: "That RFP ticket does not exist. Go back to the list to see the latest tickets.",
};

export const STAFF_ONLY_MESSAGE = "RFP intake is available to TrackFlow staff (admin or manager accounts) only.";

/** Frontend-authored copy for the statuses the upload can answer with. */
const STATUS_COPY: Record<number, string> = {
  403: STAFF_ONLY_MESSAGE,
  404: RFP_ERROR_COPY.notFound ?? "",
  413: `That PDF is larger than ${MAX_UPLOAD_MB} MB.`,
  415: "That file is not a PDF the RFP service can read. Upload the RFP as a PDF.",
};

export function describeRfpError(error: unknown): string {
  if (error instanceof RfpResponseError) return error.message;
  if (error instanceof ApiError && STATUS_COPY[error.status]) return STATUS_COPY[error.status];
  return describeError(error, "The upload was refused. Check the file and try again.", RFP_ERROR_COPY).message;
}

export function isStaffOnlyError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}
