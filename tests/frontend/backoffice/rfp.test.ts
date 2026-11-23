import {
  MAX_UPLOAD_BYTES,
  STAFF_ONLY_MESSAGE,
  checkPdf,
  describeRfpError,
  getRfpTicket,
  isAnalyzing,
  isRfpTicketDetail,
  listRfpTickets,
  stageLabel,
  stageProgress,
  uploadRfp,
  type RfpTicketDetail,
} from "@/lib/rfp";
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
