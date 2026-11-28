"use client";

import { useEffect, useState } from "react";
import { useRfpLive } from "@/components/rfp/RfpLiveContext";
import { BACKOFF_MAX_MS, type ConnectionState } from "@/lib/live-stream";

type Display = { label: string; announcement: string; dot: string; retry: boolean };

function describe(connection: ConnectionState, offline: boolean, now: number): Display | null {
  if (offline && connection.kind !== "unavailable" && connection.kind !== "idle") {
    return {
      label: "Offline",
      announcement: "You are offline. New RFPs will appear when the connection is back.",
      dot: "bg-slate-400",
      retry: false,
    };
  }
  switch (connection.kind) {
    case "connecting":
      return { label: "Connecting…", announcement: "", dot: "bg-slate-400", retry: false };
    case "live":
      return { label: "Live", announcement: "Live RFP updates connected.", dot: "bg-emerald-500", retry: false };
    case "reconnecting": {
      const seconds = Math.min(BACKOFF_MAX_MS / 1000, Math.max(0, Math.ceil((connection.retryAt - now) / 1000)));
      return {
        label: seconds > 0 ? `Reconnecting in ${seconds} s` : "Reconnecting…",
        announcement: "Live RFP updates lost. Reconnecting.",
        dot: "bg-amber-500",
        retry: seconds > 0,
      };
    }
    case "unavailable":
      return connection.reason === "forbidden"
        ? { label: "Live updates unavailable", announcement: "", dot: "bg-slate-400", retry: false }
        : null;
    default:
      return null;
  }
}

/**
 * The live-updates state in the header, so a dropped connection is never silent: "Live",
 * "Reconnecting in 8 s" (with "Retry now"), or "Offline". Screen readers hear the change of
 * state once, never the countdown.
 */
export function RfpLiveIndicator() {
  const { enabled, connection, offline, reconnectNow } = useRfpLive();
  const [now, setNow] = useState(() => Date.now());
  const counting = connection.kind === "reconnecting";

  useEffect(() => {
    if (!counting) return;
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const timer = setInterval(tick, 1000);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [counting]);

  if (!enabled) return null;
  const display = describe(connection, offline, now);
  if (!display) return null;

  return (
    <div className="flex items-center gap-2 text-xs font-semibold text-slate-600">
      <span className="flex items-center gap-1.5" aria-hidden="true">
        <span className={`h-2 w-2 shrink-0 rounded-full ${display.dot}`} />
        <span className="whitespace-nowrap">{display.label}</span>
      </span>
      <span className="sr-only" role="status" aria-live="polite">
        {display.announcement}
      </span>
      {display.retry ? (
        <button
          type="button"
          onClick={reconnectNow}
          className="whitespace-nowrap rounded-lg px-1.5 py-1 text-[color:var(--brand-primary)] underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2"
        >
          Retry now
        </button>
      ) : null}
    </div>
  );
}
