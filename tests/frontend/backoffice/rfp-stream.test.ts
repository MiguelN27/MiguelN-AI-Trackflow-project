import {
  BACKOFF_MAX_MS,
  RfpStreamConnection,
  StreamProtocolError,
  backoffDelay,
  createSseParser,
  openRfpStream,
  toRfpStreamEvent,
  type ConnectionState,
  type RfpStreamEvent,
} from "@/lib/rfp-stream";
import {
  EMPTY_LIVE_STATE,
  markAllSeen,
  markSeen,
  mergeTickets,
  noteOwnUpload,
  recordArrival,
  titleWithCount,
  toastCopy,
  unseenArrivals,
  type LiveArrival,
} from "@/lib/rfp-live";
import { NetworkError } from "@/lib/api-client";
import { clearToken, readToken, storeToken } from "@/lib/auth-storage";
import type { RfpTicketSummary } from "@/lib/rfp";
import { installBrowser, removeBrowser, type FakeBrowser } from "../shared/support/browser";
import { API, jsonResponse, scriptFetch } from "../shared/support/http";

let browser: FakeBrowser;
beforeEach(() => {
  browser = installBrowser();
  storeToken("staff-session");
});
afterEach(() => {
  clearToken();
  removeBrowser();
  jest.useRealTimers();
});

