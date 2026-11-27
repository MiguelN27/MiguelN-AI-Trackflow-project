/**
 * The backoffice's live notification stream, as the browser consumes it (Milestone 10).
 *
 * `GET /notifications/stream` is Server-Sent Events, read with `fetch` and a
 * `ReadableStream` rather than `EventSource`: `EventSource` cannot send the
 * `Authorization: Bearer` header the API requires, and it hides the status code
 * we need to tell an expired session (401) from a customer account (403) from
 * an outage (5xx). The request goes through `requestAuthenticatedApi`, so the
 * token, the 401 handling (sign in again) and telemetry are the same as every
 * other protected call.
 *
 * Named events (see `services/notifications/stream.py`), every one with `id:` = the shared
 * outbox's seq, so one cursor resumes them all:
 * - `rfp_ticket_created`: a new RFP ticket, `data:` = the ticket as the list shows it;
 * - `operational_inactivity_detected` / `operational_activity_resumed` /
 *   `operational_alert_acknowledged`: an inactivity alert opened, resolved or was taken in
 *   charge, `data:` = the whole alert (`lib/operations-live.ts`);
 * - `rfp_stream_ready`: caught up, with the cursor to resume from;
 * - `rfp_stream_resync`: missed events cannot be replayed, reload the snapshots once.
 *
 * Recovery: the connection remembers the last seq it processed and sends it as
 * `Last-Event-ID` when it reconnects, so the server replays what was missed.
 * Reconnection uses progressive backoff with jitter; a watchdog aborts a stream
 * that has gone silent for three heartbeats (a half-open connection never errors
 * by itself). Everything here is plain TypeScript, so it is unit-tested without
 * a browser.
 */

import { ApiError, requestAuthenticatedApi, UnauthorizedError } from "@/lib/api-client";
import { ALERT_EVENTS, isOperationalAlert, type AlertEventName, type OperationalAlert } from "@/lib/operations-live";
import { isRfpTicketSummary, type RfpTicketSummary } from "@/lib/rfp";

export const LIVE_STREAM_API = "/notifications/stream";
export const TICKET_CREATED_EVENT = "rfp_ticket_created";
export const STREAM_READY_EVENT = "rfp_stream_ready";
export const STREAM_RESYNC_EVENT = "rfp_stream_resync";

/** First retry after 0.5-1 s, then 1-2 s, 2-4 s, ... never more than 30 s. */
export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_MAX_MS = 30_000;
/** A stream that stayed up this long was healthy: the next drop starts the backoff over. */
export const HEALTHY_AFTER_MS = 10_000;
/** Until the server states its heartbeat: 3 x the API's default 15 s. */
export const DEFAULT_WATCHDOG_MS = 45_000;
const MIN_WATCHDOG_MS = 1000;

// --- SSE wire format ---------------------------------------------------------------

/** One dispatched event, as the WHATWG event-stream rules define it. */
export type SseMessage = { event: string; data: string; id: string };

/**
 * An incremental event-stream parser: feed it decoded text in chunks of any size
 * (a line, or a CRLF, may be split between chunks) and it returns the events
 * each chunk completes. Comments (`: keep-alive`) are skipped. As in browsers,
 * the last `id:` seen persists across events.
 */
