"use client";

import { createContext, useContext } from "react";
import { EMPTY_LIVE_STATE, type LiveState } from "@/lib/rfp-live";
import type { ConnectionState } from "@/lib/live-stream";

export type RfpLiveValue = {
  /** A staff session: only admin and manager accounts open the stream (the API is staff-only). */
  enabled: boolean;
  connection: ConnectionState;
  /** The browser reports no network. */
  offline: boolean;
  live: LiveState;
  /**
   * Bumped when a list read earlier may be missing tickets: a first connection, or a resync
   * after a gap the server could not replay. Pages holding the ticket list read it again once.
   */
  snapshotEpoch: number;
  markSeen: (ticketId: string) => void;
  markAllSeen: () => void;
  noteOwnUpload: (ticketId: string) => void;
  reconnectNow: () => void;
};

const noop = () => undefined;

export const DISABLED_RFP_LIVE: RfpLiveValue = {
  enabled: false,
  connection: { kind: "idle" },
  offline: false,
  live: EMPTY_LIVE_STATE,
  snapshotEpoch: 0,
  markSeen: noop,
  markAllSeen: noop,
  noteOwnUpload: noop,
  reconnectNow: noop,
};

export const RfpLiveContext = createContext<RfpLiveValue>(DISABLED_RFP_LIVE);

/** The live RFP notifications (`LiveProvider`), or a disabled value outside it. */
export function useRfpLive(): RfpLiveValue {
  return useContext(RfpLiveContext);
}
