"use client";

import { createContext, useContext } from "react";
import { EMPTY_ALERTS, type AlertsState, type OperationalAlert } from "@/lib/operations-live";

export type OperationsLiveValue = {
  /** A staff session: only admin and manager accounts receive alerts. */
  enabled: boolean;
  alerts: AlertsState;
  /** Why the open alerts could not be loaded, if they could not. */
  alertsError: string | null;
  /** Take an alert in charge (`POST .../acknowledge`); every tab is told through the stream. */
  acknowledge: (alertId: string) => Promise<OperationalAlert>;
  dismissResumed: (alertId: string) => void;
};

export const DISABLED_OPERATIONS_LIVE: OperationsLiveValue = {
  enabled: false,
  alerts: EMPTY_ALERTS,
  alertsError: null,
  acknowledge: () => Promise.reject(new Error("Operational alerts are not available here.")),
  dismissResumed: () => undefined,
};

export const OperationsLiveContext = createContext<OperationsLiveValue>(DISABLED_OPERATIONS_LIVE);

/** The live operational alerts (`LiveProvider`), or a disabled value outside it. */
export function useOperationalAlerts(): OperationsLiveValue {
  return useContext(OperationsLiveContext);
}
