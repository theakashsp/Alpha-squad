"use client";

/**
 * MapView — Leaflet + OpenStreetMap (CartoDB Dark) interactive map.
 *
 * Responsibilities:
 *   • Render dark-mode OSM tiles (no API key required)
 *   • Keep traffic-signal markers in sync with the Zustand store
 *   • Animate every signal through a realistic RED/GREEN/AMBER cycle
 *   • Lock emergency-overridden signals to GREEN with a pulse ring
 *   • Keep ambulance markers in sync with the Zustand store
 *   • Draw the route polyline on GREEN_WAVE_TRIGGER
 */

import { AnimatePresence, motion } from "framer-motion";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CircleMarker,
  MapContainer,
  Marker,
  Polyline,
  TileLayer,
  Tooltip,
  useMap,
  useMapEvents,
} from "react-leaflet";
import { useShallow } from "zustand/react/shallow";

import {
  selectAmbulanceList,
  selectSignalList,
  useRescueStore,
} from "@/lib/store";
import type { AmbulancePosition, Hospital, MissionType, TrafficSignal } from "@/lib/types";

// Incident type display metadata
const INCIDENT_META: Record<string, { icon: string; label: string; color: string }> = {
  CARDIAC_ARREST: { icon: "🫀", label: "Cardiac Arrest", color: "#FF4444" },
  ROAD_ACCIDENT:  { icon: "🚗", label: "Road Accident",  color: "#FF8C00" },
  STROKE:         { icon: "🧠", label: "Stroke",         color: "#9C27B0" },
  TRAUMA:         { icon: "🩹", label: "Trauma",         color: "#F44336" },
};

// ── Constants ──────────────────────────────────────────────────────────────
const BLR_CENTER: [number, number] = [12.9716, 77.5946];
const DEFAULT_ZOOM = 13;

// CartoDB dark-matter tiles — free, no API key
const TILE_URL =
  "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
const TILE_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>';

// Signal colour palette
const SIG_COLOUR: Record<string, string> = {
  GREEN: "#00E676",
  RED:   "#FF2D2D",
  AMBER: "#FFB300",
};

// ── Realistic Bengaluru traffic-light cycle ─────────────────────────────────
// Based on BBMP/BCTP standard urban junction timing:
//   RED   50 s  — cross-traffic flows
//   GREEN 35 s  — your direction flows
//   AMBER  5 s  — clear the junction
//   TOTAL 90 s  full cycle
const CYCLE_RED   = 50;
const CYCLE_GREEN = 35;
const CYCLE_AMBER = 5;
const CYCLE_TOTAL = CYCLE_RED + CYCLE_GREEN + CYCLE_AMBER; // 90 s

/**
 * Deterministic integer hash of a signal ID.
 * Each junction starts at a different point in the 90-second cycle so
 * nearby signals are naturally offset — just like real coordinated junctions.
 */
function hashId(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = (h * 0x01000193) >>> 0;
  }
  return h;
}

interface CycleInfo {
  status: "RED" | "GREEN" | "AMBER";
  secondsRemaining: number; // seconds until this phase ends
  emergency: boolean;
}

/**
 * Compute the current phase and seconds-remaining for a signal.
 * Emergency-overridden signals stay GREEN until the ambulance passes —
 * after which the normal 90-second cycle resumes exactly where it would
 * naturally be (no reset, no stutter).
 */
function getCycleInfo(sig: TrafficSignal, tick: number, isTriggered: boolean): CycleInfo {
  if (sig.emergency_override || isTriggered) {
    return { status: "GREEN", secondsRemaining: 0, emergency: true };
  }
  const phase = hashId(sig.id) % CYCLE_TOTAL;
  const t = (tick + phase) % CYCLE_TOTAL;
  if (t < CYCLE_RED) {
    return { status: "RED",   secondsRemaining: CYCLE_RED - t,                    emergency: false };
  }
  if (t < CYCLE_RED + CYCLE_GREEN) {
    return { status: "GREEN", secondsRemaining: CYCLE_RED + CYCLE_GREEN - t,      emergency: false };
  }
  return   { status: "AMBER", secondsRemaining: CYCLE_TOTAL - t,                  emergency: false };
}

