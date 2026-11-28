"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useOperationalAlerts } from "@/components/live/OperationsLiveContext";
import {
  OPERATIONS_PATH,
  alertDetail,
  describeAlertsError,
  resumedCopy,
  sortedOpenAlerts,
  type OperationalAlert,
} from "@/lib/operations-live";

const SHOWN = 3;
const RESUMED_VISIBLE_MS = 10_000;
const TICK_MS = 30_000;

export function WarningMark({ severity }: { severity: OperationalAlert["severity"] }) {
  const fill = severity === "critical" ? "var(--status-critical)" : "var(--status-warning)";
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0">
      <path d="M10 2.2 18.6 17a1 1 0 0 1-.87 1.5H2.27A1 1 0 0 1 1.4 17L10 2.2Z" fill={fill} />
      <path d="M10 7.5v4.6" stroke="#0b2247" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="10" cy="15" r="1.05" fill="#0b2247" />
    </svg>
  );
}

export function CriticalTag() {
  return (
    <span className="mr-2 inline-flex items-center rounded-md bg-[color:var(--status-critical)] px-1.5 py-0.5 align-middle text-[0.65rem] font-bold uppercase tracking-wide text-white">
      Critical
    </span>
  );
}

function clock(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

/** "Acknowledge", or who took it and when. Shared by the banner and the alerts page. */
export function AcknowledgeControl({ alert }: { alert: OperationalAlert }) {
  const { acknowledge } = useOperationalAlerts();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (alert.acknowledged_at) {
    return (
      <p className="text-xs text-slate-600">
        Acknowledged by <span className="font-semibold">{alert.acknowledged_by}</span> at{" "}
        {clock(alert.acknowledged_at)}
      </p>
    );
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <button
        type="button"
        disabled={pending}
        aria-busy={pending}
        onClick={() => {
          setPending(true);
          setError(null);
          acknowledge(alert.alert_id)
            .catch((failure: unknown) => setError(describeAlertsError(failure)))
            .finally(() => setPending(false));
        }}
        className="inline-flex min-h-9 items-center rounded-xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] px-3 py-1.5 text-sm font-semibold text-[color:var(--brand-primary)] transition hover:border-[color:var(--brand-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-60"
      >
        {pending ? "Acknowledging…" : "Acknowledge"}
      </button>
      {error ? (
        <p role="alert" className="text-xs text-red-700">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** Time that moves on by itself, for "for 2 h 14 min". Ticks only while something is shown. */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const timer = setInterval(tick, TICK_MS);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [active]);
  return now;
}

/**
 * Operational inactivity alerts, under the header on every backoffice page while any is open:
 * what did not happen, for how long, against what was expected, and who took it in charge. It is
 * a strip with warning marks, not a floating card, so it never reads as a new RFP; and it stays
 * until the activity returns (then a short "resumed" line replaces it).
 *
 * Screen readers hear a change in the set of alerts once (the polite region below), never the
 * durations ticking.
 */
export function OperationalAlertBanner() {
  const { enabled, alerts, dismissResumed } = useOperationalAlerts();
  const open = sortedOpenAlerts(alerts);
  const now = useNow(enabled && open.length > 0);

  useEffect(() => {
    const timers = alerts.resumed.map((alert) =>
      setTimeout(() => dismissResumed(alert.alert_id), RESUMED_VISIBLE_MS),
    );
    return () => timers.forEach(clearTimeout);
  }, [alerts.resumed, dismissResumed]);

  if (!enabled) return null;
  const newest = [...open].sort((a, b) => Date.parse(b.detected_at) - Date.parse(a.detected_at))[0];
  const announcement = newest
    ? `${open.length} operational ${open.length === 1 ? "alert" : "alerts"}. Newest: ${newest.label}.`
    : alerts.resumed[0]
      ? `${resumedCopy(alerts.resumed[0])}.`
      : "";

  return (
    <>
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {open.length > 0 || alerts.resumed.length > 0 ? (
        <section
          aria-label="Operational alerts"
          className="border-b border-[color:var(--border-soft)] bg-[color:var(--surface)]"
        >
          <div className="mx-auto w-full max-w-6xl space-y-2 px-4 py-3 sm:px-6 md:px-10">
            {open.length > 0 ? (
              <ul className="space-y-2">
                {open.slice(0, SHOWN).map((alert) => (
                  <li
                    key={alert.alert_id}
                    className={`flex flex-wrap items-start gap-3 rounded-xl border border-[color:var(--border-soft)] border-l-4 px-3 py-2 ${
                      alert.severity === "critical"
                        ? "border-l-[color:var(--status-critical)]"
                        : "border-l-[color:var(--status-warning)]"
                    } ${alert.acknowledged_at ? "opacity-80" : ""}`}
                  >
                    <WarningMark severity={alert.severity} />
                    <div className="min-w-0 flex-1">
                      <p className="break-words text-sm font-semibold text-[color:var(--foreground)]">
                        {alert.severity === "critical" ? <CriticalTag /> : null}
                        {alert.label}
                      </p>
                      <p className="mt-0.5 break-words text-xs text-slate-600">{alertDetail(alert, now)}</p>
                    </div>
                    <AcknowledgeControl alert={alert} />
                  </li>
                ))}
              </ul>
            ) : null}
            {alerts.resumed.map((alert) => (
              <p
                key={alert.alert_id}
                className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
              >
                <span aria-hidden="true">✓</span>
                <span className="min-w-0 flex-1 break-words">
                  {resumedCopy(alert)}
                  {alert.resolved_at ? ` · ${clock(alert.resolved_at)}` : ""}
                </span>
                <button
                  type="button"
                  onClick={() => dismissResumed(alert.alert_id)}
                  className="rounded-lg px-2 py-1 text-xs font-semibold underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2"
                >
                  Dismiss
                </button>
              </p>
            ))}
            {open.length > 0 ? (
              <Link
                href={OPERATIONS_PATH}
                className="inline-block text-sm font-semibold text-[color:var(--brand-primary)] underline-offset-2 hover:underline"
              >
                {open.length > SHOWN ? `+${open.length - SHOWN} more · View all alerts` : "View all alerts"}
              </Link>
            ) : null}
          </div>
        </section>
      ) : null}
    </>
  );
}
