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
import type { AmbulancePosition, MissionType, TrafficSignal } from "@/lib/types";

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

// Traffic-light cycle (seconds): RED 25 s → GREEN 20 s → AMBER 5 s = 50 s total
const CYCLE_RED   = 25;
const CYCLE_GREEN = 20;
const CYCLE_AMBER = 5;
const CYCLE_TOTAL = CYCLE_RED + CYCLE_GREEN + CYCLE_AMBER; // 50

/**
 * Deterministic integer hash of a string so each signal starts at a
 * different phase in the 50-second cycle.
 */
function hashId(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = (h * 0x01000193) >>> 0;
  }
  return h;
}

/** Compute the visual signal status for one tick (ignores DB state). */
function cycleStatus(sig: TrafficSignal, tick: number, isTriggered: boolean): string {
  // Emergency green wave — lock to GREEN regardless of cycle
  if (sig.emergency_override || isTriggered) return "GREEN";
  const phase = hashId(sig.id) % CYCLE_TOTAL;
  const t = (tick + phase) % CYCLE_TOTAL;
  if (t < CYCLE_RED)               return "RED";
  if (t < CYCLE_RED + CYCLE_GREEN) return "GREEN";
  return "AMBER";
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
  tick,
}: {
  signals: TrafficSignal[];
  triggeredIds: Set<string>;
  tick: number;
}) {
  return (
    <>
      {signals.map((sig) => {
        const isTriggered  = triggeredIds.has(sig.id);
        const statusNow    = cycleStatus(sig, tick, isTriggered);
        const color        = SIG_COLOUR[statusNow] ?? SIG_COLOUR.RED;
        const isGreenWave  = sig.emergency_override || isTriggered;
        const isAmber      = statusNow === "AMBER";
        return (
          <CircleMarker
            key={sig.id}
            center={[sig.lat, sig.lng]}
            radius={isGreenWave ? 10 : 7}
            pathOptions={{
              color,
              fillColor: color,
              fillOpacity: 0.9,
              weight: isGreenWave ? 3 : 1.5,
              opacity: 1,
              className: isGreenWave
                ? "signal-pulse"
                : isAmber
                ? "signal-amber"
                : undefined,
            }}
          >
            <Tooltip
              direction="top"
              offset={[0, -10]}
              opacity={0.92}
              permanent={false}
            >
              <span className="font-mono text-xs">
                {sig.junction_name}
                {isGreenWave ? (
                  <span className="ml-1 text-green-400"> ⚡ GREEN WAVE</span>
                ) : (
                  <span
                    className="ml-1"
                    style={{ color }}
                  >
                    {" "}● {statusNow}
                  </span>
                )}
                {sig.eta_seconds != null && (
                  <span className="ml-1 text-gray-300">
                    {" "}· {sig.eta_seconds}s
                  </span>
                )}
              </span>
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
                    {amb.vehicle_id} · click for route
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

// ── Main component ─────────────────────────────────────────────────────────
export default function MapView() {
  const signals            = useRescueStore(useShallow(selectSignalList));
  const ambulances         = useRescueStore(useShallow(selectAmbulanceList));
  const triggeredSignalIds = useRescueStore((s) => s.triggeredSignalIds);

  // Global 1-second tick that drives the signal colour cycle
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Which ambulance is currently selected (shows route + info card)
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const handleSelect   = useCallback((id: string) => setSelectedId((prev) => prev === id ? null : id), []);
  const handleDeselect = useCallback(() => setSelectedId(null), []);

  const selectedAmb = ambulances.find((a) => a.vehicle_id === selectedId) ?? null;

  return (
    <>
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
          animation: amberBlink 0.9s step-start infinite;
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

        <SignalMarkers
          signals={signals}
          triggeredIds={triggeredSignalIds}
          tick={tick}
        />
        <AmbulanceLayer
          ambulances={ambulances}
          selectedId={selectedId}
          onSelect={handleSelect}
        />
        <AutoCamera ambulances={ambulances} />
      </MapContainer>
    </>
  );
}