// ── Ambulance icons — colour-coded by mission ───────────────────────────────
const MISSION_PALETTE = {
  TO_HOSPITAL: { bg: "#2979FF", border: "#5c9fff", glow: "#2979FF88" }, // blue
  TO_PATIENT:  { bg: "#FF6D00", border: "#ffa040", glow: "#FF6D0088" }, // orange
  DEMO:        { bg: "#00BFA5", border: "#4dd0c4", glow: "#00BFA588" }, // teal
};
const ROUTE_COLOUR = {
  TO_HOSPITAL: "#2979FF",
  TO_PATIENT:  "#FF6D00",
  DEMO:        "#00BFA5",
};

function makeAmbIcon(palette: { bg: string; border: string; glow: string }) {
  return L.divIcon({
    className: "",
    html: `<div style="
      width:36px;height:36px;border-radius:8px;
      background:${palette.bg};display:flex;align-items:center;
      justify-content:center;font-size:20px;
      box-shadow:0 0 14px ${palette.glow};
      border:2px solid ${palette.border};
    ">🚑</div>`,
    iconSize: [36, 36],
    iconAnchor: [18, 18],
  });
}

const AMB_ICON_HOSPITAL = makeAmbIcon(MISSION_PALETTE.TO_HOSPITAL);
const AMB_ICON_PATIENT  = makeAmbIcon(MISSION_PALETTE.TO_PATIENT);
const AMB_ICON_DEMO     = makeAmbIcon(MISSION_PALETTE.DEMO);

function ambIcon(vehicleId: string, mission?: MissionType) {
  if (vehicleId === "BLR-AMB-DEMO") return AMB_ICON_DEMO;
  if (mission === "TO_PATIENT")     return AMB_ICON_PATIENT;
  return AMB_ICON_HOSPITAL;
}

function routeColour(vehicleId: string, mission?: MissionType): string {
  if (vehicleId === "BLR-AMB-DEMO") return ROUTE_COLOUR.DEMO;
  if (mission === "TO_PATIENT")     return ROUTE_COLOUR.TO_PATIENT;
  return ROUTE_COLOUR.TO_HOSPITAL;
}

// ── Sub-components that live inside <MapContainer> ─────────────────────────

