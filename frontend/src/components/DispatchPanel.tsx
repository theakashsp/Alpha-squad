"use client";

/**
 * DispatchPanel
 *
 * A full-screen modal overlay that lets an ambulance operator:
 *  1. Select incident type
 *  2. Search for a pickup location (from our 109 known junctions)
 *  3. Search for a destination hospital (from /api/hospitals)
 *  4. Submit → backend creates the rescue, animates the ambulance,
 *               triggers green wave along the route
 *
 * Rendered via createPortal into document.body so it sits above
 * the Leaflet map stacking context at all times.
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useRescueStore, selectSignalList } from "@/lib/store";
import { useUnitProfile } from "@/hooks/useUnitProfile";
import type { Hospital, TrafficSignal } from "@/lib/types";
// TrafficSignal used via store selector — keep import

// ── Incident types ─────────────────────────────────────────────────────────
interface IncidentOption {
  type: string;
  icon: string;
  label: string;
  color: string;
}

const INCIDENT_OPTIONS: IncidentOption[] = [
  { type: "CARDIAC_ARREST", icon: "🫀", label: "Cardiac Arrest", color: "#FF4444" },
  { type: "ROAD_ACCIDENT",  icon: "🚗", label: "Road Accident",  color: "#FF8C00" },
  { type: "STROKE",         icon: "🧠", label: "Stroke",         color: "#9B59B6" },
  { type: "TRAUMA",         icon: "🩹", label: "Trauma",         color: "#E74C3C" },
  { type: "FIRE",           icon: "🔥", label: "Fire Emergency", color: "#FF6B35" },
  { type: "MATERNITY",      icon: "🤰", label: "Maternity",      color: "#E91E8C" },
];

// ── Types ──────────────────────────────────────────────────────────────────
interface DispatchResult {
  vehicle_id: string;
  eta_seconds: number;
  route_points: number;
  destination_label: string;
}

interface DispatchPanelProps {
  onClose: () => void;
}

// ── Nominatim geocode result ───────────────────────────────────────────────
interface GeoResult {
  label: string;
  lat: number;
  lng: number;
}

// ── Nominatim suggestion item ──────────────────────────────────────────────
interface NominatimHit {
  place_id: number;
  display_name: string;
  lat: string;
  lon: string;
  type: string;
  addresstype: string;
}

// ── LocationInput — Google Maps-style live autocomplete ────────────────────
// Queries Nominatim as the user types (debounced 300 ms), biased to Bengaluru.
// Known junctions from the store appear as instant "Quick picks" at the top.
interface LocationInputProps {
  label: string;
  placeholder: string;
  suggestions: { label: string; sublabel?: string; lat: number; lng: number }[];
  selected: GeoResult | null;
  onSelect: (item: GeoResult) => void;
}

function LocationInput({
  label,
  placeholder,
  suggestions,
  selected,
  onSelect,
}: LocationInputProps) {
  const [query, setQuery]       = useState(selected?.label ?? "");
  const [open, setOpen]         = useState(false);
  const [searching, setSearching] = useState(false);
  const [hits, setHits]         = useState<NominatimHit[]>([]);
  const [error, setError]       = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef    = useRef<AbortController | null>(null);
  const ref         = useRef<HTMLDivElement>(null);

  // Sync display when parent resets selection
  useEffect(() => { setQuery(selected?.label ?? ""); }, [selected?.label]);

  // Close on outside click
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  // Live Nominatim search — fires 300 ms after each keystroke
  const searchNominatim = useCallback(async (q: string) => {
    if (q.trim().length < 2) { setHits([]); return; }
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    setSearching(true);
    setError(null);
    try {
      // viewbox restricts results to greater Bengaluru area
      const url =
        `https://nominatim.openstreetmap.org/search` +
        `?q=${encodeURIComponent(q + " Bengaluru")}` +
        `&format=json&limit=6&countrycodes=in` +
        `&viewbox=77.4,12.8,77.8,13.2&bounded=0` +
        `&addressdetails=0`;
      const res = await fetch(url, {
        signal: abortRef.current.signal,
        headers: { "Accept-Language": "en", "User-Agent": "RescueRoute/1.0" },
      });
      const data: NominatimHit[] = await res.json();
      setHits(data);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError("Search failed — check connection.");
    } finally {
      setSearching(false);
    }
  }, []);

  const handleChange = (val: string) => {
    setQuery(val);
    setError(null);
    setOpen(true);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => searchNominatim(val), 300);
  };

  const confirmHit = (hit: NominatimHit) => {
    // Show a clean 2-part label: "Name, Area" — strip the long OSM suffix
    const parts = hit.display_name.split(",").map((s) => s.trim());
    const shortLabel = parts.slice(0, 2).join(", ");
    onSelect({ label: shortLabel, lat: parseFloat(hit.lat), lng: parseFloat(hit.lon) });
    setQuery(shortLabel);
    setHits([]);
    setOpen(false);
  };

  const confirmSuggestion = (item: { label: string; lat: number; lng: number }) => {
    onSelect(item);
    setQuery(item.label);
    setHits([]);
    setOpen(false);
  };

  // Quick-pick junctions that match the current query
  const quickPicks = suggestions
    .filter((s) => query.length > 0 && s.label.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 4);

  const showDropdown = open && (searching || hits.length > 0 || quickPicks.length > 0 || error);

  return (
    <div ref={ref} className="relative">
      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
        📍 {label} <span className="text-red-400">*</span>
      </label>

      {/* Input row */}
      <div className="relative">
        {/* Pin icon on left */}
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none text-sm">
          {selected ? "📌" : "🔍"}
        </span>
        <input
          type="text"
          value={query}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          className={`w-full bg-slate-800 border rounded-lg pl-8 pr-8 py-2.5 text-sm text-white placeholder-slate-500
            focus:outline-none focus:ring-2 transition-colors
            ${selected
              ? "border-emerald-500/60 focus:ring-emerald-500/30"
              : "border-slate-600 focus:ring-cyan-500/30"
            }`}
          onChange={(e) => handleChange(e.target.value)}
          onFocus={() => { setOpen(true); if (query.length > 1) searchNominatim(query); }}
          onKeyDown={(e) => {
            if (e.key === "Escape") { setOpen(false); setHits([]); }
            if (e.key === "Enter")  { e.preventDefault(); searchNominatim(query); }
          }}
        />
        {/* Right status */}
        <span className="absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none">
          {searching ? (
            <svg className="animate-spin h-3.5 w-3.5 text-cyan-400" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
            </svg>
          ) : selected ? (
            <span className="text-emerald-400 text-xs font-bold">✓</span>
          ) : query.length > 0 ? (
            <button
              type="button"
              className="text-slate-500 hover:text-slate-300 text-xs"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => { setQuery(""); setHits([]); onSelect({ label: "", lat: 0, lng: 0 }); }}
              title="Clear"
            >✕</button>
          ) : null}
        </span>
      </div>

      {/* Resolved coords pill */}
      {selected && selected.lat !== 0 && !error && (
        <p className="mt-1 text-[10px] text-emerald-500/80 font-mono">
          {selected.lat.toFixed(5)}, {selected.lng.toFixed(5)}
        </p>
      )}

      {/* Error */}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}

      {/* ── Dropdown ── */}
      <AnimatePresence>
        {showDropdown && (
          <motion.div
            initial={{ opacity: 0, y: -6, scaleY: 0.95 }}
            animate={{ opacity: 1, y: 0, scaleY: 1 }}
            exit={{ opacity: 0, y: -6, scaleY: 0.95 }}
            transition={{ duration: 0.12 }}
            style={{ transformOrigin: "top" }}
            className="absolute z-50 mt-1 w-full bg-[#1e2433] border border-slate-600/80 rounded-xl shadow-2xl overflow-hidden max-h-64 overflow-y-auto"
          >
            {/* Quick-pick junctions */}
            {quickPicks.length > 0 && (
              <>
                <div className="px-3 pt-2.5 pb-1 flex items-center gap-1.5">
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Quick pick</span>
                </div>
                {quickPicks.map((item) => (
                  <button
                    key={item.label}
                    type="button"
                    className="w-full text-left px-3 py-2 hover:bg-slate-700/60 transition-colors flex items-start gap-2.5"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => confirmSuggestion(item)}
                  >
                    <span className="text-base mt-0.5 shrink-0">🚦</span>
                    <div>
                      <p className="text-sm text-white leading-tight">{item.label}</p>
                      {item.sublabel && <p className="text-xs text-slate-500">{item.sublabel}</p>}
                    </div>
                  </button>
                ))}
                {hits.length > 0 && <div className="border-t border-slate-700/60 mx-3" />}
              </>
            )}

            {/* Live Nominatim results */}
            {searching && hits.length === 0 && (
              <div className="px-4 py-3 text-sm text-slate-400 flex items-center gap-2">
                <svg className="animate-spin h-3.5 w-3.5 text-cyan-400 shrink-0" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
                </svg>
                Searching…
              </div>
            )}

            {hits.length > 0 && (
              <>
                <div className="px-3 pt-2.5 pb-1">
                  <span className="text-[10px] text-slate-500 uppercase tracking-wider">Locations</span>
                </div>
                {hits.map((hit) => {
                  const parts = hit.display_name.split(",").map((s) => s.trim());
                  const name = parts[0];
                  const area = parts.slice(1, 3).join(", ");
                  return (
                    <button
                      key={hit.place_id}
                      type="button"
                      className="w-full text-left px-3 py-2.5 hover:bg-slate-700/60 transition-colors flex items-start gap-2.5 border-b border-slate-700/30 last:border-0"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => confirmHit(hit)}
                    >
                      <span className="text-base mt-0.5 shrink-0">📍</span>
                      <div className="min-w-0">
                        <p className="text-sm text-white font-medium leading-tight truncate">{name}</p>
                        <p className="text-xs text-slate-400 truncate">{area}</p>
                      </div>
                    </button>
                  );
                })}
              </>
            )}

            {!searching && hits.length === 0 && quickPicks.length === 0 && query.length > 1 && (
              <p className="px-4 py-3 text-sm text-slate-500">No results — try a different name</p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── HospitalDropdown — curated hospital list ───────────────────────────────
interface HospitalDropdownProps {
  items: { label: string; sublabel?: string; lat: number; lng: number }[];
  selected: GeoResult | null;
  onSelect: (item: GeoResult) => void;
}

function HospitalDropdown({ items, selected, onSelect }: HospitalDropdownProps) {
  const [query, setQuery] = useState(selected?.label ?? "");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => { setQuery(selected?.label ?? ""); }, [selected?.label]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = items
    .filter((i) => i.label.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 8);

  return (
    <div ref={ref} className="relative">
      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
        🏥 Destination Hospital <span className="text-red-400">*</span>
      </label>
      <div className="relative">
        <input
          type="text"
          value={query}
          placeholder="Search hospital name…"
          className={`w-full bg-slate-800 border rounded-lg px-3 py-2.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 transition-colors ${
            selected
              ? "border-emerald-500/60 focus:ring-emerald-500/40"
              : "border-slate-600 focus:ring-cyan-500/40"
          }`}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
        />
        {selected && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-emerald-400 text-xs">✓</span>
        )}
      </div>
      <AnimatePresence>
        {open && filtered.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12 }}
            className="absolute z-50 mt-1 w-full bg-slate-800 border border-slate-600 rounded-lg shadow-2xl overflow-hidden max-h-52 overflow-y-auto"
          >
            {filtered.map((item) => (
              <button
                key={item.label}
                type="button"
                className="w-full text-left px-3 py-2 hover:bg-slate-700 transition-colors"
                onClick={() => {
                  onSelect({ label: item.label, lat: item.lat, lng: item.lng });
                  setQuery(item.label);
                  setOpen(false);
                }}
              >
                <p className="text-sm text-white font-medium">{item.label}</p>
                {item.sublabel && <p className="text-xs text-slate-400">{item.sublabel}</p>}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Default hospital assignment per ambulance ──────────────────────────────
const AMBULANCE_DEFAULT_HOSPITAL: Record<string, string> = {
  "BLR-AMB-001": "Manipal Hospital",
  "BLR-AMB-002": "Bowring & Lady Curzon Hospital",
  "BLR-AMB-003": "Fortis Hospital Bannerghatta",
  "BLR-AMB-004": "Apollo Hospital Bannerghatta",
  "BLR-AMB-005": "St. John's Medical College Hospital",
  "BLR-AMB-DEMO": "Manipal Hospital",
};

function defaultHospitalFor(vehicleId: string): string {
  return AMBULANCE_DEFAULT_HOSPITAL[vehicleId] ?? "Manipal Hospital";
}

// ── This device's ambulance ID — set at install time via env var ───────────
const THIS_UNIT_ID =
  process.env.NEXT_PUBLIC_AMBULANCE_ID ?? "BLR-AMB-001";

// ── Main component ─────────────────────────────────────────────────────────
export default function DispatchPanel({ onClose }: DispatchPanelProps) {
  const signals    = useRescueStore(selectSignalList);
  const thisUnit   = useRescueStore((s) => s.ambulances.get(THIS_UNIT_ID) ?? null);
  const { profile } = useUnitProfile();   // reads from localStorage
  const [hospitals, setHospitals] = useState<Hospital[]>([]);

  useEffect(() => {
    fetch("/api/hospitals").then((r) => r.json()).then(setHospitals).catch(() => {});
  }, []);

  // ── Form state ──────────────────────────────────────────────────────────
  const [incidentType, setIncidentType] = useState("CARDIAC_ARREST");

  // Destination: "hospital" quick-mode or "custom" free-text
  const [destMode, setDestMode]               = useState<"hospital" | "custom">("hospital");
  const [selectedHospital, setSelectedHospital] = useState<GeoResult | null>(null);
  const [customDest, setCustomDest]             = useState<GeoResult | null>(null);

  // Submission
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult]         = useState<DispatchResult | null>(null);
  const [error, setError]           = useState<string | null>(null);

  // ── Auto-fill default hospital from profile (localStorage) ──────────────
  useEffect(() => {
    if (!hospitals.length) return;
    // Prefer profile setting, fall back to hardcoded default
    const defName = profile.defaultHospital || defaultHospitalFor(THIS_UNIT_ID);
    const match = hospitals.find(
      (h) => h.name.toLowerCase().includes(defName.toLowerCase())
    );
    if (match) setSelectedHospital({ label: match.name, lat: match.lat, lng: match.lng });
  }, [hospitals, profile.defaultHospital]);

  // ── Derived lists ────────────────────────────────────────────────────────
  const junctionSuggestions = signals.map((s: TrafficSignal) => ({
    label: s.junction_name,
    sublabel: `Junction · ${s.lat.toFixed(4)}, ${s.lng.toFixed(4)}`,
    lat: s.lat,
    lng: s.lng,
  }));

  const hospitalItems = hospitals.map((h) => ({
    label: h.name,
    sublabel: `${h.type.charAt(0).toUpperCase() + h.type.slice(1)} · ${h.beds} beds`,
    lat: h.lat,
    lng: h.lng,
  }));

  // Source = live position of this unit (updates in real-time from store)
  const origin: GeoResult | null = thisUnit
    ? { label: thisUnit.origin ?? THIS_UNIT_ID, lat: thisUnit.lat, lng: thisUnit.lng }
    : null;

  // Destination = hospital or custom
  const destination: GeoResult | null =
    destMode === "hospital" ? selectedHospital : customDest;

  // ── Submit ───────────────────────────────────────────────────────────────
  const handleSubmit = useCallback(async () => {
    if (!origin)      { setError("Unit location not yet received. Wait a moment."); return; }
    if (!destination) { setError("Please choose a destination."); return; }

    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          origin_label:      origin.label,
          origin_lat:        origin.lat,
          origin_lng:        origin.lng,
          destination_label: destination.label,
          destination_lat:   destination.lat,
          destination_lng:   destination.lng,
          incident_type:     incidentType,
          speed_kmh:         45,
          vehicle_id:        THIS_UNIT_ID,
        }),
      });
      if (!res.ok) throw new Error(await res.text() || `HTTP ${res.status}`);
      setResult(await res.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Dispatch failed.");
    } finally {
      setSubmitting(false);
    }
  }, [origin, destination, incidentType]);

  const etaMinutes       = result ? Math.ceil(result.eta_seconds / 60) : 0;
  const selectedIncident = INCIDENT_OPTIONS.find((o) => o.type === incidentType)!;

  const resetForm = () => {
    setResult(null);
    setCustomDest(null);
    setDestMode("hospital");
    setIncidentType("CARDIAC_ARREST");
    // Re-set default hospital
    const defName = defaultHospitalFor(THIS_UNIT_ID);
    const match = hospitals.find(
      (h) => h.name.toLowerCase().includes(defName.toLowerCase())
    );
    if (match) setSelectedHospital({ label: match.name, lat: match.lat, lng: match.lng });
  };

  const modal = (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 flex items-center justify-center p-4"
        style={{ zIndex: 99999, background: "rgba(0,0,0,0.80)", backdropFilter: "blur(4px)" }}
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      >
        <motion.div
          className="relative w-full max-w-lg bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden"
          initial={{ scale: 0.92, opacity: 0, y: 24 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          exit={{ scale: 0.92, opacity: 0, y: 24 }}
          transition={{ type: "spring", stiffness: 300, damping: 28 }}
        >
          {/* ── Header ─────────────────────────────────────────────────── */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700 bg-gradient-to-r from-slate-900 to-slate-800">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-red-500/20 border border-red-500/40 flex items-center justify-center text-lg">🚑</div>
              <div>
                <h2 className="text-white font-bold text-base leading-tight">Emergency Dispatch</h2>
                <p className="text-slate-400 text-xs">Green wave activates automatically along the route</p>
              </div>
            </div>
            <button onClick={onClose}
              className="w-8 h-8 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-white transition-colors flex items-center justify-center text-lg leading-none">
              ×
            </button>
          </div>

          {/* ── SUCCESS ────────────────────────────────────────────────── */}
          {result ? (
            <div className="px-6 py-8 text-center space-y-4">
              <div className="text-5xl">✅</div>
              <h3 className="text-white text-xl font-bold">Dispatch Confirmed!</h3>
              <p className="text-slate-300 text-sm">
                <span className="text-cyan-400 font-mono">{result.vehicle_id}</span> is now en route to{" "}
                <span className="text-emerald-400 font-semibold">{destination?.label}</span>
              </p>
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-slate-800 rounded-xl p-3">
                  <p className="text-2xl font-bold text-emerald-400">{etaMinutes}</p>
                  <p className="text-xs text-slate-400">min ETA</p>
                </div>
                <div className="bg-slate-800 rounded-xl p-3">
                  <p className="text-2xl font-bold text-cyan-400">{result.route_points}</p>
                  <p className="text-xs text-slate-400">waypoints</p>
                </div>
                <div className="bg-slate-800 rounded-xl p-3">
                  <p className="text-lg font-bold text-yellow-400">{selectedIncident.icon}</p>
                  <p className="text-xs text-slate-400">{selectedIncident.label}</p>
                </div>
              </div>
              <div className="p-3 rounded-lg bg-emerald-900/30 border border-emerald-700/40 text-sm text-emerald-300">
                🟢 Green wave corridor activated — signals clearing ahead
              </div>
              <div className="flex gap-3">
                <button onClick={resetForm}
                  className="flex-1 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-sm font-medium transition-colors">
                  New Dispatch
                </button>
                <button onClick={onClose}
                  className="flex-1 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-bold transition-colors">
                  View on Map
                </button>
              </div>
            </div>
          ) : (
            /* ── FORM ────────────────────────────────────────────────── */
            <div className="px-6 py-5 space-y-5 max-h-[82vh] overflow-y-auto">

              {/* Incident Type */}
              <div>
                <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                  🏷️ Incident Type <span className="text-red-400">*</span>
                </label>
                <div className="grid grid-cols-3 gap-2">
                  {INCIDENT_OPTIONS.map((opt) => (
                    <button key={opt.type} type="button" onClick={() => setIncidentType(opt.type)}
                      className={`flex flex-col items-center gap-1 py-2.5 px-2 rounded-xl border text-xs font-medium transition-all ${
                        incidentType === opt.type ? "text-white" : "border-slate-700 text-slate-400 hover:border-slate-500"
                      }`}
                      style={incidentType === opt.type ? { borderColor: opt.color, background: `${opt.color}22` } : {}}>
                      <span className="text-xl">{opt.icon}</span>
                      <span className="leading-tight text-center">{opt.label}</span>
                    </button>
                  ))}
                </div>
              </div>

              {/* ── This Unit badge ──────────────────────────────────── */}
              <div className="flex items-center gap-3 bg-slate-800/70 border border-slate-700/70 rounded-xl px-4 py-3">
                <div className="w-10 h-10 rounded-lg bg-red-500/20 border border-red-500/30 flex items-center justify-center text-xl shrink-0">
                  🚑
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-slate-500 uppercase tracking-wider mb-0.5">This Unit</p>
                  <p className="text-white font-mono font-bold text-sm">{THIS_UNIT_ID}</p>
                  {profile.operatorName && (
                    <p className="text-[10px] text-slate-400 truncate">👤 {profile.operatorName}</p>
                  )}
                  <p className="text-[10px] text-slate-500">
                    Default hospital:{" "}
                    <span className="text-emerald-400 truncate">
                      {profile.defaultHospital || defaultHospitalFor(THIS_UNIT_ID)}
                    </span>
                  </p>
                </div>
                {/* Live GPS status */}
                {thisUnit ? (
                  <span className="inline-flex items-center gap-1 text-[10px] text-emerald-400 bg-emerald-400/10 border border-emerald-400/20 rounded-full px-2 py-1 shrink-0">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse inline-block" />
                    GPS Live
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-[10px] text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded-full px-2 py-1 shrink-0">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse inline-block" />
                    Locating…
                  </span>
                )}
              </div>

              {/* ── Current Location (source) ────────────────────────── */}
              <div>
                <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
                  📍 Current Location
                </label>
                {thisUnit ? (
                  <div className="flex items-center gap-2.5 bg-slate-800/60 border border-emerald-700/30 rounded-xl px-3 py-2.5">
                    <span className="text-emerald-400 text-base shrink-0">●</span>
                    <div className="min-w-0">
                      <p className="text-sm text-white font-medium truncate">
                        {thisUnit.origin ?? THIS_UNIT_ID}
                      </p>
                      <p className="text-[10px] text-slate-500 font-mono">
                        {thisUnit.lat.toFixed(5)}, {thisUnit.lng.toFixed(5)}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 bg-slate-800/40 border border-slate-700/40 rounded-xl px-3 py-2.5">
                    <svg className="animate-spin h-3.5 w-3.5 text-slate-500 shrink-0" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
                    </svg>
                    <p className="text-sm text-slate-500">Waiting for GPS fix… (start demo if testing)</p>
                  </div>
                )}
              </div>

              {/* ── Destination ──────────────────────────────────────── */}
              <div className="space-y-3">
                <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  🏁 Destination <span className="text-red-400">*</span>
                </label>

                {/* Mode toggle */}
                <div className="flex gap-2">
                  <button type="button" onClick={() => setDestMode("hospital")}
                    className={`flex items-center gap-2 flex-1 px-3 py-2 rounded-xl border text-sm font-medium transition-all ${
                      destMode === "hospital"
                        ? "bg-emerald-500/15 border-emerald-500/50 text-emerald-300"
                        : "bg-slate-800 border-slate-700 text-slate-400 hover:border-slate-500"
                    }`}>
                    <span className="text-base">🏥</span>
                    <span>Go to Hospital</span>
                  </button>
                  <button type="button" onClick={() => setDestMode("custom")}
                    className={`flex items-center gap-2 flex-1 px-3 py-2 rounded-xl border text-sm font-medium transition-all ${
                      destMode === "custom"
                        ? "bg-cyan-500/15 border-cyan-500/50 text-cyan-300"
                        : "bg-slate-800 border-slate-700 text-slate-400 hover:border-slate-500"
                    }`}>
                    <span className="text-base">📝</span>
                    <span>Custom Address</span>
                  </button>
                </div>

                {/* Hospital dropdown */}
                {destMode === "hospital" && (
                  <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                    <p className="text-[10px] text-slate-500 mb-1.5">
                      Default from profile:{" "}
                      <span className="text-emerald-400 font-medium">
                        {profile.defaultHospital || defaultHospitalFor(THIS_UNIT_ID)}
                      </span>
                      {" — "}change for this trip if needed
                    </p>
                    <HospitalDropdown
                      items={hospitalItems}
                      selected={selectedHospital}
                      onSelect={setSelectedHospital}
                    />
                  </motion.div>
                )}

                {/* Custom address */}
                {destMode === "custom" && (
                  <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                    <LocationInput
                      label="Custom Destination"
                      placeholder="Any Bengaluru address, landmark…"
                      suggestions={junctionSuggestions}
                      selected={customDest}
                      onSelect={setCustomDest}
                    />
                  </motion.div>
                )}
              </div>

              {/* Route preview */}
              {origin && destination && (
                <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3 text-sm space-y-1">
                  <div className="flex items-center gap-2 text-slate-300">
                    <span className="text-emerald-400 text-lg">●</span>
                    <span className="truncate text-xs">{origin.label}</span>
                  </div>
                  <div className="flex items-center gap-2 text-slate-500 pl-2">
                    <span className="text-slate-600">│</span>
                    <span className="text-[10px]">OSRM road-following route · green wave ahead</span>
                  </div>
                  <div className="flex items-center gap-2 text-slate-300">
                    <span className="text-red-400 text-lg">🏥</span>
                    <span className="truncate text-xs">{destination.label}</span>
                  </div>
                </motion.div>
              )}

              {/* Error */}
              {error && (
                <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  className="flex items-start gap-2 bg-red-900/30 border border-red-700/50 rounded-lg px-3 py-2.5 text-sm text-red-300">
                  <span className="text-red-400 mt-0.5">⚠</span>
                  <span>{error}</span>
                </motion.div>
              )}

              {/* Submit */}
              <div className="flex gap-3 pt-1 pb-2">
                <button type="button" onClick={onClose}
                  className="px-4 py-2.5 rounded-lg border border-slate-600 text-slate-300 hover:bg-slate-800 text-sm font-medium transition-colors">
                  Cancel
                </button>
                <button type="button"
                  disabled={submitting || !origin || !destination}
                  onClick={handleSubmit}
                  className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-lg text-sm font-bold transition-all ${
                    submitting || !origin || !destination
                      ? "bg-slate-700 text-slate-500 cursor-not-allowed"
                      : "bg-gradient-to-r from-red-600 to-red-500 hover:from-red-500 hover:to-red-400 text-white shadow-lg shadow-red-500/20"
                  }`}>
                  {submitting ? (
                    <>
                      <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
                      </svg>
                      Dispatching…
                    </>
                  ) : (
                    <>🚨 Dispatch Now</>
                  )}
                </button>
              </div>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  if (typeof window === "undefined") return null;
  return createPortal(modal, document.body);
}
