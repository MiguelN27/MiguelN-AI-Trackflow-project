# Operational inactivity alerts (Milestone 10, Part 2)

The RFP notifications announce something that **happened**. These alerts announce something that **did not happen** within its expected period:
- a warehouse that registers no orders during its working hours;
- a scheduled job that does not complete;
- a department owner who leaves an RFP section undecided past its deadline;
- a branch whose incidents nobody picks up.

They hold the same technical bar as the RFP stream:
- a named event;
- a structured payload with the existing vocabularies;
- delivery over the **same** live stream, with the same `Last-Event-ID` replay, backoff, watchdog and dedupe.

There are no model calls.

There is a real case behind this: the nightly telemetry export (DEV-53, cron 03:30) has failed on every run since 2026-11-13 with `OperationalError`, and nobody noticed.

## Decided with the developer (2026-11-26)

- **Four kinds of expected activity:**
  - **warehouse orders** (a location);
  - **scheduled jobs** (a process);
  - **RFP approvals past their SLA** (TrackFlow's version of "a vacancy left unfilled past its deadline");
  - **incidents left untouched** (one alert per branch).
- **Rules** live in a versioned file, `config/operational-activity-rules.json`, validated at API start-up. An invalid rule stops the start, naming the file and the problem.
- **Lifecycle:**
  - an alert opens once, and **resolves by itself** when the activity returns;
  - staff can **acknowledge** it with a REST POST, broadcast to every tab. This is the "SSE down, REST up" split from the RFP design.
- **One shared stream:** the RFP announcements moved into a general outbox, so a single cursor resumes both kinds of event, and each tab keeps one connection.

Why these sources and not telemetry: the order events in `telemetry_events` are emitted only by the browser form. They carry no `client_id`, they stop when the telemetry endpoint is unset, and orders made by scripts or the API never appear. The inventory order tables are authoritative. The hiring tracker's vacancies live in an external API with no deadline field.

## Where things live

| Layer | Path | Role |
|---|---|---|
| Rules | `config/operational-activity-rules.json`; `services/operations/rules.py` | The file, and its validation (a discriminated union on `source`). |
| Clock | `services/operations/schedule.py` | Active minutes inside a weekly window, in the location's timezone, compared in UTC (daylight-saving safe). |
| Sources | `services/operations/sources.py` | One adapter per source. Each returns `Watch(subject_key, subject, label, waiting_since, last_activity_at, details)`. Read-only, short sessions. |
| Decision | `services/operations/monitor.py` | `observe` (per rule; a failing source skips only its rule), then `plan` (pure), then `evaluate` (one transaction under an advisory lock) and `preview` (dry run). |
| Table | `services/operations/models.py` | `operational_alerts`, with a partial unique index: one **open** alert per rule and subject. |
| In the API | `services/operations/lifecycle.py` | Start-up validation and schema; an asyncio task that evaluates every `OPS_MONITOR_INTERVAL_SECONDS` and wakes the stream on changes. |
| HTTP | `services/operations/router.py`, `service.py`, `schemas.py` | `GET /operations/alerts` and `POST /operations/alerts/{id}/acknowledge` (staff only). |
| Outbox | `data/notifications/` | `live_notifications`, the shared feed of the stream (see below). |
| Stream | `services/notifications/` | `GET /notifications/stream`; the alert events are in its registry (`EVENT_MODELS`). |
| CLI | `scripts/operations_monitor.py` (`npm run ops`) | `rules`, `check [--dry-run]`, `alerts [--all]`. |
| Backoffice | `lib/operations-live.ts`, `lib/live-stream.ts`, `components/live/`, `components/operations/`, `app/(protected)/operations/` | The banner, the "Alerts" nav count, and the `/operations` page. |

## The rules

The defaults (each can be `"enabled": false`):

| `rule_id` | Source | Expected within | Counted during | Severity |
|---|---|---|---|---|
| `monterrey-outbound` | `warehouse_orders`, Monterrey, `outbound_order_created` | 120 min | Mon–Sat 08:00–20:00 `America/Monterrey` | warning |
| `zaragoza-outbound` | `warehouse_orders`, Zaragoza, `outbound_order_created` | 120 min | Mon–Fri 08:00–20:00 `Europe/Madrid` | warning |
| `monterrey-inbound` / `zaragoza-inbound` | `warehouse_orders`, `inbound_order_created` | 720 min (one working day) | as above | warning |
| `nightly-export` | `scheduled_job`, `nightly_export` (`job_runs`) | 26 h | always | critical |
| `weekly-report` | `scheduled_job`, `weekly_warehouse_client_performance` (`reporting.pipeline_runs`) | 7 d + 3 h | always | critical |
| `rfp-approval-sla` | `rfp_approval`: a pending section of a `waiting_for_approval` ticket, from `ready_at` | 24 h | always | warning |
| `incidents-sla-critical` | `incidents`: `lost_parcel`, `carrier_issue`, per branch | 4 h | always | critical |
| `incidents-open` | `incidents`: every other category, per branch | 24 h | always | warning |

Each source fills the alert from its existing vocabulary:

| Source | What is watched | `subject` | Activity |
|---|---|---|---|
| `warehouse_orders` | The newest `created_at` in `outbound_orders` / `inbound_orders`, joined to `products` by warehouse and optional `client_id`. | `{warehouse, client_id}`, with `warehouse` `Monterrey`/`Zaragoza`. This is the inventory enum and the approved mapping of BRIEFING's `los_angeles` (`PIPELINE_DESIGN.md`). | The activity uses the BRIEFING's event names. |
| `scheduled_job` | The last `completed` run's `finished_at`. | `{job_name}` | `details` carry the latest run's status ("last run failed"). |
| `rfp_approval` | One watch per pending section; a revision resets `ready_at`, and so the clock. | `{ticket_id, rfp_id, department_id, owner, client_name}`, the briefing's names and owners. | |
| `incidents` | One watch per branch, from its oldest open incident. | `{branch}`, from `trackflow_shared.incidents`. | `details` carry `open_incidents`, `overdue_incidents` and `oldest_reference`. |

Grouping incidents by branch is deliberate. The 2024 helpdesk seed leaves 29 incidents open at `central`, so one alert per incident would bury the screen.

## How detection works

An absence has no event to react to, so the API keeps a clock. Every `OPS_MONITOR_INTERVAL_SECONDS` (60 by default), one task per API process runs `evaluate`:

1. **Observe** every enabled rule's subjects, with no lock and no transaction held. A source that fails (any exception) is skipped for this round: its alerts stay as they are, and the other rules still run.
2. **Plan** (pure). A subject is overdue when the active minutes since `waiting_since` exceed `expected_within_minutes`; a subject whose activity never happened is overdue at once.
   - overdue and not open → open;
   - open and no longer overdue → resolve, `activity_resumed`;
   - open and gone → `slot_closed` for an RFP section (decided, or withdrawn with its proposal), otherwise `activity_resumed` (a branch whose incidents were all picked up);
   - open, but the rule was disabled or removed → `rule_disabled` / `rule_removed`.
3. **Apply** in one transaction under `pg_advisory_xact_lock(hashtext('operational_alerts'))`. The open alerts are read again inside the lock, and each change writes its alert row and its live event (`outbox.append`) together.
   - Two API workers, or the API and the CLI, never open the same alert twice. The partial unique index `uq_operational_alert_open` is the last line of defence.
   - When something changed, the task wakes the stream, and every tab gets it at once.

**Operating hours.** `schedule.active_minutes_between` counts only the minutes inside the rule's weekly window, in its timezone. Two hours without orders means two working hours, never a night or a weekend. The same instants count differently in Monterrey and Madrid. A daylight-saving day is still 12 working hours, because every comparison is made in UTC.

**Latency.** An alert opens at most one interval after its period has passed, and reaches open tabs within milliseconds of opening.

## Event contract

All three events go on `GET /notifications/stream` (staff only, the same JWT; `/rfp/notifications/stream` is an alias). `id:` is the shared outbox seq, the same counter as `rfp_ticket_created`. `data:` is always the **whole alert**, the same shape `GET /operations/alerts` returns, so a tab upserts it by `alert_id` whichever way it arrived.

```
id: 2
event: operational_inactivity_detected
data: {"alert_id":"…","rule_id":"zaragoza-outbound","source":"warehouse_orders","activity":"outbound_order_created","subject":{"warehouse":"Zaragoza","client_id":null},"label":"No outbound order registered at Zaragoza","severity":"warning","status":"open","expected_within_minutes":120,"waiting_since":"2026-11-30T08:00:00Z","last_activity_at":"2026-11-30T08:00:00Z","detected_at":"2026-11-30T10:01:00Z","overdue_minutes":1,"details":{"orders_table":"outbound_orders"},"acknowledged_at":null,"acknowledged_by":null,"resolved_at":null,"resolved_reason":null}

id: 3
event: operational_alert_acknowledged
data: {…the same alert…, "acknowledged_at":"2026-11-30T10:05:00Z","acknowledged_by":"ana.whitfield@trackflow.com"}

id: 4
event: operational_activity_resumed
data: {…the same alert…, "status":"resolved","resolved_at":"2026-11-30T10:20:00Z","resolved_reason":"activity_resumed"}
```

| Field | Meaning |
|---|---|
| `alert_id` | The alert. Each event is announced once per alert (the outbox is unique on `(event, subject)`). |
| `rule_id`, `source`, `activity` | Which rule, which kind of activity, and what was expected. |
| `subject` | Who or where, in the existing vocabularies (above). |
| `label` | The sentence the screen shows. |
| `severity` | `warning` \| `critical`. |
| `status` | `open` \| `resolved`. |
| `expected_within_minutes` | The rule's period. |
| `waiting_since` | When the period started counting. |
| `last_activity_at` | Null when the activity never happened. |
| `detected_at`, `overdue_minutes` | Active minutes past the period when detected; null when it never happened. |
| `details` | The evidence: counts, the last run's status, the oldest incident. |
| `acknowledged_at`, `acknowledged_by` | Who took it in charge. |
| `resolved_at`, `resolved_reason` | `activity_resumed` \| `slot_closed` \| `rule_disabled` \| `rule_removed`. |

**Recovery is the RFP stream's own.** A tab resumes with `Last-Event-ID` and gets the alert events it missed in seq order, in among any RFP tickets. On a fresh connection, a resync, or the first ready after a failure, it re-reads `GET /operations/alerts`. The events that arrive while that read is in flight are applied on top of it, so a snapshot can never undo a resolution or an acknowledgement it predates. The browser also advances its cursor past any numbered event it cannot use (newer than its code, or refused), so a reconnect never asks for it again.

## HTTP

| Route | Answer |
|---|---|
| `GET /operations/alerts?status=open\|resolved\|all&since_hours=` | `{alerts: [...]}`, newest first. Every open alert, plus those resolved within `since_hours` (168 by default). 401 / 403 for non-staff, 503 when the database fails. |
| `POST /operations/alerts/{alert_id}/acknowledge` | 200 with the alert. The first acknowledgement writes `operational_alert_acknowledged` and wakes the stream; repeating it changes nothing (200, no second event). 409 when already resolved, 404 unknown, 403 for a customer. |

## The backoffice

- **The banner,** under the header on every page while an alert is open.
  - A warning triangle: `--status-warning` for warnings; `--status-critical` plus a "Critical" tag for critical ones. The warning colour is a fill and an edge, never text, as `globals.css` requires.
  - The label, then "for 2 h 14 min · expected within 2 h · last order Nov 30, 09:12". Old dates show their year.
  - "Acknowledge", or "Acknowledged by … at …".
  - Up to three alerts, then "+N more · View all alerts".
  - A resolution replaces the row with a short green line ("Outbound orders resumed at Zaragoza", "Ana Whitfield is no longer holding up ModaViva", …) that leaves after 10 s.
  - Screen readers hear the change in the set of alerts once, through a polite region, never the durations ticking.

  It is a strip with warning marks, never the RFP's floating Cream Orange bell, so the two are never confused.
- **"Alerts"** in the nav counts the open alerts nobody has acknowledged.
- **`/operations`:** what is open now (live, with acknowledgement), and what resolved in the last 7 days (read once, then extended by live resolutions).
- **Customer accounts** never connect, so they have no banner and no count. `/operations` tells them it is for staff.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPS_ACTIVITY_RULES_PATH` | `config/operational-activity-rules.json` | The rules file (relative to the repository). |
| `OPS_MONITOR_ENABLED` | `true` | Evaluate inside the API. The unit tests turn it off. |
| `OPS_MONITOR_INTERVAL_SECONDS` | 60 | Evaluation interval (1–3600). |
| `LIVE_STREAM_*` | | The shared stream's settings ([live-notifications.md](../rfp/live-notifications.md)). |

**Database:** two tables are created at start-up when missing, RLS on with no policies: `operational_alerts` and the shared outbox `live_notifications`. Inspect the live Supabase schema before the first start against it.

**CLI:**
- `npm run ops -- rules` lists and validates the rules;
- `npm run ops -- check --dry-run` reports what would open or resolve, writing nothing (it still reports the reachable sources when the database is down);
- `npm run ops -- check` evaluates once, like the API;
- `npm run ops -- alerts [--all]` lists the alerts.

Exit codes: 2 invalid rules, 3 database unavailable.

## Tests

| Suite | What it proves |
|---|---|
| `tests/operations/test_rules.py` | The shipped file covers the four sources. A bad source, warehouse, activity, period, timezone, window, id, extra field, duplicate or missing file is refused with its reason, and an invalid file stops start-up. |
| `tests/operations/test_schedule.py` | Wall-clock versus working minutes; nights and weekends; Monterrey versus Madrid; the October daylight-saving day. |
| `tests/operations/test_monitor.py` | Per source, on SQLite with a fixed clock:<br>• opens once across repeated rounds;<br>• resolves with its reason;<br>• different warehouses and clients are different subjects;<br>• a failing job is reported with its last run, and a never-run job too;<br>• the RFP owner is named, and a revision restarts the clock;<br>• one alert per branch (also with identical timestamps, found on the real data);<br>• disabled or removed rules;<br>• a failing source leaves its alerts alone;<br>• one live event per change, with valid payloads;<br>• the preview writes nothing. |
| `tests/operations/test_operations_api.py` | **On the wire:** `operational_inactivity_detected` with its `id:` and `data:` validated against `OperationalAlert`; RFP tickets and alerts **in one cursor in commit order**, also through the old route.<br>**The routes:** snapshot (401/403); acknowledgement (200, idempotent, broadcast; 409/404/403).<br>**The monitor task:** wakes only on change, survives a failing round, honours `OPS_MONITOR_ENABLED`. |
| `tests/operations/test_alerts_live.py` | **Real socket:** an alert reaches an open dashboard in under 1.5 s with a 30 s poll; an alert raised while a tab is disconnected is replayed once, then its resolution arrives live, and no `alert_id` appears twice. |
| `tests/operations/test_operations_postgres.py` (opt-in) | Two concurrent evaluations open a single alert; the weekly report's run log is read in its own schema; RLS and JSONB. |
| `tests/frontend/backoffice/operations-live.test.ts` | The validator; idempotent events; an acknowledgement never undone by an older copy; resolution; the snapshot plus in-flight events; ordering; the copy (durations, the year, resolutions); the calls. |
| `tests/frontend/backoffice/live-stream.test.ts` | The three alert events typed and refused; the cursor moving past unusable events; tickets and alerts sharing one cursor without repeats. |

## Verification (2026-11-26)

- **Suites:**
  - backend **2408 passed, 66 skipped, 0 failed**, run with `.venv/bin/python` and `DYLD_LIBRARY_PATH`, without the stale `test_agent_evals.py`. Then `tests/operations` passed again (39) after the later fixes;
  - opt-in PostgreSQL **15/15** on a throwaway `postgres:17-alpine` (the 3 above, plus the 12 RFP tests now going through the shared outbox);
  - backoffice Jest **359**; `tsc` and ESLint clean; ruff clean on every changed Python file; mypy clean on the new code.
- **In the browser:** Chrome via playwright-core; the real API on a throwaway Postgres behind a TCP proxy the script could cut; scratch rules (Zaragoza outbound within 1 minute, 24/7; SLA-critical incidents); monitor every 3 s; two Sales accounts and a customer. **22 of 22 checks** passed:
  1. **Snapshot:** an alert open before the tabs connected (Central's incidents, tagged Critical) came from the snapshot, and "Alerts" counted it.
  2. **Live:** a minute after the last Zaragoza order, the alert reached both tabs **4 ms apart**, without a list re-read, reading "for 1 min · expected within 1 min · last order …".
  3. **Acknowledgement:** A acknowledged it, B showed "Acknowledged by sales.a@trackflow.com", and the count dropped.
  4. **Resolution:** a new outbound order resolved it on both tabs within one monitor round ("Outbound orders resumed at Zaragoza").
  5. **Recovery:** both tabs were cut off while the next silence opened. On reconnect, B resumed with `Last-Event-ID` and showed that alert **exactly once**.
  6. **Alongside RFPs:** a new RFP still reached B while alerts were open.
  7. **The page:** `/operations` listed the open alerts and the week's resolutions.
  8. **Customer:** no stream, no banner, no alert read.
  9. **Display and console:** dark mode, reduced motion, and 390 px with no overflow; no console errors.
- **The RFP browser verification** (34 checks) was run again on the refactored stream: **34/34**.
- **Against the developer's real data** (`npm run ops -- check --dry-run`, read-only):
  - Supabase was unreachable (`OperationalError`, the same failure as the nightly export's since 2026-11-13), so the seven database rules were skipped, as designed;
  - the incident rules, on the real TinyDB, would open three alerts: 17 SLA-critical incidents at Central, 12 others at Central, and 1 at Zaragoza — Warehouse;
  - this run found two real defects, fixed and covered by tests: incidents with identical timestamps crashed the sort, and a non-database error in one source aborted the whole round.

## Known limits and next steps

- **Supabase:** it was unreachable on 2026-11-26. Inspect the schema before the first start against it (two new tables), and re-run `npm run ops -- check --dry-run`. It should then report `nightly-export` as overdue since 2026-11-12.
- **The calendar:** operating hours know days and hours, not public holidays. A holiday counts as a working day.
- **Delivery:** alerts reach open dashboards only. An email or phone escalation for critical alerts (Resend is available) is a natural next step, and an acknowledgement could stop it.
- **Pre-existing, not from this work:** `/incidents` overflows horizontally at 390 px (its table's `min-w-[56rem]`), with or without alerts.
- **Thresholds:** they are configuration, not learned. `PIPELINE_DESIGN.md` suggests calibrating warehouse silences on four weeks of baseline once real order volumes exist.
