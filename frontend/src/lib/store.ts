/**
 * Global Zustand store.
 *
 * All WebSocket events land here first; React components subscribe to
 * slices they care about via shallow selectors to minimise re-renders.
 */
import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";

import type {
  ActiveRescue,
  AmbulancePosition,
  DashboardStats,
  GreenWaveTrigger,
  TrafficSignal,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
export interface RescueRouteState {
  // Live map data
  signals: Map<string, TrafficSignal>;
  ambulances: Map<string, AmbulancePosition>;

  // Latest green-wave event (drives the Framer Motion animation)
  latestTrigger: GreenWaveTrigger | null;
  triggeredSignalIds: Set<string>;      // currently pulsing signal IDs

  // Dashboard KPIs
  stats: DashboardStats;
  activeRescues: ActiveRescue[];

  // WebSocket connection state
  wsConnected: boolean;
  wsLastPingAt: number | null;

  // ── Actions ──────────────────────────────────────────────────────────────
  upsertSignal: (signal: TrafficSignal) => void;
  upsertSignals: (signals: TrafficSignal[]) => void;
  upsertAmbulance: (pos: AmbulancePosition) => void;

  applyGreenWaveTrigger: (trigger: GreenWaveTrigger) => void;
  clearTriggeredSignals: () => void;

  setStats: (stats: DashboardStats) => void;
  setActiveRescues: (rescues: ActiveRescue[]) => void;

  setWsConnected: (connected: boolean) => void;
  pingWs: () => void;
}

// ---------------------------------------------------------------------------
// Default stats
// ---------------------------------------------------------------------------
const DEFAULT_STATS: DashboardStats = {
  active_rescues: 0,
  average_minutes_saved: 0,
  golden_hour_survival_rate: 0,
  signals_cleared_today: 0,
};

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useRescueStore = create<RescueRouteState>()(
  subscribeWithSelector((set, get) => ({
    // ── Initial state ────────────────────────────────────────────────────
    signals: new Map(),
    ambulances: new Map(),
    latestTrigger: null,
    triggeredSignalIds: new Set(),
    stats: DEFAULT_STATS,
    activeRescues: [],
    wsConnected: false,
    wsLastPingAt: null,

    // ── Mutations ────────────────────────────────────────────────────────
    upsertSignal: (signal) =>
      set((s) => {
        const next = new Map(s.signals);
        next.set(signal.id, signal);
        return { signals: next };
      }),

    upsertSignals: (signals) =>
      set((s) => {
        const next = new Map(s.signals);
        for (const sig of signals) next.set(sig.id, sig);
        return { signals: next };
      }),

    upsertAmbulance: (pos) =>
      set((s) => {
        const next = new Map(s.ambulances);
        next.set(pos.vehicle_id, pos);
        return { ambulances: next };
      }),

    applyGreenWaveTrigger: (trigger) => {
      // Update signals map with the GREEN-overridden states
      const updatedSignals = new Map(get().signals);
      for (const sig of trigger.signals) {
        updatedSignals.set(sig.id, sig);
      }
      // Track which signal IDs are actively pulsing
      const pulsing = new Set(trigger.signals.map((s) => s.id));

      set({
        latestTrigger: trigger,
        triggeredSignalIds: pulsing,
        signals: updatedSignals,
      });

      // Auto-clear the pulse animation after 4 seconds
      setTimeout(() => get().clearTriggeredSignals(), 4_000);
    },

    clearTriggeredSignals: () => set({ triggeredSignalIds: new Set() }),

    setStats: (stats) => set({ stats }),

    setActiveRescues: (rescues) => set({ activeRescues: rescues }),

    setWsConnected: (connected) => set({ wsConnected: connected }),

    pingWs: () => set({ wsLastPingAt: Date.now() }),
  }))
);

// ---------------------------------------------------------------------------
// Derived selectors (memoised outside components for referential stability)
// ---------------------------------------------------------------------------
export const selectSignalList = (s: RescueRouteState) =>
  Array.from(s.signals.values());

export const selectAmbulanceList = (s: RescueRouteState) =>
  Array.from(s.ambulances.values());

export const selectActiveRescueCount = (s: RescueRouteState) =>
  s.stats.active_rescues;
