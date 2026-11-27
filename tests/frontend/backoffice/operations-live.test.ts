import {
  ACTIVITY_RESUMED,
  ALERT_ACKNOWLEDGED,
  EMPTY_ALERTS,
  INACTIVITY_DETECTED,
  acknowledgeAlert,
  alertDetail,
  applyAlertEvent,
  applyAlertSnapshot,
  describeAlertsError,
  dismissResumed,
  durationLabel,
  isOperationalAlert,
  listOperationalAlerts,
  resumedCopy,
  sortedOpenAlerts,
  unacknowledgedCount,
  type OperationalAlert,
} from "@/lib/operations-live";
import { ApiError } from "@/lib/api-client";
import { clearToken, storeToken } from "@/lib/auth-storage";
import { installBrowser, removeBrowser } from "../shared/support/browser";
import { API, jsonResponse, scriptFetch } from "../shared/support/http";

beforeEach(() => {
  installBrowser();
  storeToken("staff-session");
});
afterEach(() => {
  clearToken();
  removeBrowser();
});

function alert(n: number, overrides: Partial<OperationalAlert> = {}): OperationalAlert {
  return {
    alert_id: `20000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    rule_id: "zaragoza-outbound",
    source: "warehouse_orders",
    activity: "outbound_order_created",
    subject: { warehouse: "Zaragoza", client_id: null },
    label: "No outbound order registered at Zaragoza",
    severity: "warning",
    status: "open",
    expected_within_minutes: 120,
    waiting_since: "2026-11-30T08:00:00Z",
    last_activity_at: "2026-11-30T08:00:00Z",
    detected_at: "2026-11-30T10:01:00Z",
    overdue_minutes: 1,
    details: {},
    acknowledged_at: null,
    acknowledged_by: null,
    resolved_at: null,
    resolved_reason: null,
    ...overrides,
  };
}

const acknowledged = (n: number) =>
  alert(n, { acknowledged_at: "2026-11-30T10:05:00Z", acknowledged_by: "ana.whitfield@trackflow.com" });
const resolved = (n: number) =>
  alert(n, { status: "resolved", resolved_at: "2026-11-30T10:20:00Z", resolved_reason: "activity_resumed" });

describe("isOperationalAlert", () => {
  it("accepts the API's alert and refuses anything else", () => {
    expect(isOperationalAlert(alert(1))).toBe(true);
    expect(isOperationalAlert(resolved(1))).toBe(true);
    expect(isOperationalAlert({ ...alert(1), status: "closed" })).toBe(false);
    expect(isOperationalAlert({ ...alert(1), expected_within_minutes: "120" })).toBe(false);
    expect(isOperationalAlert({ ...alert(1), details: [] })).toBe(false);
    expect(isOperationalAlert(null)).toBe(false);
  });
});

describe("the alerts state", () => {
  it("shows each alert once, however often its event arrives", () => {
    const detected = { event: INACTIVITY_DETECTED, alert: alert(1) } as const;
    const once = applyAlertEvent(EMPTY_ALERTS, detected);
    const twice = applyAlertEvent(once, detected);

    expect(Object.keys(twice.open)).toEqual([alert(1).alert_id]);
    expect(twice).toEqual(once);
  });

  it("keeps the acknowledgement even when an older copy arrives later", () => {
    let state = applyAlertEvent(EMPTY_ALERTS, { event: INACTIVITY_DETECTED, alert: alert(1) });
    state = applyAlertEvent(state, { event: ALERT_ACKNOWLEDGED, alert: acknowledged(1) });
    state = applyAlertEvent(state, { event: INACTIVITY_DETECTED, alert: alert(1) }); // a replay

    expect(state.open[alert(1).alert_id].acknowledged_by).toBe("ana.whitfield@trackflow.com");
    expect(unacknowledgedCount(state)).toBe(0);
  });

  it("removes a resolved alert and keeps it briefly as 'resumed'", () => {
    let state = applyAlertEvent(EMPTY_ALERTS, { event: INACTIVITY_DETECTED, alert: alert(1) });
    state = applyAlertEvent(state, { event: ACTIVITY_RESUMED, alert: resolved(1) });

    expect(state.open).toEqual({});
    expect(state.resumed.map((item) => item.alert_id)).toEqual([alert(1).alert_id]);
    // A resolution for an alert this tab never had changes nothing.
    expect(applyAlertEvent(state, { event: ACTIVITY_RESUMED, alert: resolved(9) })).toBe(state);
    expect(dismissResumed(state, alert(1).alert_id).resumed).toEqual([]);
  });

  it("a snapshot read while events arrive cannot undo them", () => {
    // The snapshot was read before alert 1 resolved and alert 2 was acknowledged.
    const state = applyAlertSnapshot(EMPTY_ALERTS, [alert(1), alert(2)], [
      { event: ACTIVITY_RESUMED, alert: resolved(1) },
      { event: ALERT_ACKNOWLEDGED, alert: acknowledged(2) },
    ]);

    expect(Object.keys(state.open)).toEqual([alert(2).alert_id]);
    expect(state.open[alert(2).alert_id].acknowledged_at).not.toBeNull();
  });

  it("lists critical alerts first, then the longest overdue", () => {
    let state = EMPTY_ALERTS;
    for (const item of [
      alert(1, { waiting_since: "2026-11-30T09:00:00Z" }),
      alert(2, { waiting_since: "2026-11-30T07:00:00Z" }),
      alert(3, { severity: "critical", waiting_since: "2026-11-30T09:30:00Z" }),
    ]) {
      state = applyAlertEvent(state, { event: INACTIVITY_DETECTED, alert: item });
    }

    expect(sortedOpenAlerts(state).map((item) => item.alert_id.slice(-1))).toEqual(["3", "2", "1"]);
    expect(unacknowledgedCount(state)).toBe(3);
  });
});

describe("copy", () => {
  it("says how long, against what was expected, with the evidence", () => {
    expect(durationLabel(45)).toBe("45 min");
    expect(durationLabel(134)).toBe("2 h 14 min");
    expect(durationLabel(120)).toBe("2 h");
    expect(durationLabel(76 * 60)).toBe("3 d 4 h");

    const now = Date.parse("2026-11-30T10:14:00Z");
    expect(alertDetail(alert(1), now)).toMatch(/^for 2 h 14 min · expected within 2 h · last order /);
    expect(alertDetail(alert(1, { waiting_since: null, last_activity_at: null }), now)).toBe(
      "expected within 2 h · never happened yet",
    );
    // An old date says its year: "Jan 1" alone would read as this January.
    const seeded = alert(3, { source: "incidents", waiting_since: "2024-01-01T12:00:00Z" });
    expect(alertDetail(seeded, now)).toMatch(/oldest open since .*2024/);
    expect(alertDetail(alert(1), now)).not.toMatch(/2026/);
    const job = alert(2, { source: "scheduled_job", details: { last_run_status: "failed" } });
    expect(alertDetail(job, now)).toMatch(/last completed run .+ · last run failed$/);
  });

  it("says what a resolution means for each kind of activity", () => {
    expect(resumedCopy(resolved(1))).toBe("Outbound orders resumed at Zaragoza");
    expect(
      resumedCopy({ ...resolved(1), source: "scheduled_job", subject: { job_name: "nightly_export" } }),
    ).toBe("The nightly telemetry export completed again");
    expect(
      resumedCopy({
        ...resolved(1),
        source: "rfp_approval",
        resolved_reason: "slot_closed",
        subject: { owner: "Ana Whitfield", client_name: "ModaViva" },
      }),
    ).toBe("Ana Whitfield is no longer holding up ModaViva");
    expect(resumedCopy({ ...resolved(1), source: "incidents", subject: { branch: "zaragoza_warehouse" } })).toBe(
      "Zaragoza — Warehouse picked up its incidents",
    );
    expect(resumedCopy({ ...resolved(1), resolved_reason: "rule_disabled" })).toBe(
      "No longer watched: No outbound order registered at Zaragoza",
    );
  });
});

describe("calls", () => {
  it("reads the snapshot and acknowledges with the session's token", async () => {
    const requests = scriptFetch(jsonResponse(200, { alerts: [alert(1)] }), jsonResponse(200, acknowledged(1)));

    await expect(listOperationalAlerts()).resolves.toEqual([alert(1)]);
    await expect(acknowledgeAlert(alert(1).alert_id)).resolves.toEqual(acknowledged(1));

    expect(requests.map((request) => [request.method, request.url])).toEqual([
      ["GET", `${API}/operations/alerts?status=open`],
      ["POST", `${API}/operations/alerts/${alert(1).alert_id}/acknowledge`],
    ]);
    expect(requests[1].headers.Authorization).toBe("Bearer staff-session");
  });

  it("never renders a reply it cannot read, and explains a refusal", async () => {
    scriptFetch(jsonResponse(200, { alerts: [{ alert_id: "x" }] }));

    await expect(listOperationalAlerts()).rejects.toThrow("cannot read");
    expect(describeAlertsError(new ApiError(409, { detail: "resolved" }))).toBe(
      "The activity has resumed: this alert is resolved.",
    );
    expect(describeAlertsError(new ApiError(403, null))).toMatch(/staff only/);
  });
});
