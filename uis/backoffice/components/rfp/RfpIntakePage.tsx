"use client";

import Link from "next/link";
import { type ChangeEvent, type DragEvent, useEffect, useId, useState } from "react";
import { StateMessage } from "@/components/common/StateMessage";
import { RfpStatusBadge } from "@/components/rfp/RfpStatusBadge";
import {
  COUNTRY_NAMES,
  DEPARTMENT_NAMES,
  MAX_UPLOAD_MB,
  RFP_PATH,
  STAFF_ONLY_MESSAGE,
  checkPdf,
  describeRfpError,
  isAnalyzing,
  isStaffOnlyError,
  isWorking,
  listPollDelay,
  listRfpTickets,
  stageLabel,
  uploadRfp,
  type RfpTicketSummary,
} from "@/lib/rfp";

type UploadResult =
  | { name: string; state: "accepted"; ticketId: string }
  | { name: string; state: "refused"; message: string };

const secondaryButtonClass =
  "inline-flex min-h-10 items-center rounded-xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] px-4 py-2 text-sm font-semibold text-[color:var(--brand-primary)] transition hover:border-[color:var(--brand-primary)] focus-visible:outline-2 focus-visible:outline-offset-2";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

/**
 * RFP intake (Milestone 9 Part 1): upload PDFs, one ticket each, and watch them
 * move from "Analyzing" to "Intake complete" or "Discarded" without reloading.
 */
