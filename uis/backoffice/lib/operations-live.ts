/**
 * Operational inactivity alerts, as the backoffice reads them (Milestone 10, Part 2).
 *
 * An alert says that expected activity did not happen within its period: a warehouse that
 * registered no orders, a scheduled job that did not complete, an RFP owner who has not decided,
 * a branch whose incidents nobody picked up. It arrives on the shared live stream
 * (`operational_inactivity_detected`, `operational_activity_resumed`,
 * `operational_alert_acknowledged`, each carrying the whole alert) and from
 * `GET /operations/alerts` (the snapshot). Staff take one in charge with
 * `POST /operations/alerts/{id}/acknowledge`. Shapes mirror `services/operations/schemas.py`.
 *
 * Everything here is pure, so it is unit-tested without a browser. Alerts are keyed by
 * `alert_id`: a replayed or repeated event never shows an alert twice.
 */

import { ApiError, parseResponseJson, requestAuthenticatedApi } from "@/lib/api-client";
import { describeError, type ErrorCopy } from "@/lib/friendly-error";

export const OPERATIONS_PATH = "/operations";
const ALERTS_API = "/operations/alerts";

export const INACTIVITY_DETECTED = "operational_inactivity_detected";
export const ACTIVITY_RESUMED = "operational_activity_resumed";
export const ALERT_ACKNOWLEDGED = "operational_alert_acknowledged";
export const ALERT_EVENTS = [INACTIVITY_DETECTED, ACTIVITY_RESUMED, ALERT_ACKNOWLEDGED] as const;
export type AlertEventName = (typeof ALERT_EVENTS)[number];

export type AlertSource = "warehouse_orders" | "scheduled_job" | "rfp_approval" | "incidents";

export type OperationalAlert = {
  alert_id: string;
  rule_id: string;
  source: AlertSource;
  activity: string;
  subject: Record<string, string | null>;
  label: string;
  severity: "warning" | "critical";
  status: "open" | "resolved";
  expected_within_minutes: number;
  waiting_since: string | null;
  last_activity_at: string | null;
  detected_at: string;
  overdue_minutes: number | null;
  details: Record<string, unknown>;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_reason: "activity_resumed" | "slot_closed" | "rule_disabled" | "rule_removed" | null;
};

// --- Validation --------------------------------------------------------------------------------

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const isString = (value: unknown): value is string => typeof value === "string";
const isNullableString = (value: unknown): value is string | null => value === null || isString(value);
const SOURCES: readonly string[] = ["warehouse_orders", "scheduled_job", "rfp_approval", "incidents"];
const REASONS: readonly (string | null)[] = [null, "activity_resumed", "slot_closed", "rule_disabled", "rule_removed"];

export function isOperationalAlert(value: unknown): value is OperationalAlert {
  return (
    isRecord(value) &&
    isString(value.alert_id) &&
    isString(value.rule_id) &&
    isString(value.source) &&
    SOURCES.includes(value.source) &&
    isString(value.activity) &&
    isRecord(value.subject) &&
    Object.values(value.subject).every(isNullableString) &&
    isString(value.label) &&
    (value.severity === "warning" || value.severity === "critical") &&
    (value.status === "open" || value.status === "resolved") &&
    typeof value.expected_within_minutes === "number" &&
    isNullableString(value.waiting_since) &&
    isNullableString(value.last_activity_at) &&
    isString(value.detected_at) &&
    (value.overdue_minutes === null || typeof value.overdue_minutes === "number") &&
    isRecord(value.details) &&
    isNullableString(value.acknowledged_at) &&
    isNullableString(value.acknowledged_by) &&
    isNullableString(value.resolved_at) &&
    REASONS.includes(value.resolved_reason as string | null)
  );
}

// --- Calls --------------------------------------------------------------------------------------

class AlertsResponseError extends Error {}

const UNREADABLE =
  "The alerts service returned a reply this page cannot read. Please refresh, and contact TrackFlow Tech if it keeps happening.";

export async function listOperationalAlerts(status: "open" | "all" = "open"): Promise<OperationalAlert[]> {
  const response = await requestAuthenticatedApi(`${ALERTS_API}?status=${status}`, { cache: "no-store" });
  const value = await parseResponseJson(response);
  if (!isRecord(value) || !Array.isArray(value.alerts) || !value.alerts.every(isOperationalAlert)) {
    throw new AlertsResponseError(UNREADABLE);
  }
  return value.alerts;
}

export async function acknowledgeAlert(alertId: string): Promise<OperationalAlert> {
  const response = await requestAuthenticatedApi(`${ALERTS_API}/${encodeURIComponent(alertId)}/acknowledge`, {
    method: "POST",
    cache: "no-store",
  });
  const value = await parseResponseJson(response);
  if (!isOperationalAlert(value)) throw new AlertsResponseError(UNREADABLE);
  return value;
}

const ALERTS_COPY: ErrorCopy = {
  offline: "The alerts could not be reached. Check your connection and try again.",
  server: "The alerts are unavailable right now. Please try again in a moment.",
  notFound: "This alert no longer exists.",
};

export function describeAlertsError(error: unknown): string {
  if (error instanceof AlertsResponseError) return error.message;
  if (error instanceof ApiError && error.status === 409) return "The activity has resumed: this alert is resolved.";
  if (error instanceof ApiError && error.status === 403) return "Operational alerts are available to TrackFlow staff only.";
  return describeError(error, ALERTS_COPY.server, ALERTS_COPY).message;
}

// --- State ------------------------------------------------------------------------------------------

export type AlertUpdate = { event: AlertEventName; alert: OperationalAlert };