export function createSseParser(): { push(chunk: string): SseMessage[] } {
  let buffer = "";
  let skipLeadingLineFeed = false;
  let eventName = "";
  let dataLines: string[] = [];
  let idBuffer = "";

  function processLine(line: string, out: SseMessage[]) {
    if (line === "") {
      if (dataLines.length > 0) {
        out.push({ event: eventName || "message", data: dataLines.join("\n"), id: idBuffer });
      }
      eventName = "";
      dataLines = [];
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id" && !value.includes("\u0000")) idBuffer = value;
    // `retry` is ignored: reconnection timing is this module's backoff.
  }

  return {
    push(chunk: string): SseMessage[] {
      const out: SseMessage[] = [];
      let text = chunk;
      if (skipLeadingLineFeed && text.startsWith("\n")) text = text.slice(1);
      skipLeadingLineFeed = false;
      buffer += text;
      let start = 0;
      for (let index = 0; index < buffer.length; index += 1) {
        const character = buffer[index];
        if (character !== "\n" && character !== "\r") continue;
        processLine(buffer.slice(start, index), out);
        if (character === "\r") {
          if (index + 1 < buffer.length) {
            if (buffer[index + 1] === "\n") index += 1;
          } else {
            // A CR at the end of the chunk: its LF may open the next one.
            skipLeadingLineFeed = true;
          }
        }
        start = index + 1;
      }
      buffer = buffer.slice(start);
      return out;
    },
  };
}

// --- The stream's events -------------------------------------------------------------

export type TicketCreatedEvent = { type: "ticket_created"; seq: number; ticket: RfpTicketSummary };
export type StreamReadyEvent = {
  type: "ready";
  seq: number;
  cursor: number;
  replayed: number;
  heartbeatSeconds: number;
  /** A first connection (no `Last-Event-ID`): a list read before it may be missing tickets. */
  fresh: boolean;
};
export type StreamResyncEvent = { type: "resync"; seq: number; cursor: number; reason: string };
export type AlertEvent = { type: "alert"; event: AlertEventName; seq: number; alert: OperationalAlert };
export type LiveEvent = TicketCreatedEvent | AlertEvent | StreamReadyEvent | StreamResyncEvent;

function isAlertEvent(name: string): name is AlertEventName {
  return (ALERT_EVENTS as readonly string[]).includes(name);
}

function parseSeq(id: string): number | null {
  return /^\d+$/.test(id) ? Number(id) : null;
}

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/** The typed event, or null for anything the contract does not define (ignored, never rendered). */
export function toLiveEvent(message: SseMessage, fresh = false): LiveEvent | null {
  const seq = parseSeq(message.id);
  if (seq === null) return null;
  const data = parseJson(message.data);
  if (message.event === TICKET_CREATED_EVENT) {
    return isRfpTicketSummary(data) ? { type: "ticket_created", seq, ticket: data } : null;
  }
  if (isAlertEvent(message.event)) {
    return isOperationalAlert(data) ? { type: "alert", event: message.event, seq, alert: data } : null;
  }
  if (!isRecord(data) || !isCount(data.cursor)) return null;
  if (message.event === STREAM_READY_EVENT) {
    const heartbeat = data.heartbeat_seconds;
    if (!isCount(data.replayed) || typeof heartbeat !== "number" || !(heartbeat > 0)) return null;
    return { type: "ready", seq, cursor: data.cursor, replayed: data.replayed, heartbeatSeconds: heartbeat, fresh };
  }
  if (message.event === STREAM_RESYNC_EVENT) {
    return typeof data.reason === "string" ? { type: "resync", seq, cursor: data.cursor, reason: data.reason } : null;
  }
  return null;
}

// --- Transport -------------------------------------------------------------------------

/** The API answered, but not with an event stream. Retried like an outage. */
export class StreamProtocolError extends Error {}

/** Open the stream: `Authorization: Bearer`, `Accept: text/event-stream`, and the resume point. */
export async function openRfpStream(lastEventId: string | null, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
  const response = await requestAuthenticatedApi(LIVE_STREAM_API, {
    headers: {
      Accept: "text/event-stream",
      ...(lastEventId === null ? {} : { "Last-Event-ID": lastEventId }),
    },
    cache: "no-store",
    signal,
  });
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.startsWith("text/event-stream") || !response.body) {
    void response.body?.cancel();
    throw new StreamProtocolError(`expected text/event-stream, got ${contentType || "no content type"}`);
  }
  return response.body;
}

/** Progressive backoff with "equal jitter": half the window fixed, half random. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const window = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt));
  return Math.round(window / 2 + random() * (window / 2));
}

// --- The connection ------------------------------------------------------------------

export type ConnectionState =
  | { kind: "idle" }
  | { kind: "connecting"; attempt: number }
  | { kind: "live"; since: number }
  | { kind: "reconnecting"; attempt: number; retryAt: number; reason: string }
  | { kind: "unavailable"; reason: "unauthorized" | "forbidden" }
  | { kind: "stopped" };

export type LiveStreamConnectionOptions = {
  onEvent: (event: LiveEvent) => void;
  onState: (state: ConnectionState) => void;
  open?: (lastEventId: string | null, signal: AbortSignal) => Promise<ReadableStream<Uint8Array>>;
  random?: () => number;
  now?: () => number;
  healthyAfterMs?: number;
};

function failureReason(failure: unknown): string {
  if (failure === null) return "ended";
  if (failure instanceof ApiError) return `http_${failure.status}`;
  if (failure instanceof StreamProtocolError) return "protocol";
  if (failure instanceof DOMException && failure.name === "AbortError") return "silent";
  return "network";
}

/**
 * One tab's connection: open, read, reconnect with backoff, resume by
 * `Last-Event-ID`, until `stop()`. A 401 (the session expired: the API client has
 * already sent the user to sign in) or a 403 (not a staff account) stops it for good.
 */
export class LiveStreamConnection {
  state: ConnectionState = { kind: "idle" };
  private readonly options: LiveStreamConnectionOptions;
  private readonly open: NonNullable<LiveStreamConnectionOptions["open"]>;
  private readonly now: () => number;
  private stopped = true;
  private attempt = 0;
  private lastSeq: number | null = null;
  private controller: AbortController | null = null;
  private wakeUp: (() => void) | null = null;
  private watchdog: ReturnType<typeof setTimeout> | null = null;
  private watchdogMs = DEFAULT_WATCHDOG_MS;

  constructor(options: LiveStreamConnectionOptions) {
    this.options = options;
    this.open = options.open ?? openRfpStream;
    this.now = options.now ?? Date.now;
  }

