import {
  MAX_UPLOAD_BYTES,
  POLL_INTERVAL_MS,
  QUEUED_POLL_INTERVAL_MS,
  RULE_LABELS,
  STAFF_ONLY_MESSAGE,
  checkPdf,
  describeRfpError,
  failedWhileDrafting,
  getRfpTicket,
  isAnalyzing,
  isRfpTicketDetail,
  isWorking,
  listPollDelay,
  listRfpTickets,
  pollDelay,
  sectionStatusLine,
  stageLabel,
  stageProgress,
  uploadRfp,
  type RfpEvaluation,
  type RfpSection,
  type RfpTicketDetail,
} from "@/lib/rfp";
import { parseSectionMarkdown } from "@/components/rfp/ProposalSectionCard";
import { NetworkError } from "@/lib/api-client";
import { clearToken, storeToken } from "@/lib/auth-storage";
import { installBrowser, removeBrowser } from "../shared/support/browser";
import { API, jsonResponse, scriptFetch, textResponse } from "../shared/support/http";

beforeEach(() => { installBrowser(); storeToken("staff-session"); });
afterEach(() => { clearToken(); removeBrowser(); });

const TICKET = "8a7b5d4e-1c2f-4a3b-9c8d-7e6f5a4b3c2d";

const SUMMARY = {
  ticket_id: TICKET,
  rfp_id: "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
  status: "analyzing",
  current_stage: "classify_document",
  original_filename: "trackflow-request-1.pdf",
  client_name: null,
  client_country: null,
  departments_needed: [],
  created_at: "2026-11-21T10:00:00Z",
  updated_at: "2026-11-21T10:00:03Z",
};

// A section before Part 2 has drafted it.
const NOT_DRAFTED = {
  draft_status: null, draft_iterations: 0, max_iterations: null, is_sla_section: false, needs_human_review: false,
  draft_content: null, draft: null, guard_notes: [], knowledge_source: null, rate_card_version: null, review_note: null,
  evaluation: null, approval_status: null, ready_at: null,
};

function evaluation(overrides: Partial<RfpEvaluation> = {}): RfpEvaluation {
  return {
    section_id: "5c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f", department_id: "reverse", iteration: 3,
    readability: { pass: true, score: 77, details: { scored: true, flesch_kincaid_grade: 5.2, min_ease: 30, max_grade: 14 } },
    relevance: { pass: true, missing_aspects: [], covered_aspects: ["Returns scope"], not_applicable: [] },
    compliance: {
      pass: false, rule_ids: ["returns-min-48h"], checked_rule_ids: ["currency-by-country", "returns-min-48h"], reviewer: "used",
      violations: [{ rule_id: "returns-min-48h", message: "A returns promise under 48 hours (24 hours).", quote: "Returns are processed within 24 hours of arrival.", source: "check" }],
    },
    overall_pass: false,
    feedback_for_generator: "1. [returns-min-48h] A returns promise under 48 hours.",
    evaluated_at: "2026-11-23T10:02:00Z",
    ...overrides,
  };
}

function draftedSection(overrides: Partial<RfpSection> = {}): RfpSection {
  return {
    department_id: "reverse", department_name: "Reverse Logistics", owner: "Sofía Ramos", objective: null, extracts: [],
    key_aspects: [], needed_from_department: [], open_questions: [], validation_notes: [],
    ...NOT_DRAFTED,
    draft_status: "needs_human_review", draft_iterations: 3, max_iterations: 3, needs_human_review: true,
    draft_content: "## Reverse Logistics: ModaViva\n\nWe handle your returns.\n",
    draft: {
      summary: "We handle your returns.", narrative: [], assumptions: [], open_questions: [], on_time_sla_pct: null,
      price_lines: [{ item_id: "return_processing", amount: 2.8, currency: "EUR", note: "" }],
      discount_tiers: [{ min_monthly_volume: 0, max_monthly_volume: 499, discount_pct: 0 }],
    },
    knowledge_source: "local", rate_card_version: "2026-11-indicative-1", evaluation: evaluation(), approval_status: "pending",
    ready_at: "2026-11-23T10:02:01Z",
    ...overrides,
  };
}

