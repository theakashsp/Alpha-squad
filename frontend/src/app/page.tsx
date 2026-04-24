"use client";

/**
 * Root page — full-screen command centre layout.
 *
 * ┌──────────────────────────────────────────┬─────────────┐
 * │  Header bar (logo + status + seed btn)   │             │
 * ├──────────────────────────────────────────┤  Dashboard  │
 * │                                          │  sidebar    │
 * │        Mappls Map (dark)                 │  (KPI       │
 * │        + signal markers                  │   widgets)  │
 * │        + ambulance tracker               │             │
 * │        + green-wave pulse rings          │             │
 * └──────────────────────────────────────────┴─────────────┘
 *
 * The WebSocket connection is established here so it lives for the
 * lifetime of the page (not per-component).
 */

import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Layers, Play, RefreshCw, Siren, Square } from "lucide-react";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import DispatchPanel from "@/components/DispatchPanel";
import LoginScreen from "@/components/LoginScreen";
import UnitProfilePanel from "@/components/UnitProfilePanel";
import { useAuth } from "@/hooks/useAuth";
import { useRescueStream } from "@/hooks/useRescueStream";
import { useRescueStore } from "@/lib/store";

// Load the map client-side only (Leaflet uses window / document)
const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => (
    <div className="flex-1 flex items-center justify-center bg-[#0d1117]">
      <div className="flex flex-col items-center gap-3">
        <div className="w-8 h-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
        <span className="text-muted-foreground text-sm font-mono">
          Loading map…
        </span>
      </div>
    </div>
  ),
});

const Dashboard = dynamic(() => import("@/components/Dashboard"), { ssr: false });

// ---------------------------------------------------------------------------
// Emergency alert banner (shown when a GREEN_WAVE_TRIGGER fires)
// ---------------------------------------------------------------------------
function EmergencyBanner() {
  const latestTrigger = useRescueStore((s) => s.latestTrigger);
  const [visible, setVisible] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!latestTrigger) return;
    const count = latestTrigger.signals.filter((s) => s.emergency_override).length;
    setMessage(
      `🚑 ${latestTrigger.ambulance.vehicle_id} — Green Wave active · ${count} signal${count !== 1 ? "s" : ""} cleared`
    );
    setVisible(true);
    const t = setTimeout(() => setVisible(false), 5_000);
    return () => clearTimeout(t);
  }, [latestTrigger]);

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key="banner"
          initial={{ y: -48, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -48, opacity: 0 }}
          transition={{ type: "spring", stiffness: 260, damping: 20 }}
          className="absolute top-12 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-lg
                     bg-emergency-green/10 border border-emergency-green/40 backdrop-blur-sm
                     flex items-center gap-2 text-emergency-green text-xs font-mono shadow-xl glow-green"
        >
          <Siren className="w-3.5 h-3.5 animate-pulse" />
          {message}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// Module-level flag — prevents StrictMode double-mount from double-seeding
let _autoSeedFired = false;