function SignalMarkers({
  signals,
  triggeredIds,
}: {
  signals: TrafficSignal[];
  triggeredIds: Set<string>;
}) {
  // Own the tick here — keeps MapView parent stable while signals cycle every second
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <>
      {signals.map((sig) => {
        const isTriggered  = triggeredIds.has(sig.id);
        const info         = getCycleInfo(sig, tick, isTriggered);
        const color        = SIG_COLOUR[info.status];
        const isAmber      = info.status === "AMBER";

        return (
          <CircleMarker
            key={sig.id}
            center={[sig.lat, sig.lng]}
            radius={info.emergency ? 11 : 7}
            pathOptions={{
              color,
              fillColor: color,
              fillOpacity: info.emergency ? 1.0 : 0.88,
              weight: info.emergency ? 3 : 1.5,
              opacity: 1,
              className: info.emergency
                ? "signal-pulse"
                : isAmber
                ? "signal-amber"
                : undefined,
            }}
          >
            <Tooltip direction="top" offset={[0, -12]} opacity={0.95}>
              <div style={{ fontFamily: "monospace", fontSize: 11, lineHeight: 1.6 }}>
                {/* Junction name */}
                <div style={{ fontWeight: 700, marginBottom: 2 }}>
                  {sig.junction_name}
                </div>

                {info.emergency ? (
                  /* Emergency override state */
                  <div>
                    <span style={{ color: "#00E676", fontWeight: 600 }}>
                      ⚡ EMERGENCY GREEN — ambulance approaching
                    </span>
                    {sig.eta_seconds != null && (
                      <div style={{ color: "#aaa" }}>
                        Ambulance ETA: <strong style={{ color: "#fff" }}>{sig.eta_seconds}s</strong>
                      </div>
                    )}
                    <div style={{ color: "#aaa", fontSize: 10 }}>
                      Normal cycle resumes after ambulance passes
                    </div>
                  </div>
                ) : (
                  /* Normal cycle state */
                  <div>
                    <span style={{ color }}>
                      ● {info.status}
                    </span>
                    {" — "}
                    <span style={{ color: "#fff" }}>
                      {info.secondsRemaining}s
                    </span>
                    {" "}
                    <span style={{ color: "#888" }}>
                      until{" "}
                      {info.status === "RED"   ? "GREEN" :
                       info.status === "GREEN" ? "AMBER" : "RED"}
                    </span>
                    <div style={{ marginTop: 3 }}>
                      {/* Mini phase-bar */}
                      <div style={{
                        display: "flex", height: 4, borderRadius: 2,
                        overflow: "hidden", width: 120, background: "#333",
                      }}>
                        <div style={{
                          width: `${(CYCLE_RED / CYCLE_TOTAL) * 100}%`,
                          background: "#FF2D2D", opacity: info.status === "RED" ? 1 : 0.3,
                        }} />
                        <div style={{
                          width: `${(CYCLE_GREEN / CYCLE_TOTAL) * 100}%`,
                          background: "#00E676", opacity: info.status === "GREEN" ? 1 : 0.3,
                        }} />
                        <div style={{
                          width: `${(CYCLE_AMBER / CYCLE_TOTAL) * 100}%`,
                          background: "#FFB300", opacity: info.status === "AMBER" ? 1 : 0.3,
                        }} />
                      </div>
                      <div style={{ color: "#666", fontSize: 9, marginTop: 2 }}>
                        90s cycle · RED 50s · GREEN 35s · AMBER 5s
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </Tooltip>
          </CircleMarker>
        );
      })}
    </>
  );
}

/** Swap backend [lng, lat] pairs → Leaflet [lat, lng]. */
function toLatLng(poly: [number, number][]): [number, number][] {
  return poly.map(([lng, lat]) => [lat, lng]);
}

/** Deselect ambulance when user clicks the base map. */
function DeselectOnMapClick({ onDeselect }: { onDeselect: () => void }) {
  useMapEvents({ click: onDeselect });
  return null;
}

function AmbulanceLayer({
  ambulances,
  selectedId,
  onSelect,
}: {
  ambulances: AmbulancePosition[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <>
      {ambulances.map((amb) => {
        const isSelected = amb.vehicle_id === selectedId;
        const color = routeColour(amb.vehicle_id, amb.mission);
        const poly  = amb.route_polyline && amb.route_polyline.length >= 2
          ? toLatLng(amb.route_polyline as [number, number][])
          : null;

        // Make selected icon larger with a ring
        const iconPalette =
          amb.vehicle_id === "BLR-AMB-DEMO" ? MISSION_PALETTE.DEMO :
          amb.mission === "TO_PATIENT"       ? MISSION_PALETTE.TO_PATIENT :
                                               MISSION_PALETTE.TO_HOSPITAL;
        const icon = isSelected
          ? L.divIcon({
              className: "",
              html: `<div style="
                width:42px;height:42px;border-radius:10px;
                background:${iconPalette.bg};display:flex;align-items:center;
                justify-content:center;font-size:22px;
                box-shadow:0 0 0 4px ${iconPalette.border},0 0 20px ${iconPalette.glow};
                border:2px solid #fff;
              ">🚑</div>`,
              iconSize: [42, 42],
              iconAnchor: [21, 21],
            })
          : ambIcon(amb.vehicle_id, amb.mission);

        return (
          <span key={amb.vehicle_id}>
            {/* Route line — only shown when selected */}
            {isSelected && poly && (
              <Polyline
                positions={poly}
                pathOptions={{
                  color,
                  weight: 4,
                  opacity: 0.9,
                  dashArray: "10 6",
                }}
              />
            )}

            {/* Destination pin — only when selected */}
            {isSelected && poly && (
              <CircleMarker
                center={poly[poly.length - 1]}
                radius={7}
                pathOptions={{
                  color: "#fff",
                  fillColor: color,
                  fillOpacity: 1,
                  weight: 2,
                }}
              >
                <Tooltip direction="top" offset={[0, -10]} permanent opacity={0.95}>
                  <span className="font-mono text-xs font-bold">
                    🏥 {amb.destination ?? "Destination"}
                  </span>
                </Tooltip>
              </CircleMarker>
            )}

            {/* Ambulance marker — always visible, click to select */}
            <Marker
              position={[amb.lat, amb.lng]}
              icon={icon}
              eventHandlers={{
                click: (e) => {
                  e.originalEvent.stopPropagation();
                  onSelect(amb.vehicle_id);
                },
              }}
            >
              {/* Hover tooltip — brief, always */}
              {!isSelected && (
                <Tooltip direction="top" offset={[0, -20]} opacity={0.9}>
                  <span className="font-mono text-xs">
                    {amb.vehicle_id}
                    {amb.incident_type && INCIDENT_META[amb.incident_type]
                      ? ` · ${INCIDENT_META[amb.incident_type].icon} ${INCIDENT_META[amb.incident_type].label}`
                      : ""
                    }
                    {" · click for route"}
                  </span>
                </Tooltip>
              )}
            </Marker>
          </span>
        );
      })}
    </>
  );
}

/** Fly to the ambulance when it first appears. */
function AutoCamera({ ambulances }: { ambulances: AmbulancePosition[] }) {
  const map = useMap();
  const hasFlownRef = useRef(false);
  useEffect(() => {
    if (ambulances.length > 0 && !hasFlownRef.current) {
      hasFlownRef.current = true;
      const amb = ambulances[0];
      map.flyTo([amb.lat, amb.lng], 15, { duration: 1.4 });
    }
  }, [ambulances, map]);
  return null;
}

// ── Floating ambulance info card ────────────────────────────────────────────
function AmbulanceInfoCard({
  amb,
  onClose,
}: {
  amb: AmbulancePosition;
  onClose: () => void;
}) {
  const color =
    amb.vehicle_id === "BLR-AMB-DEMO" ? ROUTE_COLOUR.DEMO :
    amb.mission === "TO_PATIENT"       ? ROUTE_COLOUR.TO_PATIENT :
                                         ROUTE_COLOUR.TO_HOSPITAL;

  const missionLabel =
    amb.vehicle_id === "BLR-AMB-DEMO" ? "🟢 DEMO RUN" :
    amb.mission === "TO_PATIENT"       ? "🟠 TO PATIENT" :
                                         "🔵 TO HOSPITAL";

  const incident = amb.incident_type ? INCIDENT_META[amb.incident_type] : null;

  const etaMin = amb.eta_seconds != null ? Math.floor(amb.eta_seconds / 60) : null;
  const etaSec = amb.eta_seconds != null ? amb.eta_seconds % 60 : null;
  const distKm = amb.eta_seconds != null && amb.speed_kmh
    ? ((amb.speed_kmh * amb.eta_seconds) / 3600).toFixed(1)
    : null;

  return (
    <div
      className="absolute bottom-4 left-4 z-[1000] w-72 rounded-xl border border-border
                 bg-background/95 backdrop-blur-md shadow-2xl overflow-hidden"
      style={{ borderColor: color + "55" }}
    >
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-2.5"
           style={{ background: color + "18" }}>
        <div className="flex items-center gap-2">
          <span className="text-lg">🚑</span>
          <div>
            <p className="text-xs font-mono font-bold text-foreground">{amb.vehicle_id}</p>
            <p className="text-[10px] font-mono" style={{ color }}>{missionLabel}</p>
            {incident && (
              <p className="text-[10px] font-mono font-semibold" style={{ color: incident.color }}>
                {incident.icon} {incident.label}
              </p>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground text-lg leading-none"
        >×</button>
      </div>

      {/* Route */}
      <div className="px-4 py-3 space-y-2.5">
        <div className="flex items-start gap-2">
          <div className="flex flex-col items-center gap-0.5 mt-0.5">
            <span className="w-2 h-2 rounded-full bg-muted-foreground" />
            <span className="w-px h-5 bg-border" />
            <span className="w-2 h-2 rounded-full" style={{ background: color }} />
          </div>
          <div className="flex-1 space-y-1.5">
            <p className="text-xs font-mono text-muted-foreground truncate">
              {amb.origin ?? "Current location"}
            </p>
            <p className="text-xs font-mono font-semibold truncate" style={{ color }}>
              {amb.destination ?? "Destination"}
            </p>
          </div>
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-3 gap-2 pt-1 border-t border-border/40">
          <div className="text-center">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Speed</p>
            <p className="text-sm font-mono font-bold text-foreground">
              {Math.round(amb.speed_kmh ?? 0)}
            </p>
            <p className="text-[9px] text-muted-foreground">km/h</p>
          </div>
          <div className="text-center">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">ETA</p>
            <p className="text-sm font-mono font-bold tabular-nums" style={{ color }}>
              {etaMin !== null ? `${etaMin}:${String(etaSec).padStart(2, "0")}` : "—"}
            </p>
            <p className="text-[9px] text-muted-foreground">min:sec</p>
          </div>
          <div className="text-center">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Dist</p>
            <p className="text-sm font-mono font-bold text-foreground">
              {distKm ?? "—"}
            </p>
            <p className="text-[9px] text-muted-foreground">km left</p>
          </div>
        </div>

        <p className="text-[9px] text-muted-foreground text-center font-mono pt-0.5">
          Route shown on map · click map to dismiss
        </p>
      </div>
    </div>
  );
}

// ── Hospital markers ────────────────────────────────────────────────────────
const HOSPITAL_PALETTE: Record<string, { bg: string; border: string; label: string }> = {
  government: { bg: "#1565C0", border: "#42A5F5", label: "Government"  },
  private:    { bg: "#2E7D32", border: "#66BB6A", label: "Private"     },
  specialty:  { bg: "#6A1B9A", border: "#AB47BC", label: "Specialty"   },
};

function makeHospitalIcon(type: string) {
  const p = HOSPITAL_PALETTE[type] ?? HOSPITAL_PALETTE.private;
  return L.divIcon({
    className: "",
    html: `<div style="
      width:28px;height:28px;border-radius:6px;
      background:${p.bg};display:flex;align-items:center;
      justify-content:center;font-size:15px;
      box-shadow:0 0 8px ${p.border}88;
      border:2px solid ${p.border};
    ">🏥</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

const HOSP_ICON_GOV  = makeHospitalIcon("government");
const HOSP_ICON_PVT  = makeHospitalIcon("private");
const HOSP_ICON_SPEC = makeHospitalIcon("specialty");

function hospitalIcon(type: string) {
  if (type === "government") return HOSP_ICON_GOV;
  if (type === "specialty")  return HOSP_ICON_SPEC;
  return HOSP_ICON_PVT;
}

function HospitalLayer({ hospitals }: { hospitals: Hospital[] }) {
  return (
    <>
      {hospitals.map((h) => {
        const p = HOSPITAL_PALETTE[h.type] ?? HOSPITAL_PALETTE.private;
        return (
          <Marker
            key={h.name}
            position={[h.lat, h.lng]}
            icon={hospitalIcon(h.type)}
          >
            <Tooltip direction="top" offset={[0, -16]} opacity={0.95}>
              <div style={{ fontFamily: "monospace", fontSize: 11, lineHeight: 1.5 }}>
                <strong>{h.name}</strong><br />
                <span style={{ color: p.border }}>{p.label}</span>
                {" · "}
                <span style={{ color: "#aaa" }}>{h.beds} beds</span>
              </div>
            </Tooltip>
          </Marker>
        );
      })}
    </>
  );
}

// ── Green wave alert banner (slides in from top when a wave fires) ──────────
function GreenWaveAlert() {
  const latestTrigger = useRescueStore((s) => s.latestTrigger);
  const [visible, setVisible] = useState(false);
  const prevTriggerRef = useRef(latestTrigger);

  useEffect(() => {
    if (latestTrigger && latestTrigger !== prevTriggerRef.current) {
      prevTriggerRef.current = latestTrigger;
      setVisible(true);
      const t = setTimeout(() => setVisible(false), 6000);
      return () => clearTimeout(t);
    }
  }, [latestTrigger]);

  if (!latestTrigger) return null;

  const amb = latestTrigger.ambulance;
  const incident = amb.incident_type ? INCIDENT_META[amb.incident_type] : null;
  const sigCount = latestTrigger.signals.length;
  const color =
    amb.vehicle_id === "BLR-AMB-DEMO" ? ROUTE_COLOUR.DEMO :
    amb.mission === "TO_PATIENT"       ? ROUTE_COLOUR.TO_PATIENT :
                                         ROUTE_COLOUR.TO_HOSPITAL;

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key={latestTrigger.ambulance.vehicle_id + Date.now()}
          initial={{ y: -80, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -80, opacity: 0 }}
          transition={{ type: "spring", damping: 20, stiffness: 200 }}
          className="absolute top-4 left-1/2 -translate-x-1/2 z-[1001] flex items-center gap-3
                     px-5 py-3 rounded-xl shadow-2xl border backdrop-blur-md"
          style={{
            background: `${color}18`,
            borderColor: `${color}60`,
            minWidth: 320,
          }}
        >
          {/* Pulse dot */}
          <span className="relative flex h-3 w-3 shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                  style={{ background: color }} />
            <span className="relative inline-flex rounded-full h-3 w-3"
                  style={{ background: color }} />
          </span>

          <div className="flex-1 min-w-0">
            <p className="text-xs font-bold font-mono" style={{ color }}>
              🚦 GREEN WAVE ACTIVE
            </p>
            <p className="text-[11px] font-mono text-white/80 truncate">
              {amb.vehicle_id}
              {incident ? ` · ${incident.icon} ${incident.label}` : ""}
              {" — "}
              <span style={{ color }}>{sigCount} signal{sigCount !== 1 ? "s" : ""} cleared ahead</span>
            </p>
          </div>

          <button
            onClick={() => setVisible(false)}
            className="text-white/40 hover:text-white/80 text-sm leading-none shrink-0"
          >×</button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ── Main component ─────────────────────────────────────────────────────────
export default function MapView() {
  const signals            = useRescueStore(useShallow(selectSignalList));
  const ambulances         = useRescueStore(useShallow(selectAmbulanceList));
  const triggeredSignalIds = useRescueStore((s) => s.triggeredSignalIds);

  // Fetch hospitals once on mount
  const [hospitals, setHospitals] = useState<Hospital[]>([]);
  useEffect(() => {
    fetch("/api/hospitals")
      .then((r) => r.json())
      .then((data: Hospital[]) => setHospitals(data))
      .catch(() => {/* silently ignore if backend not ready */});
  }, []);

  // Which ambulance is currently selected (shows route + info card)
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const handleSelect   = useCallback((id: string) => setSelectedId((prev) => prev === id ? null : id), []);
  const handleDeselect = useCallback(() => setSelectedId(null), []);

  const selectedAmb = ambulances.find((a) => a.vehicle_id === selectedId) ?? null;

  return (
    <div className="relative h-full w-full">
      <style>{`
        .signal-pulse {
          animation: signalPulse 1.2s ease-out infinite;
        }
        @keyframes signalPulse {
          0%   { stroke-width: 3; stroke-opacity: 1; }
          60%  { stroke-width: 18; stroke-opacity: 0; }
          100% { stroke-width: 3; stroke-opacity: 1; }
        }
        .signal-amber {
          animation: amberBlink 0.7s step-start infinite;
        }
        @keyframes amberBlink {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.3; }
        }
        .leaflet-container:focus { outline: none; }
        .leaflet-tooltip {
          background: #0d1117 !important;
          border: 1px solid #30363d !important;
          color: #e6edf3 !important;
          border-radius: 6px !important;
          padding: 4px 8px !important;
          font-size: 11px !important;
        }
        .leaflet-tooltip-top::before { border-top-color: #30363d !important; }
        /* Permanent destination tooltip */
        .leaflet-tooltip-perm {
          background: #161b22 !important;
          border-color: #444 !important;
          white-space: nowrap;
        }
      `}</style>

      {/* Map legend — bottom right */}
      <div className="absolute bottom-4 right-4 z-[1000] rounded-xl border border-border/60
                      bg-background/90 backdrop-blur-md px-3 py-2.5 shadow-xl text-[10px] font-mono
                      space-y-1.5 min-w-[170px]">
        <p className="text-[9px] uppercase tracking-widest text-muted-foreground mb-1">Legend</p>

        {/* Signals — normal cycle */}
        <p className="text-[9px] text-muted-foreground/60 uppercase tracking-wider">
          Traffic Signals · 90 s cycle
        </p>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-full inline-block" style={{ background: "#FF2D2D" }} />
          <span className="text-muted-foreground">RED &nbsp;— 50 s</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-full inline-block" style={{ background: "#00E676" }} />
          <span className="text-muted-foreground">GREEN — 35 s</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-full inline-block" style={{ background: "#FFB300" }} />
          <span className="text-muted-foreground">AMBER — 5 s</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-full inline-block signal-pulse"
                style={{ background: "#00E676", display: "inline-block" }} />
          <span style={{ color: "#00E676" }}>⚡ EMERGENCY GREEN</span>
        </div>

        <div className="border-t border-border/40 my-1" />

        {/* Ambulances */}
        <p className="text-[9px] text-muted-foreground/60 uppercase tracking-wider">Ambulances</p>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded inline-block" style={{ background: "#FF6D00" }} />
          <span className="text-muted-foreground">→ Patient</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded inline-block" style={{ background: "#2979FF" }} />
          <span className="text-muted-foreground">→ Hospital</span>
        </div>

        <div className="border-t border-border/40 my-1" />

        {/* Hospitals */}
        <p className="text-[9px] text-muted-foreground/60 uppercase tracking-wider">Hospitals</p>
        <div className="flex items-center gap-1.5">
          <span className="text-xs">🏥</span>
          <span className="w-2 h-2 rounded-sm inline-block" style={{ background: "#1565C0" }} />
          <span className="text-muted-foreground">Government</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-xs">🏥</span>
          <span className="w-2 h-2 rounded-sm inline-block" style={{ background: "#2E7D32" }} />
          <span className="text-muted-foreground">Private</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-xs">🏥</span>
          <span className="w-2 h-2 rounded-sm inline-block" style={{ background: "#6A1B9A" }} />
          <span className="text-muted-foreground">Specialty / Med. College</span>
        </div>
      </div>

      {/* Green wave alert banner */}
      <GreenWaveAlert />

      {/* Floating info card — rendered outside MapContainer so it's above the map */}
      {selectedAmb && (
        <AmbulanceInfoCard amb={selectedAmb} onClose={handleDeselect} />
      )}

      <MapContainer
        center={BLR_CENTER}
        zoom={DEFAULT_ZOOM}
        style={{ height: "100%", width: "100%" }}
        zoomControl={false}
        attributionControl={false}
      >
        <TileLayer url={TILE_URL} attribution={TILE_ATTR} />

        <DeselectOnMapClick onDeselect={handleDeselect} />

        <HospitalLayer hospitals={hospitals} />

        <SignalMarkers
          signals={signals}
          triggeredIds={triggeredSignalIds}
        />
        <AmbulanceLayer
          ambulances={ambulances}
          selectedId={selectedId}
          onSelect={handleSelect}
        />
        <AutoCamera ambulances={ambulances} />
      </MapContainer>
    </div>
  );
}
