"use client";

/**
 * Dashboard — Command-centre sidebar panel.
 *
 * Widgets:
 *   1. Active Rescues      — live count badge + rescue list
 *   2. Average Minutes Saved — animated counter
 *   3. Golden Hour Survival — radial-style progress ring
 *   4. Signals Cleared Today
 *   5. WS connection status pill
 *   6. Latest trigger event log
 */

import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Heart,
  Radio,
  Shield,
  Siren,
  Timer,
  Zap,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";


import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Separator } from "@/components/ui/separator";
import { useRescueStream } from "@/hooks/useRescueStream";
import { cn, formatETA } from "@/lib/utils";
import { selectAmbulanceList, useRescueStore } from "@/lib/store";

// ---------------------------------------------------------------------------
// Animated counter hook
// ---------------------------------------------------------------------------
function useAnimatedNumber(target: number, duration = 800) {
  const [display, setDisplay] = useState(target);
  const raf = useRef<number>(0);

  useEffect(() => {
    const start = display;
    const diff = target - start;
    const startTime = performance.now();

    const tick = (now: number) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease-out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplay(start + diff * eased);
      if (progress < 1) raf.current = requestAnimationFrame(tick);
    };

    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  return display;
}

// ---------------------------------------------------------------------------
// Survival Ring (SVG-based, no canvas needed)
// ---------------------------------------------------------------------------
function SurvivalRing({ value }: { value: number }) {
  const r = 36;
  const circ = 2 * Math.PI * r;
  const filled = circ * (value / 100);

  return (
    <svg width="96" height="96" viewBox="0 0 96 96" className="drop-shadow-lg">
      {/* Track */}
      <circle
        cx="48" cy="48" r={r}
        fill="none"
        stroke="hsl(217 32% 17%)"
        strokeWidth="8"
      />
      {/* Fill */}
      <motion.circle
        cx="48" cy="48" r={r}
        fill="none"
        stroke="#00E676"
        strokeWidth="8"
        strokeLinecap="round"
        strokeDasharray={circ}
        strokeDashoffset={circ - filled}
        transform="rotate(-90 48 48)"
        initial={{ strokeDashoffset: circ }}
        animate={{ strokeDashoffset: circ - filled }}
        transition={{ duration: 1.2, ease: "easeOut" }}
      />
      {/* Label */}
      <text
        x="48" y="44"
        textAnchor="middle"
        fontSize="16"
        fontWeight="700"
        fill="#00E676"
        fontFamily="monospace"
      >
        {Math.round(value)}%
      </text>
      <text
        x="48" y="60"
        textAnchor="middle"
        fontSize="9"
        fill="hsl(215 20% 55%)"
        fontFamily="monospace"
      >
        SURVIVAL
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Dispatch row — driven from live ambulance store
// ---------------------------------------------------------------------------
const MISSION_META = {
  TO_HOSPITAL: { label: "To Hospital", color: "#2979FF", dot: "bg-blue-500"   },
  TO_PATIENT:  { label: "To Patient",  color: "#FF6D00", dot: "bg-orange-500" },
  DEMO:        { label: "Demo",        color: "#00BFA5", dot: "bg-teal-400"   },
} as const;

function DispatchRow({ amb }: { amb: import("@/lib/types").AmbulancePosition }) {
  const isDemo   = amb.vehicle_id === "BLR-AMB-DEMO";
  const meta     = isDemo
    ? MISSION_META.DEMO
    : amb.mission === "TO_PATIENT"
    ? MISSION_META.TO_PATIENT
    : MISSION_META.TO_HOSPITAL;

  const etaMin   = amb.eta_seconds != null ? Math.floor(amb.eta_seconds / 60) : null;
  const etaSec   = amb.eta_seconds != null ? amb.eta_seconds % 60 : null;

  return (
    <motion.div
      initial={{ opacity: 0, x: -12 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 12 }}
      className="flex items-start gap-2 py-2 border-b border-border/40 last:border-0"
    >
      {/* Coloured dot + siren */}
      <span className={`mt-1 w-2 h-2 rounded-full shrink-0 ${meta.dot} animate-pulse`} />

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 mb-0.5">
          <p className="text-xs font-mono font-bold text-foreground truncate">
            {amb.vehicle_id}
          </p>
          <span
            className="text-[9px] font-mono px-1 rounded"
            style={{ background: meta.color + "22", color: meta.color }}
          >
            {meta.label}
          </span>
        </div>
        <p className="text-[10px] text-muted-foreground truncate">
          {amb.origin ?? "—"} → {amb.destination ?? "—"}
        </p>
        <p className="text-[10px] text-muted-foreground">
          {Math.round(amb.speed_kmh ?? 0)} km/h
        </p>
      </div>

      {/* ETA */}
      {etaMin !== null && (
        <div className="text-right shrink-0">
          <p className="text-xs font-mono tabular-nums" style={{ color: meta.color }}>
            {etaMin}:{String(etaSec).padStart(2, "0")}
          </p>
          <p className="text-[9px] text-muted-foreground">ETA</p>
        </div>
      )}
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Event log item
// ---------------------------------------------------------------------------
interface LogEntry {
  id: string;
  ts: Date;
  label: string;
  count: number;
}

// ---------------------------------------------------------------------------
// Green Wave junction queue  (with live 1-second countdown)
// ---------------------------------------------------------------------------
function GreenWaveQueue() {
  const latestTrigger = useRescueStore((s) => s.latestTrigger);
  const triggeredIds  = useRescueStore((s) => s.triggeredSignalIds);

  // Seconds elapsed since the last GREEN_WAVE_TRIGGER arrived
  const [elapsed, setElapsed] = useState(0);
  const triggerRef = useRef(latestTrigger);
  useEffect(() => {
    setElapsed(0);
    triggerRef.current = latestTrigger;
  }, [latestTrigger]);
  useEffect(() => {
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, []);

  if (!latestTrigger) {
    return (
      <div>
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2 flex items-center gap-1">
          <Zap className="w-3 h-3" /> Green Wave Queue
        </p>
        <p className="text-xs text-muted-foreground py-1 pl-1 font-mono">
          Waiting for ambulance…
        </p>
      </div>
    );
  }

  const junctions = latestTrigger.signals.slice().sort(
    (a, b) => (a.eta_seconds ?? 999) - (b.eta_seconds ?? 999)
  );

  return (
    <div>
      <p className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2 flex items-center gap-1">
        <Zap className="w-3 h-3 text-emergency-green" /> Green Wave Queue
        <span className="ml-auto text-emergency-green font-mono">
          {latestTrigger.ambulance.vehicle_id}
        </span>
      </p>
      <div className="space-y-1.5">
        {junctions.map((sig) => {
          const active = triggeredIds.has(sig.id);
          // Subtract real seconds elapsed since last trigger for a live countdown
          const eta = Math.max(0, (sig.eta_seconds ?? 0) - elapsed);
          const pct = Math.max(0, Math.min(100, 100 - (eta / 30) * 100));
          return (
            <motion.div
              key={sig.id}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              className={`rounded-md border px-2 py-1.5 ${
                active
                  ? "border-emergency-green/60 bg-emergency-green/5"
                  : "border-border"
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-[11px] font-mono truncate max-w-[140px]">
                  {sig.junction_name}
                </span>
                <div className="flex items-center gap-1">
                  {active && (
                    <span className="w-1.5 h-1.5 rounded-full bg-emergency-green animate-pulse" />
                  )}
                  <span className={`text-[11px] font-mono tabular-nums ${active ? "text-emergency-green" : eta === 0 ? "text-emergency-red" : "text-muted-foreground"}`}>
                    {eta}s
                  </span>
                </div>
              </div>
              <div className="h-1 rounded-full bg-border overflow-hidden">
                <motion.div
                  className={`h-full rounded-full ${active ? "bg-emergency-green" : eta === 0 ? "bg-emergency-red/60" : "bg-muted-foreground/40"}`}
                  animate={{ width: `${pct}%` }}
                  transition={{ duration: 0.4 }}
                />
              </div>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live clock (updates every second)
// ---------------------------------------------------------------------------
function LiveClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="text-[10px] font-mono text-muted-foreground tabular-nums">
      {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
export default function Dashboard() {
  const { isConnected, connectionLabel } = useRescueStream();

  const stats       = useRescueStore((s) => s.stats);
  const ambulances  = useRescueStore(useShallow(selectAmbulanceList));
  const latestTrigger = useRescueStore((s) => s.latestTrigger);
  const setStats    = useRescueStore((s) => s.setStats);

  // REST poll every 2 s as a live-stats fallback (proxied via Next.js → FastAPI)
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch("/api/stats");
        if (res.ok) {
          const data = await res.json();
          setStats(data);
        }
      } catch { /* ignore */ }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, [setStats]);

  // Animated metric values
  const animRescues   = useAnimatedNumber(stats.active_rescues);
  const animMinutes   = useAnimatedNumber(stats.average_minutes_saved);
  const animSurvival  = useAnimatedNumber(stats.golden_hour_survival_rate);
  const animCleared   = useAnimatedNumber(stats.signals_cleared_today);

  // Event log (last 6 trigger events)
  const [eventLog, setEventLog] = useState<LogEntry[]>([]);
  useEffect(() => {
    if (!latestTrigger) return;
    const entry: LogEntry = {
      id: `${Date.now()}`,
      ts: new Date(),
      label: `Green wave — ${latestTrigger.ambulance.vehicle_id}`,
      count: latestTrigger.signals.length,
    };
    setEventLog((prev) => [entry, ...prev].slice(0, 6));
  }, [latestTrigger]);

  return (
    <aside className="w-80 h-full flex flex-col gap-3 p-3 overflow-y-auto bg-background/95 backdrop-blur-sm border-l border-border">

      {/* ── Header ───────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield className="w-5 h-5 text-primary" />
          <span className="font-bold text-sm tracking-widest uppercase text-foreground">
            RescueRoute
          </span>
        </div>
        <div className="flex items-center gap-2">
          <LiveClock />
          <Badge
            variant={isConnected ? "success" : "destructive"}
            className="gap-1 font-mono text-[10px]"
          >
            <Radio className="w-2.5 h-2.5" />
            {connectionLabel}
          </Badge>
        </div>
      </div>

      <Separator />

      {/* ── Row 1: Active Rescues + Signals Cleared ───────────────────── */}
      <div className="grid grid-cols-2 gap-2">
        <Card className="glass-card">
          <CardHeader className="pb-1 pt-3 px-3">
            <CardTitle className="flex items-center gap-1.5">
              <Activity className="w-3 h-3" />
              Active
            </CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3">
            <motion.div
              key={Math.round(animRescues)}
              initial={{ scale: 1.2, color: "#00E676" }}
              animate={{ scale: 1, color: "#e2e8f0" }}
              className="text-3xl font-mono font-bold"
            >
              {Math.round(animRescues)}
            </motion.div>
            <p className="text-[10px] text-muted-foreground mt-0.5">rescues live</p>
          </CardContent>
        </Card>

        <Card className="glass-card">
          <CardHeader className="pb-1 pt-3 px-3">
            <CardTitle className="flex items-center gap-1.5">
              <Zap className="w-3 h-3" />
              Cleared
            </CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3">
            <motion.div
              key={Math.round(animCleared)}
              initial={{ scale: 1.15, color: "#00E676" }}
              animate={{ scale: 1, color: "#e2e8f0" }}
              className="text-3xl font-mono font-bold"
            >
              {Math.round(animCleared)}
            </motion.div>
            <p className="text-[10px] text-muted-foreground mt-0.5">signals today</p>
          </CardContent>
        </Card>
      </div>

      {/* ── Row 2: Minutes Saved + Golden Hour Survival ───────────────── */}
      <Card className="glass-card">
        <CardHeader className="pb-2 pt-3 px-3">
          <CardTitle className="flex items-center gap-1.5">
            <Timer className="w-3 h-3" />
            Average Minutes Saved
          </CardTitle>
        </CardHeader>
        <CardContent className="px-3 pb-3">
          <div className="flex items-end gap-2 mb-2">
            <motion.span
              key={Math.round(animMinutes * 10)}
              initial={{ y: -6, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              className="text-4xl font-mono font-bold text-emergency-green"
            >
              {animMinutes.toFixed(1)}
            </motion.span>
            <span className="text-muted-foreground text-sm mb-1 font-mono">min / trip</span>
          </div>
          {/* Progress bar — 10 min saved = 100% full */}
          <Progress
            value={Math.min((stats.average_minutes_saved / 10) * 100, 100)}
            className="h-1.5"
            indicatorClassName="bg-emergency-green"
          />
          <p className="text-[10px] text-muted-foreground mt-1">
            Target: 8 min / trip
          </p>
        </CardContent>
      </Card>

      {/* ── Row 3: Golden Hour Survival ───────────────────────────────── */}
      <Card className="glass-card">
        <CardHeader className="pb-2 pt-3 px-3">
          <CardTitle className="flex items-center gap-1.5">
            <Heart className="w-3 h-3 text-emergency-red" />
            Golden Hour Survival
          </CardTitle>
        </CardHeader>
        <CardContent className="px-3 pb-3 flex items-center gap-4">
          <SurvivalRing value={animSurvival} />
          <div className="flex-1 space-y-2">
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Rate</p>
              <p className="font-mono text-lg font-bold text-emergency-green">
                {animSurvival.toFixed(1)}%
              </p>
            </div>
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-wider">National avg</p>
              <p className="font-mono text-sm text-muted-foreground">58.0%</p>
            </div>
            <Progress
              value={animSurvival}
              className="h-1"
              indicatorClassName="bg-emergency-green"
            />
          </div>
        </CardContent>
      </Card>

      <Separator />

      {/* ── Live dispatches (from ambulance store) ───────────────────── */}
      <div>
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1 flex items-center gap-1">
          <Clock className="w-3 h-3" /> Live dispatches
          {ambulances.length > 0 && (
            <span className="ml-auto font-mono text-emergency-green">{ambulances.length} active</span>
          )}
        </p>
        <div className="min-h-[40px]">
          <AnimatePresence initial={false}>
            {ambulances.length === 0 ? (
              <motion.p
                key="empty"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="text-xs text-muted-foreground py-2 pl-1"
              >
                No active dispatches
              </motion.p>
            ) : (
              ambulances.map((amb) => (
                <DispatchRow key={amb.vehicle_id} amb={amb} />
              ))
            )}
          </AnimatePresence>
        </div>
      </div>

      <Separator />

      {/* ── Green Wave Junction Queue ─────────────────────────────────── */}
      <GreenWaveQueue />

      <Separator />

      {/* ── Event log ─────────────────────────────────────────────────── */}
      <div>
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1 flex items-center gap-1">
          <AlertTriangle className="w-3 h-3" /> Event log
        </p>
        <div className="space-y-1">
          <AnimatePresence initial={false}>
            {eventLog.length === 0 ? (
              <motion.p
                key="no-events"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="text-xs text-muted-foreground py-1 pl-1"
              >
                Waiting for triggers…
              </motion.p>
            ) : (
              eventLog.map((entry) => (
                <motion.div
                  key={entry.id}
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex items-center gap-2 py-1"
                >
                  <CheckCircle2 className="w-3 h-3 text-emergency-green shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[11px] font-mono text-foreground truncate">{entry.label}</p>
                    <p className="text-[9px] text-muted-foreground">
                      {entry.count} signals cleared •{" "}
                      {entry.ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                    </p>
                  </div>
                </motion.div>
              ))
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* ── Footer ───────────────────────────────────────────────────── */}
      <div className="mt-auto pt-2 border-t border-border">
        <p className="text-[9px] text-muted-foreground font-mono text-center">
          Bengaluru Emergency Response • v0.1.0
        </p>
      </div>
    </aside>
  );
}