function detail(overrides: Partial<RfpTicketDetail> = {}): RfpTicketDetail {
  return {
    ticket_id: TICKET,
    rfp_id: SUMMARY.rfp_id,
    status: "intake_complete",
    current_stage: null,
    original_filename: "trackflow-request-1.pdf",
    pdf_bytes: 3692,
    created_at: SUMMARY.created_at,
    updated_at: SUMMARY.updated_at,
    completed_at: "2026-11-21T10:00:30Z",
    classification: { decision: "accept", document_kind: "formal_rfp", confidence: 0.98, language: "mixed", reasons: [], flags: [] },
    discard_reason: null,
    error_stage: null,
    error_message: null,
    llm_tokens_used: 7023,
    markdown_chars: 1371,
    markdown_truncated: false,
    metadata: {
      client_name: "ModaViva, S.L.",
      client_country: "ES",
      currency: "EUR",
      services_requested: ["warehousing", "reverse_logistics"],
      monthly_volume: null,
      deadline: "2026-08-28",
      deadline_text: "Proposal Due Date: August 28, 2026",
      deadline_kind: "proposal_due",
      budget_range: null,
      departments_needed: ["warehouse", "reverse"],
      evidence: {},
      missing: ["monthly_volume", "budget_range"],
      notes: [],
      readability: {
        language: "mixed", word_count: 195, sentence_count: 13, char_count: 1371, estimated_tokens: 343,
        flesch_reading_ease: 17.64, flesch_kincaid_grade: 14.38, gunning_fog: 11.13, processing_cost: "low", note: null,
      },
    },
    sections: [
      {
        department_id: "warehouse", department_name: "Warehouse Operations", owner: "Ana Whitfield", objective: "Size storage.",
        extracts: ["Almacenamiento de aproximadamente 15.000 SKUs"],
        key_aspects: [{ aspect: "Storage", detail: "About 15,000 SKUs", source_quote: "Almacenamiento de aproximadamente 15.000 SKUs" }],
        needed_from_department: ["Cost per pallet and per SKU in EUR"], open_questions: ["What monthly order volume should we plan for?"],
        validation_notes: [],
        ...NOT_DRAFTED,
      },
    ],
    sales_brief: {
      summary: "ModaViva requests warehousing and returns.", client_name: "ModaViva, S.L.", client_country: "ES", currency: "EUR",
      commercial_owner: "Miguel Torres",
      departments: [{ department_id: "warehouse", department_name: "Warehouse Operations", owner: "Ana Whitfield", ask: "Quote storage.", needed_from_department: [], open_questions: [] }],
      client_questions: ["Ask for the expected monthly order volume: the RFP does not state it."], risks: [], unassigned_requirements: ["ERP integration"],
      validation_notes: [],
    },
    handoff: { contract_version: "rfp-intake/1", next_stage: "drafting", status: "pending", created_at: "2026-11-21T10:00:30Z", claimed_at: null },
    proposal: null,
    events: [
      { node: "convert_to_markdown", outcome: "completed", detail: null, duration_ms: 40, created_at: "2026-11-21T10:00:01Z" },
      { node: "classify_document", outcome: "completed", detail: null, duration_ms: 1500, created_at: "2026-11-21T10:00:03Z" },
    ],
    ...overrides,
  };
}

it("uploads the PDF as multipart form data with the session token", async () => {
  const requests = scriptFetch(jsonResponse(202, { ticket_id: TICKET, rfp_id: SUMMARY.rfp_id, status: "analyzing", original_filename: "rfp.pdf" }));
  const file = new File([new Uint8Array([37, 80, 68, 70, 45])], "rfp.pdf", { type: "application/pdf" });

  const accepted = await uploadRfp(file);

  expect(accepted.ticket_id).toBe(TICKET);
  expect(requests[0]).toMatchObject({ url: `${API}/rfp/tickets`, method: "POST" });
  expect(requests[0].headers.Authorization).toBe("Bearer staff-session");
  // The browser must set the multipart boundary itself.
  expect(requests[0].headers["Content-Type"]).toBeUndefined();
  const body = requests[0].body as FormData;
  expect(body).toBeInstanceOf(FormData);
  expect((body.get("file") as File).name).toBe("rfp.pdf");
});

it("lists tickets and reads one ticket", async () => {
  const requests = scriptFetch(jsonResponse(200, { tickets: [SUMMARY] }), jsonResponse(200, detail()));

  expect(await listRfpTickets()).toEqual([SUMMARY]);
  expect((await getRfpTicket(TICKET)).sales_brief?.departments[0].owner).toBe("Ana Whitfield");
  expect(requests.map((request) => request.url)).toEqual([`${API}/rfp/tickets`, `${API}/rfp/tickets/${TICKET}`]);
});

