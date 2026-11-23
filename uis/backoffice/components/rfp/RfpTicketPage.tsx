"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";
import { StateMessage } from "@/components/common/StateMessage";
import { RfpStatusBadge } from "@/components/rfp/RfpStatusBadge";
import {
  COUNTRY_NAMES,
  POLL_INTERVAL_MS,
  RFP_PATH,
  SERVICE_LABELS,
  describeRfpError,
  formatBytes,
  getRfpTicket,
  isAnalyzing,
  stageLabel,
  stageProgress,
  type RfpMetadata,
  type RfpSalesBrief,
  type RfpSection,
  type RfpTicketDetail,
} from "@/lib/rfp";

const cardClass =
  "rounded-2xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] p-5 shadow-[0_12px_32px_-24px_rgba(37,99,235,0.35)]";
const labelClass = "text-xs font-semibold uppercase tracking-wide text-slate-500";

const DOCUMENT_KINDS: Record<string, string> = {
  formal_rfp: "Formal RFP",
  informal_rfp: "Informal RFP",
  carrier_or_vendor_offer: "A carrier or vendor offering its services to TrackFlow",
  other: "Not a request for a proposal",
};

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatDay(value: string): string {
  // A calendar date with no time: format it in UTC so it never shifts a day.
  return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" }).format(
    new Date(`${value}T00:00:00Z`),
  );
}

/** A value the RFP does not give: said plainly, never left blank. */
function Missing({ children }: { children: ReactNode }) {
  return <span className="font-semibold text-orange-700">{children}</span>;
}

