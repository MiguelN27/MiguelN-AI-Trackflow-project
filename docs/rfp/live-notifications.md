# Live RFP notifications (Milestone 10, Part 1)

Every RFP that comes in is money on the table. Until now nobody noticed a new ticket until they reloaded the dashboard. Now, the moment an RFP ticket is registered, the backoffice shows it on every staff screen. Nobody reloads, and if a connection drops, the page reconnects by itself without losing a ticket.

This is a communication layer only: nothing here calls a model or an agent.

**Since Milestone 10 Part 2 the stream is shared.** It is now `GET /notifications/stream`, and the route below, `/rfp/notifications/stream`, serves the same handler. It also carries the operational inactivity alerts ([inactivity-alerts.md](../operations/inactivity-alerts.md)). The RFP announcements moved from their own `rfp_ticket_notifications` table into the general `live_notifications` outbox, so one cursor resumes every kind of event, and the settings were renamed from `RFP_STREAM_*` to `LIVE_STREAM_*`. An old `rfp_ticket_notifications` table is left unused and not dropped; a tab holding an old cursor gets `rfp_stream_resync`. Nothing in the RFP behaviour below changed.

Sources:
- the ticket and checklist: `lead-instr.png`, `stages-todo.png`;
- the briefing, for the names: the root `agentic-workflow-context.md` (§2.3 Ticket: `ticket_id`, `rfp_id`, `status`, `created_at`, `updated_at`; the first status is `analyzing`).

## Where things live

| Layer | Path | Role |
|---|---|---|
| Feed (outbox) | `data/notifications/` (`LiveNotification`, `outbox.py`); `data/pipelines/rfp_intake/store.py` | The shared `live_notifications` outbox. `create_ticket` appends its `rfp_ticket_created` in the ticket's transaction. Reads: `outbox.notifications_after`, `outbox.latest_seq`. |
| Stream | `services/notifications/stream.py` | The hub one API process shares, the replay, the SSE frames, and the event registry (`EVENT_MODELS`). |
| Route | `services/notifications/router.py`; `services/rfp/router.py` | `GET /notifications/stream`, also served as `/rfp/notifications/stream`. The upload's first background task is `notifications.wake`. |
| Transport (browser) | `uis/backoffice/lib/live-stream.ts` | `fetch` + `ReadableStream`, the SSE parser, backoff, the watchdog, `Last-Event-ID`. |
| Screen state | `uis/backoffice/lib/rfp-live.ts` | Dedupe by `ticket_id`, unseen tickets, own uploads, the notification's text, the tab title. |
| UI | `components/live/LiveProvider.tsx`; `components/rfp/RfpLiveContext.ts`, `NewRfpToast.tsx`, `RfpLiveIndicator.tsx`; `RfpIntakePage.tsx`; the nav | One stream per staff tab, opened in the protected layout. |

## Flow

```
upload (POST /rfp/tickets) ─┐                       ┌─> tab 1 ─ fetch stream ─┐
CLI (npm run rfp -- run) ───┼─> store.create_ticket ─┤                         ├─> toast, nav count,
                            │   (ticket + outbox row │   hub: 1 watcher/process├─> new row on /rfp
                            │    in one transaction) └─> fan-out, ring buffer ─┤
                            └─ notifications.wake ──────> (read the feed now)  └─> tab N
```

1. **Registration.** `store.create_ticket` inserts the ticket (`analyzing`) and a `live_notifications` row (`seq`, `rfp_ticket_created`, the ticket as the list shows it) in one transaction.
   - On PostgreSQL it first takes `pg_advisory_xact_lock(hashtext('live_notifications'))`. Registrations are serialized until their commit, so **seq order is commit order**: a reader that has seen seq N has seen everything below it.
   - The lock is transaction-scoped, so it works behind Supabase's transaction pooler.
2. **Wake-up.** After the 202, the upload's first background task is `notifications.wake()`. The hub's watcher reads the feed at once (in tests, under 100 ms to every open tab).
   - A ticket registered by another process (the CLI, another worker) is picked up by the watcher's poll, every `LIVE_STREAM_POLL_SECONDS` while anyone is connected.