it.each([
  ["a list without tickets", { items: [] }],
  ["a ticket with an unknown status", { tickets: [{ ...SUMMARY, status: "approved" }] }],
  ["a ticket without an id", { tickets: [{ ...SUMMARY, ticket_id: 7 }] }],
])("refuses %s rather than showing an empty list", async (_label, body) => {
  scriptFetch(jsonResponse(200, body));
  await expect(listRfpTickets()).rejects.toThrow("cannot read");
});

it("refuses a ticket reply it cannot render", async () => {
  scriptFetch(jsonResponse(200, { ...detail(), sections: [{ department_id: "warehouse" }] }), textResponse(200, "<html>proxy</html>"));
  await expect(getRfpTicket(TICKET)).rejects.toThrow("cannot read");
  await expect(getRfpTicket(TICKET)).rejects.toThrow("cannot read");
});

it("validates complete, discarded and still-analysing tickets", () => {
  expect(isRfpTicketDetail(detail())).toBe(true);
  expect(isRfpTicketDetail(detail({ status: "discarded", metadata: null, sections: [], sales_brief: null, handoff: null, discard_reason: "Not a client RFP." }))).toBe(true);
  expect(isRfpTicketDetail(detail({ status: "analyzing", metadata: null, sections: [], sales_brief: null, handoff: null }))).toBe(true);
  expect(isRfpTicketDetail({ ...detail(), llm_tokens_used: "many" })).toBe(false);
});

it.each([
  [403, STAFF_ONLY_MESSAGE],
  [413, "That PDF is larger than 20 MB."],
  [415, "That file is not a PDF the RFP service can read. Upload the RFP as a PDF."],
  [404, "That RFP ticket does not exist. Go back to the list to see the latest tickets."],
])("explains a %i in TrackFlow's own words", async (status, message) => {
  scriptFetch(jsonResponse(status, { detail: "server text" }));
  const failure = await listRfpTickets().catch((error: unknown) => error);
  expect(describeRfpError(failure)).toBe(message);
});

it("keeps server failures and network errors generic", async () => {
  scriptFetch(jsonResponse(503, { detail: "RFP storage unavailable" }));
  const failure = await listRfpTickets().catch((error: unknown) => error);

  expect(describeRfpError(failure)).toContain("RFP service is having trouble");
  expect(describeRfpError(new NetworkError(new TypeError("Failed to fetch")))).toContain("Could not reach the RFP service");
});

it("checks the file before uploading it", () => {
  expect(checkPdf({ name: "rfp.PDF", type: "", size: 10 })).toBeNull();
  expect(checkPdf({ name: "scan", type: "application/pdf", size: 10 })).toBeNull();
  expect(checkPdf({ name: "notes.docx", type: "application/msword", size: 10 })).toContain("Choose a PDF");
  expect(checkPdf({ name: "rfp.pdf", type: "application/pdf", size: 0 })).toBe("That file is empty.");
  expect(checkPdf({ name: "rfp.pdf", type: "application/pdf", size: MAX_UPLOAD_BYTES + 1 })).toContain("larger than 20 MB");
});

it("names stages in plain language, departments included", () => {
  expect(stageLabel(null)).toBe("Queued");
  expect(stageLabel("queued")).toBe("Queued");
  expect(stageLabel("classify_document")).toBe("Check it is an RFP");
  expect(stageLabel("analyze_department:lastmile")).toBe("Department analysis: Last Mile and Carrier Management");
  expect(isAnalyzing("analyzing")).toBe(true);
  expect(isAnalyzing("intake_complete")).toBe(false);
});

it("shows the run's progress from its events", () => {
  const running = stageProgress(detail({ status: "analyzing", current_stage: "measure_readability" }));
  expect(running.slice(0, 4).map((step) => step.state)).toEqual(["done", "done", "current", "pending"]);

  const failed = stageProgress(detail({ status: "failed", error_stage: "analyze_department:reverse", current_stage: "analyze_department:reverse" }));
  expect(failed.find((step) => step.key === "analyze_department")?.state).toBe("failed");
});

// --- Part 2: proposal drafting -------------------------------------------------------------

const PROPOSAL = {
  contract_version: "rfp-proposal/1", next_stage: "approval", status: "pending", needs_human_review: true,
  sla_department: "warehouse", currency: "EUR", rate_card_version: "2026-11-indicative-1", max_iterations: 3,
  average_iterations_per_section: 2, passed_first_attempt: 1, created_at: "2026-11-23T10:02:02Z", claimed_at: null,
};

