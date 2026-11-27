"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useSession } from "@/components/auth/AuthProvider";
import { NewRfpToast } from "@/components/rfp/NewRfpToast";
import { RfpLiveContext, type RfpLiveValue } from "@/components/rfp/RfpLiveContext";
import {
  EMPTY_LIVE_STATE,
  markAllSeen as markAllSeenIn,
  markSeen as markSeenIn,
  noteOwnUpload as noteOwnUploadIn,
  recordArrival,
  titleWithCount,
  type LiveState,
} from "@/lib/rfp-live";
import { RfpStreamConnection, type ConnectionState } from "@/lib/rfp-stream";

function subscribeToNetwork(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

function subscribeToVisibility(onChange: () => void) {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
}

const isOffline = () => !navigator.onLine;
const isHidden = () => document.visibilityState === "hidden";
const never = () => false;

/**
 * One live connection per signed-in staff tab, for the whole backoffice (Milestone 10): a new
 * RFP ticket shows up on any page the moment it is registered, as a notification, a count on the
 * "RFP intake" link, the tab title while the tab is in the background, and a new row on `/rfp`.
 *
 * The connection (`lib/rfp-stream.ts`) reconnects by itself with progressive backoff and resumes
 * with `Last-Event-ID`; this provider adds the browser's hints (back online, tab visible again)
 * so it does not wait out a backoff it no longer needs.
 */
export function RfpLiveProvider({ children }: { children: ReactNode }) {
  const { user } = useSession();
  const enabled = user?.role === "admin" || user?.role === "manager";
  const [connection, setConnection] = useState<ConnectionState>({ kind: "idle" });
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE_STATE);
  const [snapshotEpoch, setSnapshotEpoch] = useState(0);
  const streamRef = useRef<RfpStreamConnection | null>(null);
  const offline = useSyncExternalStore(subscribeToNetwork, isOffline, never);
  const hidden = useSyncExternalStore(subscribeToVisibility, isHidden, never);

  useEffect(() => {
    if (!enabled) return;
    // Events before `rfp_stream_ready` on a resumed connection are replays: they were
    // registered while this tab was disconnected.
    let caughtUp = false;
    // The connection failed since it was last live (not the server's routine end of a stream).
    let recovering = false;
    const stream = new RfpStreamConnection({
      onState: (state) => {
        if (state.kind === "connecting") caughtUp = false;
        if (state.kind === "reconnecting") recovering = true;
        setConnection(state);
      },
      onEvent: (event) => {
        if (event.type === "ticket_created") {
          const arrival = { ticket: event.ticket, seq: event.seq, whileOffline: !caughtUp };
          setLive((current) => recordArrival(current, arrival));
          return;
        }
        // The list is read again once when it may be stale: a first connection (anything
        // registered before it), a resync (a gap the replay cannot cover), or the first ready
        // after a failure (missed tickets came by replay, but the tickets already listed may have
        // moved on meanwhile, and a list read may have failed during the outage).
        const stale = event.type === "resync" || (event.type === "ready" && (event.fresh || recovering));
        if (event.type === "ready") {
          caughtUp = true;
          recovering = false;
        }
        if (stale) setSnapshotEpoch((value) => value + 1);
      },
    });
    streamRef.current = stream;
    stream.start();

    const reconnect = () => stream.reconnectNow();
    const onVisible = () => {
      if (document.visibilityState === "visible") stream.reconnectNow();
    };
    window.addEventListener("online", reconnect);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("online", reconnect);
      document.removeEventListener("visibilitychange", onVisible);
      stream.stop();
      streamRef.current = null;
      setConnection({ kind: "idle" });
      setLive(EMPTY_LIVE_STATE);
    };
  }, [enabled, user?.email]);

  const unseenCount = live.unseen.length;
  useEffect(() => {
    document.title = titleWithCount(document.title, hidden ? unseenCount : 0);
  }, [hidden, unseenCount]);

  const markSeen = useCallback((ticketId: string) => setLive((current) => markSeenIn(current, ticketId)), []);
  const markAllSeen = useCallback(() => setLive((current) => markAllSeenIn(current)), []);
  const noteOwnUpload = useCallback(
    (ticketId: string) => setLive((current) => noteOwnUploadIn(current, ticketId)),
    [],
  );
  const reconnectNow = useCallback(() => streamRef.current?.reconnectNow(), []);

  const value = useMemo<RfpLiveValue>(
    () => ({
      enabled,
      connection,
      offline,
      live,
      snapshotEpoch,
      markSeen,
      markAllSeen,
      noteOwnUpload,
      reconnectNow,
    }),
    [enabled, connection, offline, live, snapshotEpoch, markSeen, markAllSeen, noteOwnUpload, reconnectNow],
  );

  return (
    <RfpLiveContext.Provider value={value}>
      {children}
      {enabled ? <NewRfpToast /> : null}
    </RfpLiveContext.Provider>
  );
}
