"use client";

import { useSession } from "@/components/auth/AuthProvider";
import { BackofficeNavShell } from "@/components/common/BackofficeNavShell";
import { useOperationalAlerts } from "@/components/live/OperationsLiveContext";
import { useRfpLive } from "@/components/rfp/RfpLiveContext";
import { RfpLiveIndicator } from "@/components/rfp/RfpLiveIndicator";
import { OPERATIONS_PATH, unacknowledgedCount } from "@/lib/operations-live";
import { RFP_PATH } from "@/lib/rfp";

/**
 * The header for signed-in views: the shared bar plus the account controls, and for staff the
 * live updates: the connection's state, how many new tickets wait on "RFP intake", and how
 * many operational alerts nobody has taken in charge on "Alerts".
 */
export function BackofficeNav() {
  const { user, signOut } = useSession();
  const { live } = useRfpLive();
  const { alerts } = useOperationalAlerts();

  return (
    <BackofficeNavShell
      counts={{ [RFP_PATH]: live.unseen.length, [OPERATIONS_PATH]: unacknowledgedCount(alerts) }}
      actions={
        <>
          <RfpLiveIndicator />
          {user ? (
            <span className="hidden max-w-[16rem] truncate font-mono text-xs text-slate-500 sm:block" title={user.email}>
              {user.email}
            </span>
          ) : null}
          <button
            type="button"
            onClick={signOut}
            className="rounded-xl border border-[color:var(--border-soft)] px-3 py-2 text-sm font-semibold text-slate-600 transition hover:border-[color:var(--brand-primary)] hover:text-[color:var(--brand-primary)]"
          >
            Log out
          </button>
        </>
      }
    />
  );
}
