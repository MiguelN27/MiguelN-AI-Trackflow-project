/**
 * What the backoffice does with live RFP notifications (Milestone 10): pure state, so it is
 * unit-tested without a browser. The stream itself is `lib/live-stream.ts`; the React side is
 * `components/live/LiveProvider.tsx`.
 *
 * Dedupe at the screen: tickets are keyed by `ticket_id`. A ticket the list already shows, a
 * replayed ticket, or the tab's own upload never adds a second row or a second notification.
 */

import type { RfpTicketSummary } from "@/lib/rfp";

/** How many pushed tickets a tab keeps (the list shows the 50 newest anyway). */
export const MAX_RECEIVED = 50;

export type LiveArrival = {
  ticket: RfpTicketSummary;
  seq: number;
  /** Replayed after a reconnect: it was registered while this tab was disconnected. */
  whileOffline: boolean;
};

export type LiveState = {
  /** Every ticket pushed to this tab, newest first, once each. */
  received: LiveArrival[];
  /** Pushed tickets nobody has looked at yet (ticket ids, newest first). */
  unseen: string[];
  /** Tickets this tab uploaded itself: shown in the list, never announced to the uploader. */
  own: string[];
};

export const EMPTY_LIVE_STATE: LiveState = { received: [], unseen: [], own: [] };

export function recordArrival(state: LiveState, arrival: LiveArrival): LiveState {
  const id = arrival.ticket.ticket_id;
  if (state.received.some((item) => item.ticket.ticket_id === id)) return state;
  return {
    ...state,
    received: [arrival, ...state.received].slice(0, MAX_RECEIVED),
    unseen: state.own.includes(id) ? state.unseen : [id, ...state.unseen],
  };
}

export function noteOwnUpload(state: LiveState, ticketId: string): LiveState {
  if (state.own.includes(ticketId)) return state;
  return {
    ...state,
    own: [ticketId, ...state.own].slice(0, MAX_RECEIVED),
    unseen: state.unseen.filter((id) => id !== ticketId),
  };
}

export function markSeen(state: LiveState, ticketId: string): LiveState {
  return state.unseen.includes(ticketId) ? { ...state, unseen: state.unseen.filter((id) => id !== ticketId) } : state;
}

export function markAllSeen(state: LiveState): LiveState {
  return state.unseen.length === 0 ? state : { ...state, unseen: [] };
}

export function unseenArrivals(state: LiveState): LiveArrival[] {
  return state.received.filter((item) => state.unseen.includes(item.ticket.ticket_id));
}

/**
 * The list to render: the latest snapshot from `GET /rfp/tickets`, plus every pushed ticket it
 * does not have yet. The snapshot wins for a ticket in both (its status is fresher). Newest first.
 */
export function mergeTickets(snapshot: RfpTicketSummary[], received: RfpTicketSummary[]): RfpTicketSummary[] {
  const known = new Set(snapshot.map((ticket) => ticket.ticket_id));
  const merged = [...snapshot];
  for (const ticket of received) {
    if (known.has(ticket.ticket_id)) continue;
    known.add(ticket.ticket_id);
    merged.push(ticket);
  }
  return merged
    .map((ticket, index) => ({ ticket, index, at: Date.parse(ticket.created_at) }))
    .sort((a, b) => b.at - a.at || a.index - b.index)
    .map((entry) => entry.ticket);
}

export type ToastCopy = { title: string; detail: string; href: string; action: string };

function clock(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

/** What the notification says: which RFP arrived and that it is being analysed, or how many. */
export function toastCopy(arrivals: LiveArrival[], listPath: string): ToastCopy | null {
  if (arrivals.length === 0) return null;
  const offline = arrivals.some((item) => item.whileOffline);
  if (arrivals.length === 1) {
    const [{ ticket }] = arrivals;
    return {
      title: offline ? "An RFP arrived while you were offline" : "New RFP received",
      detail: `${ticket.original_filename} · registered ${clock(ticket.created_at)} · needs processing (analysis started)`,
      href: `${listPath}/${ticket.ticket_id}`,
      action: "Open ticket",
    };
  }
  const names = arrivals.slice(0, 2).map((item) => item.ticket.original_filename);
  const more = arrivals.length - names.length;
  return {
    title: offline ? `${arrivals.length} RFPs arrived while you were offline` : `${arrivals.length} new RFPs received`,
    detail: `${names.join(", ")}${more > 0 ? ` and ${more} more` : ""} · all need processing`,
    href: listPath,
    action: "View RFP list",
  };
}

const COUNT_PREFIX = /^\(\d+\) /;

/** "(2) RFP intake" while new tickets wait unseen in a background tab; the plain title otherwise. */
export function titleWithCount(title: string, count: number): string {
  const plain = title.replace(COUNT_PREFIX, "");
  return count > 0 ? `(${count}) ${plain}` : plain;
}