function List({ items, empty }: { items: string[]; empty?: string }) {
  if (items.length === 0) return empty ? <p className="mt-1 text-sm text-slate-500">{empty}</p> : null;
  return (
    <ul className="mt-1 list-disc space-y-1 pl-5 text-sm leading-relaxed">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

function ClientCard({ metadata }: { metadata: RfpMetadata }) {
  const country = metadata.client_country ? COUNTRY_NAMES[metadata.client_country] ?? metadata.client_country : null;
  const rows: [string, ReactNode][] = [
    ["Client", metadata.client_name ?? <Missing>Not stated: confirm with the client</Missing>],
    [
      "Country and currency",
      country ? (
        `${country} · quote in ${metadata.currency}`
      ) : (
        <Missing>Not established: ask the client (it sets USD or EUR)</Missing>
      ),
    ],
    [
      "Services requested",
      metadata.services_requested.length > 0
        ? metadata.services_requested.map((service) => SERVICE_LABELS[service] ?? service).join(", ")
        : <Missing>None stated</Missing>,
    ],
    [
      "Monthly volume",
      metadata.monthly_volume !== null ? (
        `${metadata.monthly_volume.toLocaleString()} orders a month`
      ) : (
        <Missing>Not stated in the RFP: ask the client</Missing>
      ),
    ],
    [
      metadata.deadline_kind === "service_start" ? "Wanted live by" : "Deadline",
      metadata.deadline ? (
        formatDay(metadata.deadline)
      ) : metadata.deadline_text ? (
        <>
          “{metadata.deadline_text}” <Missing>(no full date: confirm it)</Missing>
        </>
      ) : (
        <Missing>Not stated: ask the client</Missing>
      ),
    ],
    ["Reference budget", metadata.budget_range ?? <span className="text-slate-600">None given</span>],
  ];
  return (
    <section aria-labelledby="client-heading" className={cardClass}>
      <h2 id="client-heading" className="text-lg font-semibold">
        The request
      </h2>
      <dl className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt className={labelClass}>{label}</dt>
            <dd className="mt-0.5 text-sm">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function BriefCard({ brief }: { brief: RfpSalesBrief }) {
  return (
    <section aria-labelledby="brief-heading" className={cardClass}>
      <h2 id="brief-heading" className="text-lg font-semibold">
        Sales brief
      </h2>
      <p className="mt-2 text-base leading-relaxed">{brief.summary}</p>
      <div className="mt-5 grid gap-5 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold">Ask the client</h3>
          <List items={brief.client_questions} empty="Nothing: the RFP answers what the departments need." />
        </div>
        <div>
          <h3 className="text-sm font-semibold">Risks for the proposal</h3>
          <List items={brief.risks} empty="None flagged." />
        </div>
      </div>
      {brief.unassigned_requirements.length > 0 ? (
        <div className="mt-5">
          <h3 className="text-sm font-semibold">Not covered by Warehouse, Last Mile or Reverse Logistics</h3>
          <List items={brief.unassigned_requirements} />
        </div>
      ) : null}
    </section>
  );
}

function DepartmentCard({ section, ask }: { section: RfpSection; ask: string | null }) {
  const headingId = `dept-${section.department_id}`;
  return (
    <article aria-labelledby={headingId} className={cardClass}>
      <p className={labelClass}>{section.department_name}</p>
      <h3 id={headingId} className="mt-1 text-lg font-semibold">
        Ask {section.owner}
      </h3>
      {ask ? <p className="mt-2 text-sm leading-relaxed">{ask}</p> : null}

      <h4 className="mt-4 text-sm font-semibold">What they must provide</h4>
      <List items={section.needed_from_department} empty="Nothing specific beyond their standard quote." />

      <h4 className="mt-4 text-sm font-semibold">Open questions</h4>
      <List items={section.open_questions} empty="None: the RFP gives what this department needs." />

      {section.key_aspects.length > 0 ? (
        <details className="mt-4 group">
          <summary className="cursor-pointer text-sm font-semibold text-[color:var(--brand-primary)]">
            What the RFP says ({section.key_aspects.length})
          </summary>
          <ul className="mt-2 space-y-3">
            {section.key_aspects.map((aspect) => (
              <li key={`${aspect.aspect}-${aspect.source_quote}`} className="text-sm">
                <p>
                  <span className="font-semibold">{aspect.aspect}:</span> {aspect.detail}
                </p>
                <blockquote className="mt-1 border-l-2 border-[color:var(--border-soft)] pl-3 text-xs italic text-slate-600">
                  “{aspect.source_quote}”
                </blockquote>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

function CostCard({ ticket }: { ticket: RfpTicketDetail }) {
  const readability = ticket.metadata?.readability;
  if (!readability) return null;
  const tier = { low: "Low", medium: "Medium", high: "High" }[readability.processing_cost];
  const rows: [string, string][] = [
    ["Processing cost", tier],
    ["Length", `${readability.word_count.toLocaleString()} words · ~${readability.estimated_tokens.toLocaleString()} tokens`],
    [
      "Reading level",
      readability.flesch_kincaid_grade !== null
        ? `Grade ${readability.flesch_kincaid_grade} (Flesch ${readability.flesch_reading_ease}, Fog ${readability.gunning_fog})`
        : "Not scored",
    ],
    ["Model tokens used", ticket.llm_tokens_used.toLocaleString()],
  ];
  return (
    <section aria-labelledby="cost-heading" className={cardClass}>
      <h2 id="cost-heading" className="text-lg font-semibold">
        Processing cost
      </h2>
      <dl className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt className={labelClass}>{label}</dt>
            <dd className="mt-0.5 text-sm tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {readability.note ? <p className="mt-3 text-xs text-slate-600">{readability.note}</p> : null}
    </section>
  );
}

function Progress({ ticket }: { ticket: RfpTicketDetail }) {
  const steps = stageProgress(ticket);
  const marks = { done: "✓", current: "…", pending: "", failed: "✕" };
  const tones = {
    done: "text-emerald-800",
    current: "font-semibold text-[color:var(--brand-primary)]",
    pending: "text-slate-500",
    failed: "font-semibold text-red-700",
  };
  return (
    <ol className="mt-3 grid gap-1.5 text-sm sm:grid-cols-2">
      {steps.map((step) => (
        <li key={step.key} className={`flex items-center gap-2 ${tones[step.state]}`}>
          <span aria-hidden="true" className="inline-block w-4 text-center">
            {marks[step.state]}
          </span>
          {step.label}
          <span className="sr-only">: {step.state}</span>
        </li>
      ))}
    </ol>
  );
}

/** One RFP ticket, as Sales reads it: what is needed from each department and who to ask. */
export function RfpTicketPage({ ticketId }: { ticketId: string }) {
  const [ticket, setTicket] = useState<RfpTicketDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    getRfpTicket(ticketId)
      .then((detail) => {
        if (!active) return;
        setTicket(detail);
        setError(null);
        if (isAnalyzing(detail.status)) {
          timer = setTimeout(() => setRefresh((value) => value + 1), POLL_INTERVAL_MS);
        }
      })
      .catch((failure: unknown) => {
        if (active) setError(describeRfpError(failure));
      });
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [ticketId, refresh]);

  const asks = new Map(ticket?.sales_brief?.departments.map((dept) => [dept.department_id, dept.ask]) ?? []);
  const classification = ticket?.classification;

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 md:px-10">
      <Link href={RFP_PATH} className="text-sm font-semibold text-[color:var(--brand-primary)] hover:underline">
        ← All RFP tickets
      </Link>

      {error && !ticket ? (
        <div role="alert" className="mt-6">
          <StateMessage tone="error">
            {error}
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setRefresh((value) => value + 1)}
                className="inline-flex min-h-10 items-center rounded-xl border border-red-300 bg-[color:var(--surface)] px-4 py-2 text-sm font-semibold text-red-700 transition hover:bg-red-100"
              >
                Try again
              </button>
            </div>
          </StateMessage>
        </div>
      ) : null}

      {!ticket && !error ? (
        <p className="mt-6 text-sm text-slate-600" role="status">
          Loading the ticket…
        </p>
      ) : null}

      {ticket ? (
        <>
          <header className="mt-4 border-b border-[color:var(--border-soft)] pb-5">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="break-all text-2xl font-semibold leading-tight">
                {ticket.metadata?.client_name ?? ticket.original_filename}
              </h1>
              <RfpStatusBadge status={ticket.status} />
            </div>
            <p className="mt-2 text-sm text-slate-600">
              {ticket.original_filename} · {formatBytes(ticket.pdf_bytes)} · uploaded{" "}
              <time dateTime={ticket.created_at}>{formatDateTime(ticket.created_at)}</time>
            </p>
          </header>

          <div className="mt-6 space-y-6" aria-live="polite">
            {isAnalyzing(ticket.status) ? (
              <section aria-labelledby="progress-heading" className={cardClass}>
                <h2 id="progress-heading" className="text-lg font-semibold">
                  Analysing: {stageLabel(ticket.current_stage)}
                </h2>
                <p className="mt-1 text-sm text-slate-600">This page updates on its own.</p>
                <Progress ticket={ticket} />
              </section>
            ) : null}

            {ticket.status === "discarded" ? (
              <StateMessage tone="info">
                <p className="font-semibold">Not a client RFP: no department was involved.</p>
                <p className="mt-1">{ticket.discard_reason}</p>
                <p className="mt-2 text-xs">
                  If this is wrong, TrackFlow Tech can run it again with{" "}
                  <code className="font-mono">npm run rfp -- reprocess {ticket.ticket_id}</code>.
                </p>
              </StateMessage>
            ) : null}

            {ticket.status === "failed" ? (
              <StateMessage tone="error">
                <p className="font-semibold">The analysis stopped at “{stageLabel(ticket.error_stage)}”.</p>
                <p className="mt-1">{ticket.error_message}</p>
                <p className="mt-2 text-xs">
                  Nothing was lost: the PDF is stored. TrackFlow Tech can run it again with{" "}
                  <code className="font-mono">npm run rfp -- reprocess {ticket.ticket_id}</code>.
                </p>
                <Progress ticket={ticket} />
              </StateMessage>
            ) : null}

            {classification?.review_note ? <StateMessage tone="info">{classification.review_note}</StateMessage> : null}

            {ticket.sales_brief ? <BriefCard brief={ticket.sales_brief} /> : null}

            {ticket.sections.length > 0 ? (
              <section aria-labelledby="departments-heading">
                <h2 id="departments-heading" className="mb-3 text-lg font-semibold">
                  Who to ask
                </h2>
                <div className="grid gap-5 md:grid-cols-2">
                  {ticket.sections.map((section) => (
                    <DepartmentCard key={section.department_id} section={section} ask={asks.get(section.department_id) ?? null} />
                  ))}
                </div>
              </section>
            ) : null}

            {ticket.metadata ? <ClientCard metadata={ticket.metadata} /> : null}

            <CostCard ticket={ticket} />

            {classification ? (
              <details className={cardClass}>
                <summary className="cursor-pointer text-sm font-semibold">How the document was classified</summary>
                <dl className="mt-3 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
                  <div>
                    <dt className={labelClass}>Kind</dt>
                    <dd>{DOCUMENT_KINDS[classification.document_kind ?? ""] ?? classification.document_kind}</dd>
                  </div>
                  <div>
                    <dt className={labelClass}>Confidence</dt>
                    <dd className="tabular-nums">
                      {typeof classification.confidence === "number" ? `${Math.round(classification.confidence * 100)}%` : "-"}
                    </dd>
                  </div>
                  <div>
                    <dt className={labelClass}>Language</dt>
                    <dd>{classification.language ?? "-"}</dd>
                  </div>
                </dl>
                <List items={classification.reasons ?? []} />
                {ticket.handoff ? (
                  <p className="mt-3 text-xs text-slate-600">
                    Handed off to proposal drafting ({ticket.handoff.status}, contract {ticket.handoff.contract_version}).
                  </p>
                ) : null}
              </details>
            ) : null}
          </div>
        </>
      ) : null}
    </main>
  );
}