function summary(n: number, overrides: Partial<RfpTicketSummary> = {}): RfpTicketSummary {
  return {
    ticket_id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    rfp_id: `10000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    status: "analyzing",
    current_stage: "queued",
    original_filename: `rfp-${n}.pdf`,
    client_name: null,
    client_country: null,
    departments_needed: [],
    created_at: `2026-11-26T10:00:0${n}Z`,
    updated_at: `2026-11-26T10:00:0${n}Z`,
    ...overrides,
  };
}

const created = (seq: number, ticket = summary(seq)) =>
  `id: ${seq}\nevent: rfp_ticket_created\ndata: ${JSON.stringify(ticket)}\n\n`;
const ready = (cursor: number, replayed = 0, heartbeat = 15) =>
  `id: ${cursor}\nevent: rfp_stream_ready\ndata: {"cursor":${cursor},"replayed":${replayed},"heartbeat_seconds":${heartbeat}}\n\n`;
const KEEP_ALIVE = ": keep-alive\n\n";

/** A server-side stream the test writes to, drops, or ends. */
function serverStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    send: (text: string) => controller.enqueue(encoder.encode(text)),
    end: () => controller.close(),
    drop: () => controller.error(new TypeError("network error")),
  };
}

function sseResponse(stream: ReadableStream<Uint8Array>): Response {
  return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream; charset=utf-8" } });
}

const flush = () => jest.advanceTimersByTimeAsync(0);

type Harness = {
  connection: RfpStreamConnection;
  events: RfpStreamEvent[];
  states: ConnectionState[];
  opens: Array<string | null>;
};

/** A connection whose `open` hands out the given server streams, one per (re)connection. */
function connect(
  streams: Array<ReturnType<typeof serverStream> | Error>,
  options: { random?: () => number } = {},
): Harness {
  const events: RfpStreamEvent[] = [];
  const states: ConnectionState[] = [];
  const opens: Array<string | null> = [];
  const connection = new RfpStreamConnection({
    open: async (lastEventId) => {
      opens.push(lastEventId);
      const next = streams.shift();
      if (next === undefined) throw new Error("no more scripted connections");
      if (next instanceof Error) throw next;
      return next.stream;
    },
    onEvent: (event) => events.push(event),
    onState: (state) => states.push(state),
    random: options.random ?? (() => 0.5),
  });
  connection.start();
  return { connection, events, states, opens };
}

const ticketSeqs = (events: RfpStreamEvent[]) =>
  events.flatMap((event) => (event.type === "ticket_created" ? [event.seq] : []));

// --- The SSE wire format ---------------------------------------------------------------

describe("createSseParser", () => {
  it("dispatches named events whatever the chunk boundaries, CRLF split included", () => {
    const parser = createSseParser();
    const wire = `id: 7\r\nevent: rfp_ticket_created\r\ndata: {"a":1}\r\n\r\n`;
    const out = [];
    for (const piece of [wire.slice(0, 5), wire.slice(5, 23), wire.slice(23, 44), wire.slice(44, 45), wire.slice(45)]) {
      out.push(...parser.push(piece));
    }
    expect(out).toEqual([{ event: "rfp_ticket_created", data: '{"a":1}', id: "7" }]);
  });

  it("skips keep-alive comments, joins multi-line data, and keeps the last id", () => {
    const parser = createSseParser();
    const out = parser.push(`${KEEP_ALIVE}id: 3\nevent: a\ndata: one\ndata: two\n\n: ping\n\ndata:three\n\n`);
    expect(out).toEqual([
      { event: "a", data: "one\ntwo", id: "3" },
      // No `event:` is a generic "message"; the id persists, as in browsers.
      { event: "message", data: "three", id: "3" },
    ]);
  });

  it("does not dispatch a frame without data, but remembers its id", () => {
    const parser = createSseParser();
    expect(parser.push("id: 9\n\n")).toEqual([]);
    expect(parser.push("event: x\ndata\n\n")).toEqual([{ event: "x", data: "", id: "9" }]);
  });
});

describe("toRfpStreamEvent", () => {
  it("types the three named events and validates their data", () => {
    expect(toRfpStreamEvent({ event: "rfp_ticket_created", id: "4", data: JSON.stringify(summary(4)) })).toEqual({
      type: "ticket_created",
      seq: 4,
      ticket: summary(4),
    });
    expect(
      toRfpStreamEvent({ event: "rfp_stream_ready", id: "4", data: '{"cursor":4,"replayed":2,"heartbeat_seconds":15}' }, true),
    ).toEqual({ type: "ready", seq: 4, cursor: 4, replayed: 2, heartbeatSeconds: 15, fresh: true });
    expect(toRfpStreamEvent({ event: "rfp_stream_resync", id: "9", data: '{"reason":"replay_limit","cursor":9}' })).toEqual({
      type: "resync",
      seq: 9,
      cursor: 9,
      reason: "replay_limit",
    });
  });

  it("ignores what the contract does not define instead of rendering it", () => {
    const bad = [
      { event: "message", id: "4", data: JSON.stringify(summary(4)) },
      { event: "rfp_ticket_created", id: "4", data: '{"ticket_id":"x"}' },
      { event: "rfp_ticket_created", id: "4", data: "not json" },
      { event: "rfp_ticket_created", id: "abc", data: JSON.stringify(summary(4)) },
      { event: "rfp_stream_ready", id: "4", data: '{"cursor":4}' },
    ];
    expect(bad.map((message) => toRfpStreamEvent(message))).toEqual([null, null, null, null, null]);
  });
});

// --- Transport: the same JWT, sent as a header ------------------------------------------

describe("openRfpStream", () => {
  it("sends the session's bearer token, asks for an event stream and resumes from Last-Event-ID", async () => {
    const server = serverStream();
    const requests = scriptFetch(sseResponse(server.stream), sseResponse(serverStream().stream));

    await openRfpStream("7", new AbortController().signal);
    await openRfpStream(null, new AbortController().signal);

    expect(requests[0].url).toBe(`${API}/rfp/notifications/stream`);
    expect(requests[0].headers).toMatchObject({
      Authorization: "Bearer staff-session",
      Accept: "text/event-stream",
      "Last-Event-ID": "7",
    });
    expect(requests[1].headers).not.toHaveProperty("Last-Event-ID");
  });

  it("refuses a reply that is not an event stream", async () => {
    scriptFetch(jsonResponse(200, { tickets: [] }));

    await expect(openRfpStream(null, new AbortController().signal)).rejects.toBeInstanceOf(StreamProtocolError);
  });
});

// --- Reconnection and recovery ---------------------------------------------------------------

describe("backoffDelay", () => {
  it("grows with every failed attempt, with jitter, and never exceeds 30 s", () => {
    const low = [0, 1, 2, 3, 4, 5, 6, 9].map((attempt) => backoffDelay(attempt, () => 0));
    const high = [0, 1, 2, 3, 4, 5, 6, 9].map((attempt) => backoffDelay(attempt, () => 1));
    expect(low).toEqual([500, 1000, 2000, 4000, 8000, 15000, 15000, 15000]);
    expect(high).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    expect(Math.max(...high)).toBe(BACKOFF_MAX_MS);
  });
});

describe("RfpStreamConnection", () => {
  beforeEach(() => jest.useFakeTimers({ now: new Date("2026-11-26T10:00:00Z") }));

  it("goes live, and passes each new ticket on once", async () => {
    const server = serverStream();
    const { connection, events, states } = connect([server]);
    await flush();

    server.send(ready(2) + KEEP_ALIVE + created(3));
    await flush();

    expect(states.map((state) => state.kind)).toEqual(["connecting", "live"]);
    expect(events.map((event) => event.type)).toEqual(["ready", "ticket_created"]);
    expect(events[0]).toMatchObject({ type: "ready", cursor: 2, fresh: true });
    expect(connection.lastEventId).toBe("3");
    connection.stop();
  });

  it("after a dropped connection: backs off, resumes from the last seq, and never repeats a ticket", async () => {
    const first = serverStream();
    const second = serverStream();
    const { connection, events, states, opens } = connect([first, second]);
    await flush();
    first.send(ready(3) + created(4));
    await flush();

    first.drop();
    await flush();

    const waiting = states.at(-1);
    expect(waiting).toMatchObject({ kind: "reconnecting", attempt: 1, reason: "network" });
    expect(waiting?.kind === "reconnecting" && waiting.retryAt - Date.now()).toBe(backoffDelay(0, () => 0.5));
    expect(opens).toEqual([null]);

    await jest.advanceTimersByTimeAsync(750);
    expect(opens).toEqual([null, "4"]);

    // The replay overlaps what this tab already had (seq 4), then brings what it missed.
    second.send(created(4) + created(5) + created(6) + ready(6, 2));
    await flush();

    expect(ticketSeqs(events)).toEqual([4, 5, 6]);
    expect(events.at(-1)).toMatchObject({ type: "ready", replayed: 2, fresh: false });
    expect(states.at(-1)?.kind).toBe("live");
    connection.stop();
  });

  it("waits longer after each consecutive failure", async () => {
    const outage = Array.from({ length: 5 }, () => new NetworkError(new TypeError("Failed to fetch")));
    const { connection, states } = connect([...outage, serverStream()], { random: () => 1 });
    const waits: number[] = [];
    for (const expected of [1000, 2000, 4000, 8000, 16000]) {
      await flush();
      const state = states.at(-1);
      if (state?.kind !== "reconnecting") throw new Error(`expected a backoff, got ${state?.kind}`);
      waits.push(state.retryAt - Date.now());
      await jest.advanceTimersByTimeAsync(expected);
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 16000]);
    connection.stop();
  });

  it("resumes at once, without backoff, when the server ends a healthy stream on purpose", async () => {
    const first = serverStream();
    const { connection, states, opens } = connect([first, serverStream()]);
    await flush();
    first.send(ready(8));
    await flush();
    await jest.advanceTimersByTimeAsync(12_000);

    first.end(); // the stream's lifetime, or the token's expiry
    await flush();

    expect(opens).toEqual([null, "8"]);
    expect(states.map((state) => state.kind)).not.toContain("reconnecting");
    connection.stop();
  });

  it("aborts a stream that has gone silent for three heartbeats, then reconnects", async () => {
    const first = serverStream();
    const { connection, states, opens } = connect([first, serverStream()]);
    await flush();
    first.send(ready(1, 0, 2)); // heartbeat every 2 s: silent for 6 s is dead
    await flush();

    await jest.advanceTimersByTimeAsync(5_900);
    expect(states.at(-1)?.kind).toBe("live");
    await jest.advanceTimersByTimeAsync(200);

    expect(states.at(-1)).toMatchObject({ kind: "reconnecting", reason: "silent" });
    await jest.advanceTimersByTimeAsync(1000);
    expect(opens).toEqual([null, "1"]);
    connection.stop();
  });

  it("keeps a stream alive as long as keep-alive comments arrive", async () => {
    const first = serverStream();
    const { connection, states } = connect([first]);
    await flush();
    first.send(ready(1, 0, 2));
    await flush();

    for (let beat = 0; beat < 5; beat += 1) {
      await jest.advanceTimersByTimeAsync(2000);
      first.send(KEEP_ALIVE);
      await flush();
    }

    expect(states.at(-1)?.kind).toBe("live");
    connection.stop();
  });

  it("a resync moves the cursor to the server's, even backwards", async () => {
    const first = serverStream();
    const { connection, events } = connect([first]);
    await flush();
    first.send(ready(40) + `id: 2\nevent: rfp_stream_resync\ndata: {"reason":"cursor_unknown","cursor":2}\n\n` + created(3));
    await flush();

    expect(connection.lastEventId).toBe("3");
    expect(ticketSeqs(events)).toEqual([3]);
    connection.stop();
  });

  it("retries an outage (503) but stops for good on an expired session (401) or a customer (403)", async () => {
    const server = serverStream();
    scriptFetch(jsonResponse(503, { detail: "RFP storage unavailable" }), sseResponse(server.stream));
    const states: ConnectionState[] = [];
    const retrying = new RfpStreamConnection({ onEvent: () => undefined, onState: (s) => states.push(s), random: () => 0 });
    retrying.start();
    await flush();
    expect(states.at(-1)).toMatchObject({ kind: "reconnecting", reason: "http_503" });
    await jest.advanceTimersByTimeAsync(500);
    expect(states.at(-1)?.kind).toBe("connecting");
    retrying.stop();

    for (const [status, reason] of [
      [401, "unauthorized"],
      [403, "forbidden"],
    ] as const) {
      storeToken("staff-session");
      const requests = scriptFetch(jsonResponse(status, { detail: "no" }));
      const seen: ConnectionState[] = [];
      const connection = new RfpStreamConnection({ onEvent: () => undefined, onState: (s) => seen.push(s) });
      connection.start();
      await flush();
      await jest.advanceTimersByTimeAsync(60_000);

      expect(seen.at(-1)).toEqual({ kind: "unavailable", reason });
      expect(requests).toHaveLength(1);
    }
    // The 401 went through the API client's session handling: token cleared, sign-in requested.
    expect(browser.events).toContain("trackflow:unauthorized");
    expect(readToken()).not.toBeNull(); // re-stored for the 403 case; the 401 had cleared it
  });

  it("stops cleanly: no reconnection after stop()", async () => {
    const first = serverStream();
    const { connection, opens, states } = connect([first, serverStream()]);
    await flush();
    connection.stop();
    first.drop();
    await jest.advanceTimersByTimeAsync(60_000);

    expect(opens).toEqual([null]);
    expect(states.at(-1)?.kind).toBe("stopped");
  });
});

// --- What the screen does with it -------------------------------------------------------------

const arrival = (n: number, whileOffline = false): LiveArrival => ({ ticket: summary(n), seq: n, whileOffline });

describe("live state", () => {
  it("never shows the same ticket twice", () => {
    let state = recordArrival(EMPTY_LIVE_STATE, arrival(1));
    state = recordArrival(state, arrival(1));
    state = recordArrival(state, arrival(2));

    expect(state.received.map((item) => item.seq)).toEqual([2, 1]);
    expect(state.unseen).toEqual([summary(2).ticket_id, summary(1).ticket_id]);
  });

  it("does not announce a tab's own upload, whichever arrives first", () => {
    const pushedFirst = noteOwnUpload(recordArrival(EMPTY_LIVE_STATE, arrival(1)), summary(1).ticket_id);
    const uploadedFirst = recordArrival(noteOwnUpload(EMPTY_LIVE_STATE, summary(1).ticket_id), arrival(1));

    for (const state of [pushedFirst, uploadedFirst]) {
      expect(state.received).toHaveLength(1); // still in the list
      expect(state.unseen).toEqual([]); // but not announced
    }
  });

  it("forgets what was seen", () => {
    const state = recordArrival(recordArrival(EMPTY_LIVE_STATE, arrival(1)), arrival(2));

    expect(markSeen(state, summary(1).ticket_id).unseen).toEqual([summary(2).ticket_id]);
    expect(markAllSeen(state).unseen).toEqual([]);
    expect(unseenArrivals(markSeen(state, summary(2).ticket_id)).map((item) => item.seq)).toEqual([1]);
  });

  it("merges pushed tickets into the list snapshot by ticket_id, the snapshot's status winning", () => {
    const snapshot = [summary(2, { status: "intake_complete" }), summary(1, { status: "done" })];
    const received = [summary(3), summary(2)];

    const merged = mergeTickets(snapshot, received);

    expect(merged.map((ticket) => [ticket.original_filename, ticket.status])).toEqual([
      ["rfp-3.pdf", "analyzing"],
      ["rfp-2.pdf", "intake_complete"],
      ["rfp-1.pdf", "done"],
    ]);
  });

  it("says which RFP arrived and that it needs processing, or how many, or that it came while offline", () => {
    expect(toastCopy([], "/rfp")).toBeNull();
    expect(toastCopy([arrival(1)], "/rfp")).toMatchObject({
      title: "New RFP received",
      href: `/rfp/${summary(1).ticket_id}`,
      action: "Open ticket",
    });
    expect(toastCopy([arrival(1)], "/rfp")?.detail).toMatch(/^rfp-1\.pdf · registered .+ · needs processing/);
    expect(toastCopy([arrival(3), arrival(2), arrival(1)], "/rfp")).toMatchObject({
      title: "3 new RFPs received",
      detail: "rfp-3.pdf, rfp-2.pdf and 1 more · all need processing",
      href: "/rfp",
    });
    expect(toastCopy([arrival(2, true), arrival(1, true)], "/rfp")?.title).toBe("2 RFPs arrived while you were offline");
  });

  it("counts unseen tickets in the tab title, without stacking prefixes", () => {
    expect(titleWithCount("TrackFlow Backoffice", 2)).toBe("(2) TrackFlow Backoffice");
    expect(titleWithCount("(2) TrackFlow Backoffice", 3)).toBe("(3) TrackFlow Backoffice");
    expect(titleWithCount("(3) TrackFlow Backoffice", 0)).toBe("TrackFlow Backoffice");
  });
});