3. **Fan-out.** The watcher validates each row with `RfpTicketSummary`, encodes the SSE frame **once**, keeps it in a ring of 256, and puts it on every connection's queue.
4. **Browser.** The backoffice inserts the ticket from the event's own data: no reload, no list re-read.

## Event contract

`GET /notifications/stream` (alias `/rfp/notifications/stream`) is **staff only**, with the same JWT as every other backoffice call (`Authorization: Bearer`, `StaffUser`).
- A missing, invalid or expired token, or a password-reset token, gets **401**. A customer (`user`) account gets **403**. Both answers come before any event byte.
- An optional `Last-Event-ID` header resumes after a drop.
- Response headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-store`, `Connection: keep-alive`, `X-Accel-Buffering: no` (so a proxy does not buffer it).

```
event: rfp_ticket_created
id: 42
data: {"ticket_id":"…","rfp_id":"…","status":"analyzing","current_stage":"queued","original_filename":"trackflow-request-1.pdf","client_name":null,"client_country":null,"departments_needed":[],"created_at":"2026-11-26T22:13:35.910324Z","updated_at":"2026-11-26T22:13:35.910332Z"}

event: rfp_stream_ready
id: 42
data: {"cursor":42,"replayed":1,"heartbeat_seconds":15}

: keep-alive