export function RfpIntakePage() {
  const [tickets, setTickets] = useState<RfpTicketSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [staffOnly, setStaffOnly] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [uploading, setUploading] = useState<string | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputId = useId();
  const hintId = useId();

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    listRfpTickets()
      .then((items) => {
        if (!active) return;
        setTickets(items);
        setListError(null);
        // Keep reading while anything is being analysed or drafted.
        const delay = listPollDelay(items);
        if (delay !== null) {
          timer = setTimeout(() => setRefresh((value) => value + 1), delay);
        }
      })
      .catch((failure: unknown) => {
        if (!active) return;
        setStaffOnly(isStaffOnlyError(failure));
        setListError(describeRfpError(failure));
      });
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  async function uploadFiles(files: File[]) {
    if (files.length === 0 || uploading) return;
    const outcomes: UploadResult[] = [];
    for (const file of files) {
      const problem = checkPdf(file);
      if (problem) {
        outcomes.push({ name: file.name, state: "refused", message: problem });
        continue;
      }
      setUploading(file.name);
      try {
        const ticket = await uploadRfp(file);
        outcomes.push({ name: file.name, state: "accepted", ticketId: ticket.ticket_id });
      } catch (failure: unknown) {
        outcomes.push({ name: file.name, state: "refused", message: describeRfpError(failure) });
        if (isStaffOnlyError(failure)) setStaffOnly(true);
      }
    }
    setUploading(null);
    setResults(outcomes);
    setRefresh((value) => value + 1);
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    void uploadFiles(files);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void uploadFiles(Array.from(event.dataTransfer.files));
  }

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 md:px-10">
      <header className="mb-6 border-b border-[color:var(--border-soft)] pb-5">
        <p className="mb-2 text-sm font-medium text-slate-600">Commercial team</p>
        <h1 className="text-2xl font-semibold leading-tight">RFP intake</h1>
        <p className="mt-2 max-w-3xl text-sm text-slate-600">
          Upload each request for proposal as a PDF. TrackFlow checks that it really is an RFP, reads the
          client&apos;s details and tells you what to ask Warehouse, Last Mile and Reverse Logistics, so you
          don&apos;t have to read the whole document first.
        </p>
      </header>

      {staffOnly ? (
        <StateMessage tone="error">{STAFF_ONLY_MESSAGE}</StateMessage>
      ) : (
        <>
          <section aria-labelledby="upload-heading">
            <h2 id="upload-heading" className="sr-only">
              Upload RFPs
            </h2>
            <div
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              className={`rounded-2xl border-2 border-dashed p-6 transition ${
                dragging
                  ? "border-[color:var(--brand-primary)] bg-blue-50"
                  : "border-[color:var(--border-soft)] bg-[color:var(--surface)]"
              }`}
            >
              <label htmlFor={inputId} className="block text-base font-semibold">
                Drop RFP PDFs here, or choose files
              </label>
              <p id={hintId} className="mt-1 text-sm text-slate-600">
                One ticket per PDF, up to {MAX_UPLOAD_MB} MB each. The analysis runs in the background and
                usually takes under a minute.
              </p>
              <input
                id={inputId}
                type="file"
                accept="application/pdf,.pdf"
                multiple
                disabled={uploading !== null}
                onChange={onPick}
                aria-describedby={hintId}
                className="mt-4 block w-full max-w-md text-sm text-slate-600 file:mr-4 file:min-h-11 file:cursor-pointer file:rounded-xl file:border-0 file:bg-[color:var(--brand-primary)] file:px-5 file:py-2.5 file:text-sm file:font-semibold file:text-[color:var(--on-brand)] hover:file:bg-[color:var(--brand-secondary)] disabled:opacity-60"
              />
              <div aria-live="polite" className="mt-4 space-y-2">
                {uploading ? (
                  <StateMessage tone="info">
                    Uploading <span className="font-semibold">{uploading}</span>…
                  </StateMessage>
                ) : null}
                {!uploading
                  ? results.map((result, index) =>
                      result.state === "accepted" ? (
                        <StateMessage tone="success" key={`${result.name}-${index}`}>
                          <span className="font-semibold">{result.name}</span> was accepted and is being analysed.{" "}
                          <Link href={`${RFP_PATH}/${result.ticketId}`} className="font-semibold underline">
                            Open its ticket
                          </Link>
                        </StateMessage>
                      ) : (
                        <StateMessage tone="error" key={`${result.name}-${index}`}>
                          <span className="font-semibold">{result.name}</span>: {result.message}
                        </StateMessage>
                      ),
                    )
                  : null}
              </div>
            </div>
          </section>

          <section aria-labelledby="tickets-heading" className="mt-10">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
              <h2 id="tickets-heading" className="text-lg font-semibold">
                Tickets
              </h2>
              <p className="text-sm text-slate-600" role="status">
                {tickets
                  ? `${tickets.length} ${tickets.length === 1 ? "ticket" : "tickets"}, newest first`
                  : listError
                    ? "Tickets unavailable"
                    : "Loading tickets…"}
              </p>
            </div>

            {listError ? (
              <div role="alert">
                <StateMessage tone="error">
                  {listError}
                  <div className="mt-3">
                    <button type="button" className={secondaryButtonClass} onClick={() => setRefresh((v) => v + 1)}>
                      Try again
                    </button>
                  </div>
                </StateMessage>
              </div>
            ) : null}

            {tickets?.length === 0 ? (
              <StateMessage tone="info">No RFPs yet. Upload the first one above.</StateMessage>
            ) : null}

            {tickets && tickets.length > 0 ? (
              <div className="overflow-x-auto border-y border-[color:var(--border-soft)] bg-[color:var(--surface)]">
                <table className="block w-full text-left text-sm sm:table sm:min-w-[760px]">
                  <caption className="sr-only">RFP tickets, newest first</caption>
                  <thead className="hidden border-b border-[color:var(--border-soft)] bg-slate-50 text-slate-600 sm:table-header-group">
                    <tr>
                      {["Document", "Client", "Status", "Departments", "Uploaded"].map((label) => (
                        <th scope="col" key={label} className="px-3 py-3 font-semibold">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="block divide-y divide-slate-100 sm:table-row-group">
                    {tickets.map((ticket) => (
                      <tr key={ticket.ticket_id} className="grid grid-cols-2 gap-3 p-4 sm:table-row sm:p-0">
                        <th scope="row" className="col-span-2 min-w-0 break-words font-semibold sm:px-3 sm:py-4">
                          <Link
                            href={`${RFP_PATH}/${ticket.ticket_id}`}
                            className="text-[color:var(--brand-primary)] underline-offset-2 hover:underline"
                          >
                            {ticket.original_filename}
                          </Link>
                        </th>
                        <td className="min-w-0 sm:px-3 sm:py-4">
                          <span className="mb-1 block text-xs text-slate-600 sm:hidden">Client</span>
                          {ticket.client_name ?? <span className="text-slate-500">Not known yet</span>}
                          {ticket.client_country ? (
                            <span className="block text-xs text-slate-600">
                              {COUNTRY_NAMES[ticket.client_country] ?? ticket.client_country}
                            </span>
                          ) : null}
                        </td>
                        <td className="min-w-0 sm:px-3 sm:py-4">
                          <span className="mb-1 block text-xs text-slate-600 sm:hidden">Status</span>
                          <RfpStatusBadge status={ticket.status} />
                          {isWorking(ticket.status) || ticket.status === "waiting_for_approval" ? (
                            <span className="mt-1 block text-xs text-slate-600">{stageLabel(ticket.current_stage)}</span>
                          ) : null}
                        </td>
                        <td className="min-w-0 sm:px-3 sm:py-4">
                          <span className="mb-1 block text-xs text-slate-600 sm:hidden">Departments</span>
                          {ticket.departments_needed.length > 0 ? (
                            ticket.departments_needed.map((id) => DEPARTMENT_NAMES[id] ?? id).join(", ")
                          ) : (
                            <span className="text-slate-500">{isAnalyzing(ticket.status) ? "Not decided yet" : "None"}</span>
                          )}
                        </td>
                        <td className="min-w-0 sm:px-3 sm:py-4">
                          <span className="mb-1 block text-xs text-slate-600 sm:hidden">Uploaded</span>
                          <time dateTime={ticket.created_at}>{formatDate(ticket.created_at)}</time>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </section>
        </>
      )}
    </main>
  );
}