export type AlertsState = {
  /** Open alerts, by id. */
  open: Record<string, OperationalAlert>;
  /** Alerts that resolved in this tab, newest first: the brief "activity resumed" notice. */
  resumed: OperationalAlert[];
};

export const EMPTY_ALERTS: AlertsState = { open: {}, resumed: [] };
const MAX_RESUMED = 5;

/** One live event folded into the state. Idempotent: the same event twice changes nothing. */
export function applyAlertEvent(state: AlertsState, update: AlertUpdate): AlertsState {
  const { alert } = update;
  if (alert.status === "resolved" || update.event === ACTIVITY_RESUMED) {
    if (!(alert.alert_id in state.open)) return state;
    const { [alert.alert_id]: _closed, ...open } = state.open;
    void _closed;
    return { open, resumed: [alert, ...state.resumed.filter((item) => item.alert_id !== alert.alert_id)].slice(0, MAX_RESUMED) };
  }
  const current = state.open[alert.alert_id];
  // An older copy (no acknowledgement yet) never overwrites a newer one.
  if (current?.acknowledged_at && !alert.acknowledged_at) return state;
  return { ...state, open: { ...state.open, [alert.alert_id]: alert } };
}

/**
 * The open alerts from `GET /operations/alerts`, then every live event that arrived while it was
 * being read: the snapshot cannot undo a resolution or an acknowledgement it predates.
 */
export function applyAlertSnapshot(state: AlertsState, snapshot: OperationalAlert[], since: AlertUpdate[]): AlertsState {
  const open = Object.fromEntries(snapshot.filter((alert) => alert.status === "open").map((alert) => [alert.alert_id, alert]));
  return since.reduce(applyAlertEvent, { ...state, open });
}

export function dismissResumed(state: AlertsState, alertId: string): AlertsState {
  return { ...state, resumed: state.resumed.filter((alert) => alert.alert_id !== alertId) };
}

/** Open alerts, critical first, then the longest overdue. */
export function sortedOpenAlerts(state: AlertsState): OperationalAlert[] {
  return Object.values(state.open).sort(
    (a, b) =>
      Number(b.severity === "critical") - Number(a.severity === "critical") ||
      Date.parse(a.waiting_since ?? a.detected_at) - Date.parse(b.waiting_since ?? b.detected_at),
  );
}

export function unacknowledgedCount(state: AlertsState): number {
  return Object.values(state.open).filter((alert) => !alert.acknowledged_at).length;
}

// --- Copy ----------------------------------------------------------------------------------------------

/** "45 min", "2 h 14 min", "3 d 4 h". */
export function durationLabel(minutes: number): string {
  const total = Math.max(0, Math.floor(minutes));
  if (total < 60) return `${total} min`;
  const hours = Math.floor(total / 60);
  if (hours < 48) return total % 60 ? `${hours} h ${total % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}

/** "Nov 30, 09:12", with the year when it is not this year's ("Jan 1, 2024, 07:00"). */
function clock(value: string, now: number): string {
  const date = new Date(value);
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return new Intl.DateTimeFormat(undefined, {
    ...(sameYear ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

const LAST_NOUN: Record<AlertSource, string> = {
  warehouse_orders: "last order",
  scheduled_job: "last completed run",
  rfp_approval: "waiting since",
  incidents: "oldest open since",
};

/**
 * The line under the alert's label: how long, what was expected, and the evidence.
 * "for 2 h 14 min · expected within 2 h (working hours) · last order Nov 30, 09:12".
 */
export function alertDetail(alert: OperationalAlert, now: number): string {
  const since = alert.waiting_since ?? alert.last_activity_at;
  const parts: string[] = [];
  if (since) parts.push(`for ${durationLabel((now - Date.parse(since)) / 60_000)}`);
  parts.push(`expected within ${durationLabel(alert.expected_within_minutes)}`);
  parts.push(since ? `${LAST_NOUN[alert.source]} ${clock(since, now)}` : "never happened yet");
  const lastRun = alert.details.last_run_status;
  if (typeof lastRun === "string" && lastRun !== "completed") parts.push(`last run ${lastRun}`);
  return parts.join(" · ");
}

const JOB_NAMES: Record<string, string> = {
  nightly_export: "The nightly telemetry export",
  weekly_warehouse_client_performance: "The weekly warehouse & client report",
};

/** Mirrors `trackflow_shared.incidents.BRANCH_LABELS["en"]`. */
const BRANCH_NAMES: Record<string, string> = {
  central: "Central",
  la_warehouse: "Los Angeles — Warehouse",
  la_office: "Los Angeles — Office",
  zaragoza_warehouse: "Zaragoza — Warehouse",
  zaragoza_office: "Zaragoza — Office",
};

/** What a resolution means, from the alert's own fields: "Outbound orders resumed at Zaragoza". */
export function resumedCopy(alert: OperationalAlert): string {
  if (alert.resolved_reason === "rule_disabled" || alert.resolved_reason === "rule_removed") {
    return `No longer watched: ${alert.label}`;
  }
  const subject = alert.subject;
  switch (alert.source) {
    case "warehouse_orders": {
      const what = alert.activity === "outbound_order_created" ? "Outbound orders" : "Inbound receipts";
      const client = subject.client_id ? ` for ${subject.client_id}` : "";
      return `${what} resumed at ${subject.warehouse ?? "the warehouse"}${client}`;
    }
    case "scheduled_job":
      return `${JOB_NAMES[subject.job_name ?? ""] ?? subject.job_name ?? "The job"} completed again`;
    case "rfp_approval":
      return `${subject.owner ?? "The owner"} is no longer holding up ${subject.client_name ?? "the RFP"}`;
    case "incidents":
      return `${BRANCH_NAMES[subject.branch ?? ""] ?? subject.branch ?? "The branch"} picked up its incidents`;
  }
}
