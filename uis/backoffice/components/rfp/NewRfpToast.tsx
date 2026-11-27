"use client";

import Link from "next/link";
import { useRfpLive } from "@/components/rfp/RfpLiveContext";
import { RFP_PATH } from "@/lib/rfp";
import { toastCopy, unseenArrivals } from "@/lib/rfp-live";

function BellIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true" className="h-5 w-5">
      <path
        d="M10 2.5a5 5 0 0 0-5 5v2.6c0 .5-.2 1-.5 1.4L3.3 13a.75.75 0 0 0 .6 1.2h12.2a.75.75 0 0 0 .6-1.2l-1.2-1.5c-.3-.4-.5-.9-.5-1.4V7.5a5 5 0 0 0-5-5Z"
        fill="currentColor"
      />
      <path d="M8 15.5a2 2 0 0 0 4 0" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

/**
 * The new-RFP notification: a card floating over every backoffice page, unlike anything else on
 * the dashboard (the status badges and the inline info/success/error boxes stay where they are).
 * It says which RFP arrived and that it needs processing, or how many arrived, including while
 * the tab was offline. It stays until someone opens the ticket or dismisses it: an RFP is money on
 * the table, so it does not fade away by itself.
 *
 * The polite live region is always mounted, so screen readers announce each new notification.
 */
export function NewRfpToast() {
  const { live, markSeen, markAllSeen } = useRfpLive();
  const arrivals = unseenArrivals(live);
  const copy = toastCopy(arrivals, RFP_PATH);
  const single = arrivals.length === 1 ? arrivals[0].ticket.ticket_id : null;

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-0 z-40 flex justify-end p-4 sm:p-6">
      <div role="status" aria-live="polite" aria-atomic="true" className="w-full max-w-sm">
        {copy ? (
          <section
            aria-label="New RFP notification"
            className="pointer-events-auto flex gap-3 overflow-hidden rounded-2xl border border-[color:var(--border-soft)] border-l-4 border-l-[color:var(--brand-accent)] bg-[color:var(--surface)] p-4 shadow-[0_12px_32px_-12px_rgba(11,34,71,0.35)] motion-safe:animate-[rfp-toast-in_220ms_ease-out]"
          >
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[color:var(--brand-accent)] text-[color:var(--on-accent)]">
              <BellIcon />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-[color:var(--foreground)]">{copy.title}</p>
              <p className="mt-1 break-words text-sm text-slate-600">{copy.detail}</p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Link
                  href={copy.href}
                  onClick={() => (single ? markSeen(single) : markAllSeen())}
                  className="inline-flex min-h-9 items-center rounded-xl bg-[color:var(--brand-primary)] px-3 py-1.5 text-sm font-semibold text-[color:var(--on-brand)] transition hover:bg-[color:var(--brand-secondary)] focus-visible:outline-2 focus-visible:outline-offset-2"
                >
                  {copy.action}
                </Link>
                <button
                  type="button"
                  onClick={markAllSeen}
                  className="inline-flex min-h-9 items-center rounded-xl px-3 py-1.5 text-sm font-semibold text-slate-600 transition hover:text-[color:var(--brand-primary)] focus-visible:outline-2 focus-visible:outline-offset-2"
                >
                  Dismiss
                </button>
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
