/**
 * useRescueStream
 *
 * Connects to the backend WebSocket, parses incoming messages, and
 * dispatches them into the Zustand store.  Components never touch the
 * raw WS object — they only subscribe to store slices.
 *
 * Handles all four message types:
 *   GREEN_WAVE_TRIGGER  → store.applyGreenWaveTrigger
 *   AMBULANCE_UPDATE    → store.upsertAmbulance
 *   SIGNAL_UPDATE       → store.upsertSignal
 *   DASHBOARD_STATS     → store.setStats
 */
"use client";

import { useCallback, useEffect, useRef } from "react";
import useWebSocket, { ReadyState } from "react-use-websocket";

import { useRescueStore } from "@/lib/store";
import type { WSMessage } from "@/lib/types";

// ── Connection quality labels ──────────────────────────────────────────────
export const CONNECTION_LABELS: Record<ReadyState, string> = {
  [ReadyState.CONNECTING]: "Connecting…",
  [ReadyState.OPEN]:       "Live",
  [ReadyState.CLOSING]:    "Closing…",
  [ReadyState.CLOSED]:     "Disconnected",
  [ReadyState.UNINSTANTIATED]: "Uninitialised",
};

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws/frontend";

// Reconnect with capped exponential back-off
const RECONNECT_INTERVAL_MS = 2_000;
const MAX_RECONNECT_ATTEMPTS = 20;

// ---------------------------------------------------------------------------
export function useRescueStream() {
  const {
    applyGreenWaveTrigger,
    upsertAmbulance,
    upsertSignal,
    setStats,
    setWsConnected,
    pingWs,
  } = useRescueStore();

  const messageCountRef = useRef(0);

  // ── Message handler ───────────────────────────────────────────────────────
  const onMessage = useCallback(
    (event: MessageEvent<string>) => {
      pingWs();
      messageCountRef.current += 1;

      let data: WSMessage;
      try {
        data = JSON.parse(event.data) as WSMessage;
      } catch {
        console.warn("[useRescueStream] Failed to parse WS message", event.data);
        return;
      }

      switch (data.type) {
        case "GREEN_WAVE_TRIGGER":
          applyGreenWaveTrigger(data);
          break;

        case "AMBULANCE_UPDATE":
          upsertAmbulance(data.ambulance);
          break;

        case "SIGNAL_UPDATE":
          upsertSignal(data.signal);
          break;

        case "DASHBOARD_STATS":
          setStats({
            active_rescues:           data.active_rescues,
            average_minutes_saved:    data.average_minutes_saved,
            golden_hour_survival_rate: data.golden_hour_survival_rate,
            signals_cleared_today:    data.signals_cleared_today,
          });
          break;

        default:
          // unknown type — ignore silently in production
          break;
      }
    },
    [applyGreenWaveTrigger, upsertAmbulance, upsertSignal, setStats, pingWs]
  );

  // ── react-use-websocket ───────────────────────────────────────────────────
  const { readyState, sendJsonMessage } = useWebSocket(WS_URL, {
    onMessage,
    onOpen: () => {
      setWsConnected(true);
      console.info("[useRescueStream] WebSocket connected:", WS_URL);
    },
    onClose: () => {
      setWsConnected(false);
      console.info("[useRescueStream] WebSocket closed.");
    },
    onError: (e) => {
      console.error("[useRescueStream] WebSocket error:", e);
    },
    shouldReconnect: () => true,
    reconnectInterval: (attempt) =>
      Math.min(RECONNECT_INTERVAL_MS * 2 ** attempt, 30_000),
    reconnectAttempts: MAX_RECONNECT_ATTEMPTS,
    share: true,          // share a single socket across all hook instances
    heartbeat: {
      message: JSON.stringify({ type: "PING" }),
      returnMessage: "PONG",
      timeout: 30_000,
      interval: 25_000,
    },
  });

  // Keep WS connected state in sync with readyState
  useEffect(() => {
    setWsConnected(readyState === ReadyState.OPEN);
  }, [readyState, setWsConnected]);

  return {
    readyState,
    connectionLabel: CONNECTION_LABELS[readyState],
    isConnected: readyState === ReadyState.OPEN,
    messageCount: messageCountRef.current,
    /** Send a raw JSON payload to the backend (for manual overrides etc.) */
    sendMessage: sendJsonMessage,
  };
}