// ---------------------------------------------------------------------------
// Seed button — calls POST /api/signals/seed on first load
// ---------------------------------------------------------------------------
function SeedButton() {
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const { upsertSignals } = useRescueStore();

  const seed = useCallback(async () => {
    setStatus("loading");
    try {
      const seedRes = await fetch("/api/signals/seed", { method: "POST" });
      if (!seedRes.ok) throw new Error(await seedRes.text());

      // Fetch full signal list and hydrate the Zustand store
      const listRes = await fetch("/api/signals");
      if (listRes.ok) {
        const signals = await listRes.json();
        upsertSignals(signals);
      }
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }, [upsertSignals]);

  // Auto-seed exactly once per page load (guard against StrictMode double-mount)
  useEffect(() => {
    if (_autoSeedFired) return;
    _autoSeedFired = true;
    seed();
  }, [seed]);

  return (
    <button
      onClick={seed}
      disabled={status === "loading"}
      title="Re-seed junction fixtures"
      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-mono border transition-colors
        ${status === "error"
          ? "border-emergency-red/40 text-emergency-red"
          : "border-border text-muted-foreground hover:border-primary hover:text-primary"
        }`}
    >
      <RefreshCw className={`w-3 h-3 ${status === "loading" ? "animate-spin" : ""}`} />
      {status === "loading" ? "Seeding…" : status === "done" ? "Seeded ✓" : status === "error" ? "Retry" : "Seed map"}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Demo button — seeds junctions then fires the in-process simulation
// ---------------------------------------------------------------------------
function DemoButton() {
  const [status, setStatus] = useState<"idle" | "starting" | "running" | "error">("idle");

  const startDemo = useCallback(async () => {
    setStatus("starting");
    try {
      await fetch("/api/signals/seed", { method: "POST" });
      const res = await fetch("/api/demo/start", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      setStatus("running");
    } catch {
      setStatus("error");
      setTimeout(() => setStatus("idle"), 3000);
    }
  }, []);

  const stopDemo = useCallback(async () => {
    await fetch("/api/demo/stop", { method: "POST" }).catch(() => {});
    setStatus("idle");
  }, []);

  if (status === "running") {
    return (
      <button
        onClick={stopDemo}
        className="flex items-center gap-1.5 px-3 py-1 rounded-md text-[11px] font-mono
                   border border-emergency-red/50 text-emergency-red
                   hover:border-emergency-red hover:bg-emergency-red/10 transition-colors"
      >
        <Square className="w-3 h-3" />
        Stop Demo
      </button>
    );
  }

  return (
    <button
      onClick={startDemo}
      disabled={status === "starting"}
      className="flex items-center gap-1.5 px-3 py-1 rounded-md text-[11px] font-mono
                 border border-emergency-green/50 text-emergency-green
                 hover:border-emergency-green hover:bg-emergency-green/10
                 disabled:opacity-50 transition-colors"
    >
      <Play className={`w-3 h-3 ${status === "starting" ? "animate-pulse" : ""}`} />
      {status === "starting" ? "Starting…" : status === "error" ? "Retry" : "▶ Start Demo"}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------
function Header({
  onDispatch,
  onProfile,
  onLogout,
  onSwitchAccount,
  operatorName,
}: {
  onDispatch: () => void;
  onProfile: () => void;
  onLogout: () => void;
  onSwitchAccount: () => void;
  operatorName: string;
}) {
  const { isConnected, connectionLabel } = useRescueStream();
  const signalCount = useRescueStore((s) => s.signals.size);
  const ambulanceCount = useRescueStore((s) => s.ambulances.size);

  return (
    <header className="h-10 flex items-center justify-between px-4 border-b border-border bg-background/90 backdrop-blur-sm z-20 relative">
      {/* Brand */}
      <div className="flex items-center gap-2.5">
        <span className="text-lg">🚑</span>
        <span className="font-bold text-sm tracking-wider text-foreground uppercase">
          RescueRoute
        </span>
        <span className="text-muted-foreground text-[10px] font-mono hidden sm:block">
          Rolling Green Wave · Bengaluru
        </span>
      </div>

      {/* Centre info chips */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-mono text-muted-foreground flex items-center gap-1">
          <Layers className="w-3 h-3" />
          {signalCount} signals
        </span>
        <span className="text-[10px] font-mono text-muted-foreground flex items-center gap-1">
          <Siren className="w-3 h-3" />
          {ambulanceCount} ambulances
        </span>
      </div>

      {/* Right actions */}
      <div className="flex items-center gap-2">
        {/* Profile / settings button */}
        <button
          onClick={onProfile}
          title="Unit Profile & Settings"
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-mono
                     border border-slate-600 text-slate-400
                     hover:border-cyan-500/60 hover:text-cyan-400 hover:bg-cyan-500/10
                     transition-colors"
        >
          🪪 Profile
        </button>
        {/* Dispatch button — primary CTA */}
        <button
          onClick={onDispatch}
          className="flex items-center gap-1.5 px-3 py-1 rounded-md text-[11px] font-mono font-bold
                     border border-red-500/60 text-red-400 bg-red-500/10
                     hover:bg-red-500/20 hover:border-red-400 hover:text-red-300
                     transition-colors"
        >
          <Siren className="w-3 h-3" />
          Dispatch
        </button>
        <DemoButton />
        <SeedButton />
        <Badge
          variant={isConnected ? "success" : "destructive"}
          className="text-[10px] font-mono gap-1"
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              isConnected ? "bg-emergency-green animate-pulse" : "bg-emergency-red"
            }`}
          />
          {connectionLabel}
        </Badge>
        {!isConnected && (
          <AlertTriangle className="w-3.5 h-3.5 text-emergency-amber animate-pulse" />
        )}
        {/* Operator chip + session buttons */}
        {operatorName && (
          <div className="flex items-center gap-1.5 pl-1 border-l border-border">
            <span className="text-[10px] text-muted-foreground font-mono hidden md:block truncate max-w-[100px]">
              👤 {operatorName}
            </span>
            <button
              onClick={onSwitchAccount}
              title="Switch to a different unit"
              className="text-[10px] font-mono px-2 py-1 rounded border border-slate-700 text-slate-500 hover:border-cyan-500/50 hover:text-cyan-400 transition-colors"
            >
              Switch
            </button>
            <button
              onClick={onLogout}
              title="End shift / log out"
              className="text-[10px] font-mono px-2 py-1 rounded border border-slate-700 text-slate-500 hover:border-red-500/50 hover:text-red-400 transition-colors"
            >
              End Shift
            </button>
          </div>
        )}
      </div>
    </header>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export default function HomePage() {
  const { session, hydrated, login, logout, switchUnit, activeUnitId } = useAuth();

  // Boot the WebSocket connection at the top level (always, so it's ready on login)
  useRescueStream();

  const [showDispatch, setShowDispatch] = useState(false);
  const [showProfile, setShowProfile]   = useState(false);

  // While sessionStorage is being read, render nothing to avoid flash
  if (!hydrated) return null;

  // Not logged in → show login screen, passing auth callbacks from this hook instance
  if (!session) {
    return (
      <LoginScreen
        activeUnitId={activeUnitId}
        onLogin={login}
        onSwitchUnit={switchUnit}
      />
    );
  }

  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-background scanlines">
      <Header
        onDispatch={() => setShowDispatch(true)}
        onProfile={() => setShowProfile(true)}
        onLogout={logout}
        onSwitchAccount={logout}
        operatorName={session.name}
      />

      {/* Main area */}
      <main className="flex-1 flex overflow-hidden relative">
        {/* Map fills remaining space */}
        <div className="flex-1 relative">
          <MapView />
          <EmergencyBanner />
        </div>

        {/* Dashboard sidebar */}
        <Dashboard />
      </main>

      {/* Dispatch modal */}
      {showDispatch && (
        <DispatchPanel onClose={() => setShowDispatch(false)} />
      )}

      {/* Profile / settings modal */}
      {showProfile && (
        <UnitProfilePanel onClose={() => setShowProfile(false)} />
      )}
    </div>
  );
}
