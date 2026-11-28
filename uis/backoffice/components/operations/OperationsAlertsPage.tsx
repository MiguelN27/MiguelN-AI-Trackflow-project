"use client";

import { useEffect, useMemo, useState } from "react";
import { StateMessage } from "@/components/common/StateMessage";
import { useOperationalAlerts } from "@/components/live/OperationsLiveContext";
import { AcknowledgeControl, CriticalTag, WarningMark, useNow } from "@/components/operations/OperationalAlertBanner";
import {
  alertDetail,
  describeAlertsError,
  durationLabel,
  listOperationalAlerts,
  resumedCopy,
  sortedOpenAlerts,
  type OperationalAlert,
} from "@/lib/operations-live";

const REASONS: Record<NonNullable<OperationalAlert["resolved_reason"]>, string> = {
  activity_resumed: "Activity resumed",
  slot_closed: "No longer waiting",
  rule_disabled: "Rule disabled",
  rule_removed: "Rule removed",
};

function when(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(
    new Date(value),
  );
}

/**
 * Operational alerts (Milestone 10, Part 2): what is overdue now, live, and what resolved in the
 * last 7 days. The open list is the same live state as the banner; the history is read once and
 * extended by every resolution that arrives on the stream, so nothing here needs a reload.
 */
export function OperationsAlertsPage() {
  const { enabled, alerts, alertsError } = useOperationalAlerts();
  const [history, setHistory] = useState<OperationalAlert[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const open = sortedOpenAlerts(alerts);
  const now = useNow(open.length > 0);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    listOperationalAlerts("all")
      .then((items) => {
        if (active) setHistory(items.filter((alert) => alert.status === "resolved"));
      })
      .catch((failure: unknown) => {
        if (active) setHistoryError(describeAlertsError(failure));
      });
    return () => {
      active = false;
    };
  }, [enabled]);

  const resolved = useMemo(() => {
    const byId = new Map<string, OperationalAlert>();
    for (const alert of [...alerts.resumed, ...(history ?? [])]) {
      if (!byId.has(alert.alert_id)) byId.set(alert.alert_id, alert);
    }
    return [...byId.values()].sort((a, b) => Date.parse(b.resolved_at ?? "") - Date.parse(a.resolved_at ?? ""));
  }, [alerts.resumed, history]);

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 md:px-10">
      <header className="mb-6 border-b border-[color:var(--border-soft)] pb-5">
        <p className="mb-2 text-sm font-medium text-slate-600">Operations</p>
        <h1 className="text-2xl font-semibold leading-tight">Operational alerts</h1>
        <p className="mt-2 max-w-3xl text-sm text-slate-600">
          Raised when expected activity does not happen in time: a warehouse that registers no orders during working
          hours, a scheduled job that does not complete, a department owner who has not decided on an RFP section, a
          branch whose incidents nobody picked up. Each one resolves by itself when the activity returns.
        </p>
      </header>

      {!enabled ? (
        <StateMessage tone="error">Operational alerts are available to TrackFlow staff only.</StateMessage>
      ) : (
        <>
          <section aria-labelledby="open-heading">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
              <h2 id="open-heading" className="text-lg font-semibold">
                Open now
              </h2>
              <p className="text-sm text-slate-600">
                {open.length === 0 ? "Nothing overdue" : `${open.length} open, updated live`}
              </p>
            </div>
            {alertsError ? <StateMessage tone="error">{alertsError}</StateMessage> : null}
            {open.length === 0 && !alertsError ? (
              <StateMessage tone="success">Every watched activity is on time.</StateMessage>
            ) : (
              <ul className="space-y-3">
                {open.map((alert) => (
                  <li
                    key={alert.alert_id}
                    className={`flex flex-wrap items-start gap-3 rounded-2xl border border-[color:var(--border-soft)] border-l-4 bg-[color:var(--surface)] p-4 ${
                      alert.severity === "critical"
                        ? "border-l-[color:var(--status-critical)]"
                        : "border-l-[color:var(--status-warning)]"
                    }`}
                  >
                    <WarningMark severity={alert.severity} />
                    <div className="min-w-0 flex-1">
                      <p className="break-words font-semibold">
                        {alert.severity === "critical" ? <CriticalTag /> : null}
                        {alert.label}
                      </p>
                      <p className="mt-1 break-words text-sm text-slate-600">{alertDetail(alert, now)}</p>
                      <p className="mt-1 text-xs text-slate-500">
                        Detected {when(alert.detected_at)}
                        {alert.overdue_minutes ? ` · ${durationLabel(alert.overdue_minutes)} past the expected period` : ""}
                        {" · "}rule <code className="font-mono">{alert.rule_id}</code>
                      </p>
                    </div>
                    <AcknowledgeControl alert={alert} />
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="resolved-heading" className="mt-10">
            <h2 id="resolved-heading" className="mb-3 text-lg font-semibold">
              Resolved in the last 7 days
            </h2>
            {historyError ? <StateMessage tone="error">{historyError}</StateMessage> : null}
            {history === null && !historyError ? <p className="text-sm text-slate-600">Loading…</p> : null}
            {history !== null && resolved.length === 0 ? (
              <StateMessage tone="info">Nothing resolved this week.</StateMessage>
            ) : null}
            {resolved.length > 0 ? (
              <div className="overflow-x-auto border-y border-[color:var(--border-soft)] bg-[color:var(--surface)]">
                <table className="block w-full text-left text-sm sm:table sm:min-w-[720px]">
                  <caption className="sr-only">Alerts resolved in the last 7 days, newest first</caption>
                  <thead className="hidden border-b border-[color:var(--border-soft)] bg-slate-50 text-slate-600 sm:table-header-group">
                    <tr>
                      {["What", "Detected", "Resolved", "How"].map((label) => (
                        <th scope="col" key={label} className="px-3 py-3 font-semibold">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="block divide-y divide-slate-100 sm:table-row-group">
                    {resolved.map((alert) => (
                      <tr key={alert.alert_id} className="grid grid-cols-2 gap-2 p-4 sm:table-row sm:p-0">
                        <th scope="row" className="col-span-2 min-w-0 break-words font-semibold sm:px-3 sm:py-3">
                          {resumedCopy(alert)}
                          <span className="block text-xs font-normal text-slate-600">{alert.label}</span>
                        </th>
                        <td className="sm:px-3 sm:py-3">{when(alert.detected_at)}</td>
                        <td className="sm:px-3 sm:py-3">{alert.resolved_at ? when(alert.resolved_at) : "—"}</td>
                        <td className="col-span-2 sm:col-span-1 sm:px-3 sm:py-3">
                          {alert.resolved_reason ? REASONS[alert.resolved_reason] : "—"}
                          {alert.acknowledged_by ? (
                            <span className="block text-xs text-slate-600">Taken by {alert.acknowledged_by}</span>
                          ) : null}
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
