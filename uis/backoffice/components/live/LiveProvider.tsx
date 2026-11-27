"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { useSession } from "@/components/auth/AuthProvider";
import { OperationsLiveContext, type OperationsLiveValue } from "@/components/live/OperationsLiveContext";
import { NewRfpToast } from "@/components/rfp/NewRfpToast";
import { RfpLiveContext, type RfpLiveValue } from "@/components/rfp/RfpLiveContext";
import { LiveStreamConnection, type ConnectionState } from "@/lib/live-stream";
import {
  ALERT_ACKNOWLEDGED,
  EMPTY_ALERTS,
  acknowledgeAlert,
  applyAlertEvent,
  applyAlertSnapshot,
  describeAlertsError,
  dismissResumed as dismissResumedIn,
  listOperationalAlerts,
  type AlertUpdate,
  type AlertsState,
} from "@/lib/operations-live";
import {
  EMPTY_LIVE_STATE,
  markAllSeen as markAllSeenIn,
  markSeen as markSeenIn,
  noteOwnUpload as noteOwnUploadIn,
  recordArrival,
  titleWithCount,
  type LiveState,
} from "@/lib/rfp-live";

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
 * One live connection per signed-in staff tab, for the whole backoffice (Milestone 10), and what
 * arrives on it:
 * - new RFP tickets: a notification on any page, a count on "RFP intake", the tab title while the
 *   tab is in the background, and a new row on `/rfp` (`RfpLiveContext`);
 * - operational inactivity alerts: the banner under the header, a count on "Alerts", and the
 *   `/operations` page (`OperationsLiveContext`). Their snapshot (`GET /operations/alerts`) is
 *   read whenever a list may be stale (a first connection, a resync, the recovery after a
 *   failure); live events that arrive meanwhile are applied on top of it.
 *
 * The connection (`lib/live-stream.ts`) reconnects by itself with progressive backoff and resumes
 * with `Last-Event-ID`; this provider adds the browser's hints (back online, tab visible again)
 * so it does not wait out a backoff it no longer needs.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const { user } = useSession();
  const enabled = user?.role === "admin" || user?.role === "manager";
  const [connection, setConnection] = useState<ConnectionState>({ kind: "idle" });
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE_STATE);
  const [alerts, setAlerts] = useState<AlertsState>(EMPTY_ALERTS);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [snapshotEpoch, setSnapshotEpoch] = useState(0);
  const streamRef = useRef<LiveStreamConnection | null>(null);
  // Alert events received while a snapshot is being read: re-applied on top of it.
  const pendingAlerts = useRef<AlertUpdate[] | null>(null);
  const offline = useSyncExternalStore(subscribeToNetwork, isOffline, never);
  const hidden = useSyncExternalStore(subscribeToVisibility, isHidden, never);

  useEffect(() => {
    if (!enabled) return;
    // Events before `rfp_stream_ready` on a resumed connection are replays: they were
    // registered while this tab was disconnected.
    let caughtUp = false;
    // The connection failed since it was last live (not the server's routine end of a stream).
    let recovering = false;
    const stream = new LiveStreamConnection({
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
        if (event.type === "alert") {
          const update = { event: event.event, alert: event.alert };
          pendingAlerts.current?.push(update);
          setAlerts((current) => applyAlertEvent(current, update));
          return;
        }
        // The snapshots are read again once when they may be stale: a first connection
        // (anything before it), a resync (a gap the replay cannot cover), or the first ready
        // after a failure (missed events came by replay, but what is listed may have moved on
        // meanwhile, and a read may have failed during the outage).
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
      setAlerts(EMPTY_ALERTS);
    };
  }, [enabled, user?.email]);

  // The open alerts: read after every stale point, merged with what arrived meanwhile.
  useEffect(() => {
    if (!enabled || snapshotEpoch === 0) return;
    let active = true;
    pendingAlerts.current = [];
    listOperationalAlerts("open")
      .then((snapshot) => {
        if (!active) return;
        const since = pendingAlerts.current ?? [];
        pendingAlerts.current = null;
        setAlerts((current) => applyAlertSnapshot(current, snapshot, since));
        setAlertsError(null);
      })
      .catch((failure: unknown) => {
        if (!active) return;
        pendingAlerts.current = null;
        setAlertsError(describeAlertsError(failure));
      });
    return () => {
      active = false;
    };
  }, [enabled, snapshotEpoch]);

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
  const acknowledge = useCallback(async (alertId: string) => {
    const alert = await acknowledgeAlert(alertId);
    // Shown at once; the stream's own `operational_alert_acknowledged` changes nothing more.
    setAlerts((current) => applyAlertEvent(current, { event: ALERT_ACKNOWLEDGED, alert }));
    return alert;
  }, []);
  const dismissResumed = useCallback(
    (alertId: string) => setAlerts((current) => dismissResumedIn(current, alertId)),
    [],
  );

  const rfpValue = useMemo<RfpLiveValue>(
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
  const operationsValue = useMemo<OperationsLiveValue>(
    () => ({ enabled, alerts, alertsError, acknowledge, dismissResumed }),
    [enabled, alerts, alertsError, acknowledge, dismissResumed],
  );

  return (
    <RfpLiveContext.Provider value={rfpValue}>
      <OperationsLiveContext.Provider value={operationsValue}>
        {children}
        {enabled ? <NewRfpToast /> : null}
      </OperationsLiveContext.Provider>
    </RfpLiveContext.Provider>
  );
}
