/**
 * RFP intake, as the backoffice reads it (Milestone 9 Part 1).
 *
 * Three calls, all with the session token and staff-only on the API:
 * `POST /rfp/tickets` (multipart PDF upload, answers 202 at once),
 * `GET /rfp/tickets` and `GET /rfp/tickets/{id}`. The analysis runs in the
 * background, so the pages poll while a ticket is `analyzing`. Every reply is
 * validated before it is rendered: a reply the page cannot read is an error,
 * never an empty ticket. Shapes mirror `services/rfp/models.py`.
 */

import { ApiError, parseResponseJson, requestAuthenticatedApi } from "@/lib/api-client";
import { describeError, type ErrorCopy } from "@/lib/friendly-error";

export const RFP_PATH = "/rfp";
const TICKETS_API = "/rfp/tickets";

/** Matches `RFP_MAX_UPLOAD_MB`'s default in `data/pipelines/rfp_intake/config.py`. */
export const MAX_UPLOAD_MB = 20;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
/** How often an `analyzing` ticket is re-read. */
export const POLL_INTERVAL_MS = 3000;

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
    isStringArray(value.open_questions)
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
    Array.isArray(value.events) &&
    value.events.every((event) => isRecord(event) && isString(event.node) && isString(event.outcome))
  );
}

// --- Helpers ------------------------------------------------------------------------------

export function isAnalyzing(status: RfpStatus): boolean {
  return status === "analyzing";
}

/** Plain-language check before a file is sent; null when it can be uploaded. */
export function checkPdf(file: { name: string; type: string; size: number }): string | null {
  const looksLikePdf = file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf";
  if (!looksLikePdf) return "Choose a PDF file: RFPs are uploaded as PDFs.";
  if (file.size === 0) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return `That PDF is larger than ${MAX_UPLOAD_MB} MB.`;
  return null;
}

/** "analyze_department:warehouse" -> "Department analysis: Warehouse Operations". */
export function stageLabel(stage: string | null): string {
  if (!stage || stage === "queued") return "Queued";
  const [key, department] = stage.split(":");
  const label = STAGES.find((item) => item.key === key)?.label ?? key.replaceAll("_", " ");
  return department ? `${label}: ${DEPARTMENT_NAMES[department] ?? department}` : label;
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