  /** The seq to resume from (`Last-Event-ID`), or null before the first event. */
  get lastEventId(): string | null {
    return this.lastSeq === null ? null : String(this.lastSeq);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    void this.run();
  }

  stop(): void {
    this.stopped = true;
    this.controller?.abort();
    this.wakeUp?.();
    this.clearWatchdog();
    this.setState({ kind: "stopped" });
  }

  /** Skip the rest of the backoff wait (back online, tab visible again, "Retry now"). */
  reconnectNow(): void {
    if (this.state.kind === "reconnecting") this.wakeUp?.();
  }

  private setState(state: ConnectionState) {
    this.state = state;
    this.options.onState(state);
  }

  private async run(): Promise<void> {
    while (!this.stopped) {
      const fresh = this.lastSeq === null;
      const session: { liveSince: number | null } = { liveSince: null };
      let failure: unknown = null;
      this.setState({ kind: "connecting", attempt: this.attempt });
      const controller = new AbortController();
      this.controller = controller;
      try {
        const body = await this.open(this.lastEventId, controller.signal);
        await this.read(body, controller, fresh, session);
      } catch (error: unknown) {
        failure = error;
      } finally {
        this.clearWatchdog();
        this.controller = null;
      }
      if (this.stopped) return;

      if (failure instanceof UnauthorizedError) {
        this.halt({ kind: "unavailable", reason: "unauthorized" });
        return;
      }
      if (failure instanceof ApiError && failure.status === 403) {
        this.halt({ kind: "unavailable", reason: "forbidden" });
        return;
      }

      const livedMs = session.liveSince === null ? 0 : this.now() - session.liveSince;
      const healthy = livedMs >= (this.options.healthyAfterMs ?? HEALTHY_AFTER_MS);
      if (healthy) this.attempt = 0;
      // The server ended a healthy stream on purpose (its lifetime, or the token's): resume at once.
      if (failure === null && healthy) continue;

      const delay = backoffDelay(this.attempt, this.options.random);
      this.attempt += 1;
      this.setState({ kind: "reconnecting", attempt: this.attempt, retryAt: this.now() + delay, reason: failureReason(failure) });
      await this.sleep(delay);
    }
  }

  private halt(state: ConnectionState) {
    this.stopped = true;
    this.setState(state);
  }

  private async read(
    body: ReadableStream<Uint8Array>,
    controller: AbortController,
    fresh: boolean,
    session: { liveSince: number | null },
  ): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const parser = createSseParser();
    const aborted = new Promise<never>((_, reject) => {
      const fail = () => reject(new DOMException("The stream went silent or was stopped.", "AbortError"));
      if (controller.signal.aborted) fail();
      else controller.signal.addEventListener("abort", fail, { once: true });
    });
    aborted.catch(() => undefined);
    this.armWatchdog(controller);
    try {
      for (;;) {
        const { value, done } = await Promise.race([reader.read(), aborted]);
        if (done) return;
        for (const message of parser.push(decoder.decode(value, { stream: true }))) {
          this.dispatch(message, fresh, session);
        }
        // Any bytes, a keep-alive comment included, prove the connection is alive. Armed after
        // dispatching, so the heartbeat that `rfp_stream_ready` announces applies at once.
        this.armWatchdog(controller);
      }
    } finally {
      reader.cancel().catch(() => undefined);
    }
  }

  private dispatch(message: SseMessage, fresh: boolean, session: { liveSince: number | null }) {
    const event = toLiveEvent(message, fresh);
    if (event === null) {
      // An event this tab cannot use (newer than this code, or refused): still past it, so a
      // reconnect never asks for it again. Control events always parse, so this is data.
      const seq = parseSeq(message.id);
      if (seq !== null && (this.lastSeq === null || seq > this.lastSeq)) this.lastSeq = seq;
      return;
    }
    if (event.type === "ticket_created" || event.type === "alert") {
      // Already processed (a replay overlapping what this tab had seen): never twice.
      if (this.lastSeq !== null && event.seq <= this.lastSeq) return;
      this.lastSeq = event.seq;
    } else {
      // The server's cursor is authoritative (after a resync it may even be lower).
      this.lastSeq = event.cursor;
      if (event.type === "ready") {
        this.watchdogMs = Math.max(MIN_WATCHDOG_MS, 3 * event.heartbeatSeconds * 1000);
        session.liveSince = this.now();
        this.setState({ kind: "live", since: session.liveSince });
      }
    }
    this.options.onEvent(event);
  }

  private armWatchdog(controller: AbortController) {
    this.clearWatchdog();
    this.watchdog = setTimeout(() => controller.abort(), this.watchdogMs);
  }

  private clearWatchdog() {
    if (this.watchdog !== null) clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        this.wakeUp = null;
        resolve();
      };
      const timer = setTimeout(done, ms);
      this.wakeUp = done;
    });
  }
}