it("validates drafted tickets, passed and sent for human review", () => {
  const passed = draftedSection({ draft_status: "passed", needs_human_review: false, draft_iterations: 1, evaluation: evaluation({ overall_pass: true, feedback_for_generator: "" }) });
  const handoff = { ...detail().handoff!, status: "claimed" };

  expect(isRfpTicketDetail(detail({ status: "waiting_for_approval", sections: [passed], handoff, proposal: { ...PROPOSAL, needs_human_review: false } }))).toBe(true);
  expect(isRfpTicketDetail(detail({ status: "needs_human_review", sections: [draftedSection()], handoff, proposal: PROPOSAL }))).toBe(true);
  expect(isRfpTicketDetail(detail({ status: "under_evaluation", sections: [draftedSection({ draft_status: "under_evaluation", evaluation: null })], handoff }))).toBe(true);
});

it.each([
  ["an evaluation without the pass flags", { ...evaluation(), compliance: { rule_ids: [], violations: [] } }],
  ["an evaluation without feedback", { ...evaluation(), feedback_for_generator: undefined }],
])("refuses a section with %s", (_label, broken) => {
  const section = { ...draftedSection(), evaluation: broken } as unknown as RfpSection;
  expect(isRfpTicketDetail(detail({ status: "needs_human_review", sections: [section] }))).toBe(false);
});

it("polls while a run works on the ticket and while it waits in the drafting queue", () => {
  expect(isWorking("drafting") && isWorking("under_evaluation") && isWorking("analyzing")).toBe(true);
  expect(isWorking("needs_human_review") || isWorking("waiting_for_approval")).toBe(false);
  expect(pollDelay(detail({ status: "under_evaluation" }))).toBe(POLL_INTERVAL_MS);
  expect(pollDelay(detail())).toBe(QUEUED_POLL_INTERVAL_MS); // intake_complete, handoff pending
  expect(pollDelay(detail({ status: "waiting_for_approval", handoff: { ...detail().handoff!, status: "claimed" } }))).toBeNull();

  const now = Date.parse("2026-11-23T10:00:00Z");
  const recent = { status: "intake_complete" as const, updated_at: "2026-11-23T09:59:00Z" };
  expect(listPollDelay([{ status: "drafting", updated_at: SUMMARY.updated_at }], now)).toBe(POLL_INTERVAL_MS);
  expect(listPollDelay([recent], now)).toBe(QUEUED_POLL_INTERVAL_MS);
  expect(listPollDelay([{ ...recent, updated_at: "2026-11-23T08:00:00Z" }], now)).toBeNull();
});

it("names the loop's stages with the department and the attempt", () => {
  expect(stageLabel("generate_section:reverse#2")).toBe("Write the draft: Reverse Logistics (attempt 2)");
  expect(stageLabel("check_compliance:warehouse#1")).toBe("Check the company guidelines: Warehouse Operations (attempt 1)");
  expect(stageLabel("retrieve_knowledge:lastmile")).toBe("Look up TrackFlow's policies: Last Mile and Carrier Management");
  expect(stageLabel("synthesize_proposal")).toBe("Assemble the approval tickets");
  expect(sectionStatusLine(draftedSection())).toBe("Needs human review · attempt 3 of 3");
  expect(sectionStatusLine({ draft_status: null, draft_iterations: 0, max_iterations: null })).toBe("Waiting to be drafted");
});

it("explains every guideline of briefing section 5 in plain words", () => {
  expect(Object.keys(RULE_LABELS).sort()).toEqual(
    ["currency-by-country", "no-carrier-rates", "on-time-sla-stated", "returns-min-48h", "volume-discount-table"],
  );
});

it("tells a drafting failure from an analysis failure", () => {
  const claimed = { ...detail().handoff!, status: "claimed" };
  expect(failedWhileDrafting(detail({ status: "failed", handoff: claimed }))).toBe(true);
  expect(failedWhileDrafting(detail({ status: "failed", handoff: null }))).toBe(false);
});

it("reads the section Markdown as headings, paragraphs, lists and tables, never HTML", () => {
  const blocks = parseSectionMarkdown(
    "## Title\n\nOne paragraph.\n\n### Pricing\n\n| Item | Price |\n|---|---|\n| Storage | 16.00 EUR |\n\n- First\n- <b>Second</b>\n",
  );

  expect(blocks).toEqual([
    { kind: "heading", text: "Title" },
    { kind: "paragraph", text: "One paragraph." },
    { kind: "heading", text: "Pricing" },
    { kind: "table", header: ["Item", "Price"], rows: [["Storage", "16.00 EUR"]], caption: "Pricing" },
    { kind: "list", items: ["First", "<b>Second</b>"] },
  ]);
});