event: rfp_stream_resync
id: 57
data: {"reason":"cursor_expired","cursor":57}
```

| Event | Meaning | `data` |
|---|---|---|
| `rfp_ticket_created` | A new RFP ticket was registered. **Which one:** `ticket_id`, `rfp_id`, `original_filename`. **That it needs processing:** `status: analyzing`, the briefing's first status. | Exactly one row of `GET /rfp/tickets` (`RfpTicketSummary`), with the briefing's field names. Server paths, the uploader and the PDF hash are never sent. |
| `rfp_stream_ready` | Caught up. The replay (if any) is done, and live events follow. | `cursor` (resume from it), `replayed`, `heartbeat_seconds`. |
| `rfp_stream_resync` | The missed events cannot be replayed. Refetch the list once. | `reason`: `cursor_unknown`, `cursor_invalid`, `cursor_expired` or `replay_limit`; and `cursor`. |
| `: keep-alive` | A comment frame every `LIVE_STREAM_HEARTBEAT_SECONDS` while idle. It is not an event. | none |

Every dispatched event is named; there is never a generic `message`.

A stream also ends normally: at its token's `exp`, or after `LIVE_STREAM_MAX_SECONDS`, whichever comes first. The client then resumes at once with `Last-Event-ID`. That way an expired session stops receiving, and every connection is re-authenticated regularly.

## Design decisions

### 1. Independent connections, one shared layer

Each tab has its own connection: its own response, its own JWT check, its own cursor and its own small bounded queue. Behind them there is **one hub per API process**, fed by **one watcher**:
- an event is validated and encoded once, then put on every queue;
- no connection queries the database on its own while live;
- no connection holds a database connection or a worker thread (the stream is async).

What 50 people at once costs:
- 50 sockets and 50 idle coroutines, a few KB each;
- one indexed query per poll per process, whatever the number of viewers, and none while nobody is connected;
- usually no query at all for a reconnect, because it replays from the ring;
- 50 tiny keep-alive comments per heartbeat.

The designs this avoids, and where they break:

| Design | Breaks at | Why |
|---|---|---|
| A sync generator (`StreamingResponse` over a sync iterator) | the 41st viewer | It keeps one threadpool thread per viewer. AnyIO's default limit is 40, so every sync endpoint freezes, uploads and login included. |
| A DB connection or `LISTEN` per stream | the 11th viewer | The SQLAlchemy pool is 5 + 5. |
| Each stream polling the database | 50 queries a second | Load grows with every viewer. |

Safety measures:
- **Slow client:** after `LIVE_STREAM_QUEUE_SIZE` queued events it is disconnected once its backlog is sent, and it resumes by replay. sse-starlette's send timeout (30 s) cuts a client that stops reading. Nobody else waits on it.
- **Reconnect bursts:** jittered exponential backoff keeps an API restart from bringing every tab back in the same instant.

Why an outbox table and not an in-memory queue: tickets are also registered by `npm run rfp -- run`, a separate process. And the Supabase transaction pooler (port 6543) does not support `LISTEN`. The table also makes replay survive an API restart.

Scaling past one process: each worker or replica runs its own hub off the same table. At thousands of viewers, swap the watcher for Redis pub/sub or `LISTEN` on a direct connection. The hub's interface does not change.

### 2. Recovery: Last-Event-ID with a short server-side replay

The client remembers the last `seq` it processed and sends it as `Last-Event-ID` when it reconnects. The server then works in this order:
1. it **subscribes first**, so nothing published from then on is lost;
2. it replays every event after that seq, from the ring, or from the table when the ring does not reach back that far;
3. it sends `rfp_stream_ready`;
4. it goes live, skipping anything at or below the cursor it just sent.

So there is no gap and no double send. The browser then says "2 RFPs arrived while you were offline".

Two cases the replay does not cover:
- **The first connection of a tab** has no cursor. The list is read again once after `rfp_stream_ready`, so anything registered before the stream opened is in that list.
- **A cursor the replay cannot honour** (unknown or from another database, more than 24 h old, more than 200 events behind, or malformed) gets `rfp_stream_resync`. The client reads the list once. A loss is never silent.

After a connection **failed** (not after the server's routine end of a stream), the list is also read once on recovery. Missed tickets come by replay, but the status of the tickets already listed may have moved on, and a list read may have failed during the outage.

**Duplicates** are prevented at three levels:

| Level | How | What it covers |
|---|---|---|
| Database | One row per ticket and event (`uq_rfp_notification_ticket_event`) | Reprocessing a ticket never announces it again. |
| Stream | The browser ignores any `rfp_ticket_created` at or below its last processed seq. A ready or resync moves the cursor to the server's. | Replay overlap. |
| Screen | Tickets are keyed by `ticket_id`. The list is the API snapshot plus pushed tickets it lacks, the snapshot's status winning (`mergeTickets`). | A pushed ticket already listed, the uploader's own tab, and a snapshot overlapping the stream never add a second row or a second notification. |

### 3. Why SSE and not WebSockets, and when that changes

**Why SSE:**
- The traffic is one-way and rare: a few small events a day, from server to browser.
- SSE is plain HTTP on the stack we already run: the same router, CORS, `StaffUser` dependency and bearer header through `fetch`.
- A browser WebSocket cannot send an `Authorization` header. The token would end up in the URL, and so in logs, or need a separate auth handshake.
- SSE already defines what recovery needs: named events, `id:` plus `Last-Event-ID`, and comment heartbeats. On a WebSocket we would rebuild all of that.
- It passes proxies, is multiplexed under HTTP/2, and its framing is asserted in a plain HTTP test.

**When it stops being the right tool:**
- Reacting to a ticket ("claim it", "assign to me", approve) is not, by itself, a reason to switch. Those are rare, discrete commands. They belong on REST POSTs, which already have validation, idempotency, a 4xx contract and an audit trail (the decision route is one). Their outcome is broadcast back as another named event, e.g. `rfp_ticket_claimed`: SSE down, REST up.
- WebSockets win when the browser-to-server direction becomes frequent, conversational and stateful:
  - live co-editing of a proposal section, with presence, cursors or typing;
  - a chat between Sales and the department owners;
  - a first-click-wins claim that must be acknowledged and ordered within the same stream as the pushes;
  - per-message acknowledgements;
  - binary payloads;
  - knowing at once that a client has gone (SSE only finds out on its next write).

## The backoffice

The live stream is opened for staff sessions only. Customer accounts never connect.

**The provider.** `LiveProvider`, in `app/(protected)/layout.tsx`, opens one stream per tab for the whole backoffice, so a new RFP is noticed on any page.

**The transport** (`lib/live-stream.ts`):
- `fetch` + `ReadableStream` through `requestAuthenticatedApi`, not `EventSource`, which cannot send the bearer header. A 401 therefore signs the user out like any other call, and a 403 stops the stream for good.
- An incremental SSE parser that handles chunks split anywhere, CRLF included.
- **Backoff:** "equal jitter": 0.5-1 s, 1-2 s, 2-4 s … capped at 30 s. A stream that stayed up 10 s resets it. A server's routine end of a stream reconnects at once.
- **Watchdog:** no byte for three heartbeats (45 s by default) means a half-open connection, so it aborts and reconnects.
- The `online` event, the tab becoming visible again, and "Retry now" all skip the rest of the wait.

**What the team sees.** It is visually distinct from the dashboard's other data (status badges, inline info/success/error boxes):
- **The notification:** a floating card, bottom-right, with a Cream Orange (`--brand-accent`) edge and bell.
  - Its text: "New RFP received", then the file, the time, and "needs processing (analysis started)". With "Open ticket" and "Dismiss".
  - Several arrivals collapse into "3 new RFPs received". After a replay it reads "2 RFPs arrived while you were offline".
  - It stays until opened or dismissed, and it is never shown to the person who uploaded the file.
- **A count on "RFP intake"** in the nav ("RFP intake 1 new" for screen readers), on every page.
- **"Live" / "Reconnecting in 8 s · Retry now" / "Offline"** in the header. Screen readers hear the change of state, not the countdown.
- **On `/rfp`:** the new row appears at the top with a solid "New" pill (`--on-accent` text on the accent: 7.0:1) and a brief highlight, until the person leaves the list or opens the ticket.
- **The tab title:** "(2) …" while the tab is in the background and new tickets wait.
- **Motion and modes:** under `prefers-reduced-motion` nothing moves (`motion-safe:` keyframes), and both dark mode and 390 px work.

**Progress polling.** The list still re-reads itself while a ticket is being analysed or drafted (`listPollDelay`), to follow its progress. That is not how new tickets arrive: the stream carries registrations, not status changes. Pushing status changes too (`rfp_ticket_status_changed` on the same stream) is the natural next step.

## Configuration

All are optional. Defaults are in `services/notifications/config.py`, documented in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `LIVE_STREAM_HEARTBEAT_SECONDS` | 15 | Keep-alive comment interval. The browser's watchdog allows three. |
| `LIVE_STREAM_POLL_SECONDS` | 2 | The watcher's poll, for tickets registered by another process, only while someone is connected. |
| `LIVE_STREAM_REPLAY_HOURS` | 24 | Replay window for `Last-Event-ID`. |
| `LIVE_STREAM_REPLAY_LIMIT` | 200 | Most events replayed. Beyond, `rfp_stream_resync`. |
| `LIVE_STREAM_QUEUE_SIZE` | 100 | Events queued for one slow client before it is disconnected. |
| `LIVE_STREAM_MAX_SECONDS` | 900 | A stream's lifetime (or its token's expiry, if sooner). |

**Dependency:** `sse-starlette` 3.7.0 was already installed through `mcp`. It was declared with `uv add`, which changed no version. It also ends open streams when uvicorn shuts down or `--reload` restarts. Without that, an open backoffice tab would hang every reload.

**Database:** the shared outbox `live_notifications` (RLS on, no policies) is created by `store.initialize_schema` and by the operations start-up. Inspect the live Supabase schema before the first start against it.

## Tests

| Suite | What it proves |
|---|---|
| `tests/test_rfp_stream_api.py` (16) | **On the wire**, through the real response bytes:<br>• `text/event-stream`, `Cache-Control`, `X-Accel-Buffering`;<br>• the named `event:`, the `id:`, and `data:` JSON validated by `RfpTicketSummary` with the briefing's fields;<br>• the `: keep-alive` comment frames;<br>• 401 (none, invalid, expired, reset token) and 403 before any event;<br>• replay after `Last-Event-ID`, in order;<br>• resync for unknown, malformed, too-old and too-many;<br>• reprocessing is not re-announced;<br>• an expired token ends the stream;<br>• the upload wakes the stream first;<br>• a 503 when the feed cannot be read. |
| `tests/test_rfp_stream_live.py` (4) | **On a real socket** (uvicorn in a thread):<br>• an upload reaches two open dashboards in under 1.5 s with the poll at 30 s;<br>• a CLI-registered ticket arrives through the poll;<br>• **drop and recovery:** the tickets registered while disconnected are replayed once, then live delivery continues, and no `ticket_id` arrives twice;<br>• after an API restart, the replay comes from the database;<br>• a closed tab is noticed. |
| `tests/test_live_notifications_hub.py` (8) | One frame object per event for every connection; no reads while nobody is connected; slow-client eviction; a wake-up from a worker thread; ring versus database; bad rows skipped; shutdown. |
| `tests/pipelines/rfp_intake/test_notifications_store.py` (4) | The ticket and its announcement commit together or not at all; the payload equals the list row; seq order; reprocessing. |
| `tests/test_rfp_postgres.py` (opt-in, +1) | On a real PostgreSQL: a registration waits behind another's open notification transaction, so seq order is commit order; RLS and JSONB on the new table. |
| `tests/frontend/backoffice/live-stream.test.ts` (27) | The parser with arbitrary chunking; event validation; the bearer and `Last-Event-ID` headers; progressive backoff and its bounds; resuming from the last seq after a drop without repeating a ticket; immediate resume after a healthy end; the watchdog; 503 retried versus 401/403 stopping; the screen's dedupe (own upload, replay, snapshot overlap); the notification's text; the title. |

Run them with:
- `uv run pytest tests/test_rfp_stream_api.py tests/test_rfp_stream_live.py tests/test_live_notifications_hub.py tests/pipelines/rfp_intake/test_notifications_store.py`
- `INVENTORY_TEST_DATABASE_URL=… uv run pytest tests/test_rfp_postgres.py`
- `cd uis/backoffice && npm test`

## Manual verification (2026-11-26)

Setup:
- Chrome through playwright-core;
- the real API on a throwaway `postgres:17-alpine`, behind a TCP proxy the script could cut;
- the backoffice dev server;
- two Sales manager accounts (A uploads, B watches) and a customer account;
- the model endpoint pointed at a closed port, so the intake failed on purpose: the notification does not depend on it.

Result: **33 of 34 checks** on the first run; **34 of 34** after the TinyDB fix below, with no 500 in the API log.

1. **First connection.** Both dashboards went Live. The stream request carried the JWT and `Accept: text/event-stream`, and no `Last-Event-ID`.
2. **A uploads.**
   - B's notification arrived in **79-101 ms**, saying which file and "needs processing".
   - B's list was not re-read to show it.
   - The row appeared once, marked New, and the nav counted 1.
   - A, the uploader, saw the row once and no notification.
3. **B on `/incidents`.** B was notified there too, and the count followed B. "Open ticket" opened it and cleared the count.
4. **B's network drops.**
   - The browser's offline flag showed "Offline", then "Live" again.
   - A real cut of B's connection showed "Reconnecting".
   - While B was disconnected, A registered 2 tickets. B resumed with `Last-Event-ID: 11` and showed "2 RFPs arrived while you were offline", each row once. The list error from the outage cleared.
5. **The API goes down.** The countdowns grew: 1, 2, 4, 5, 12 s. The CLI registered a ticket meanwhile. After the restart, B recovered it ("An RFP arrived while you were offline") once. 14 rows, no duplicate.
6. **Customer account.** It opened no stream and showed no indicator.
7. **Display checks.**
   - A background tab showed "(1) TrackFlow Backoffice".
   - With reduced motion, the notification did not animate.
   - At 390 px, no overflow and the card fits.
   - Dark mode was checked by screenshot.

**The one failure was not caused by this change, and is now fixed.** It was a CORS error in A's console: a `GET /rfp/tickets` answered 500 by a pre-existing TinyDB race (`get_current_user` → TinyDB's shared file handle), when both sessions loaded at once in dev (StrictMode doubles every mount). A 500 bypasses CORS. Each staff tab makes one more authenticated request at load (the stream), so the race had become slightly more likely to show.

The race also lost and corrupted concurrent writes: in a reproduction with 8 threads, 475 of 481 inserts were lost and the JSON file ended with "Extra data". `services/core/db.py` now opens `ThreadSafeTinyDB`, with one re-entrant lock shared by its storage and its tables, and `tests/test_core_db.py` pins it. The same reproduction now stores 481/481 with no error.

## Known limits and next steps

- **Status changes are not pushed.** Push them as `rfp_ticket_status_changed` on the same stream, then drop the list's progress polling.
- **The browser connection limit.** Under HTTP/1.1, a browser allows 6 connections per origin, so one person with 6+ backoffice tabs would starve their own requests. Fixes: a leader tab sharing its stream through `BroadcastChannel`, or HTTP/2 at a proxy.
- **Supabase:** inspect the live `rfp_*` schema before the first API start against it. The new table is created at start-up.
