"use client";

/**
 * MapplsMap
 *
 * Renders a full-screen dark-mode Mappls Interactive Map centred on Bengaluru.
 * Responsibilities:
 *   • Load the Mappls JS SDK via a <script> tag (CDN)
 *   • Initialise the map once the SDK fires its ready callback
 *   • Keep traffic-signal markers in sync with the Zustand store
 *   • Keep ambulance markers in sync with the Zustand store
 *   • Draw a route polyline on GREEN_WAVE_TRIGGER
 *   • Animate a Framer-Motion "green wave pulse" overlay on triggered signals
 */

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";

import {
  selectAmbulanceList,
  selectSignalList,
  useRescueStore,
} from "@/lib/store";
import type { AmbulancePosition, TrafficSignal } from "@/lib/types";

// ── Mappls SDK config ──────────────────────────────────────────────────────
const MAPPLS_API_KEY = process.env.NEXT_PUBLIC_MAPPLS_API_KEY ?? "";
const MAPPLS_SDK_URL = `https://apis.mappls.com/advancedmaps/v1/${MAPPLS_API_KEY}/map_load?v=1.5&mapping=true`;

// Module-level flag so StrictMode double-mount never injects the script twice
let _sdkScriptInjected = false;

// Bengaluru centroid [lng, lat]
const BLR_CENTER: [number, number] = [77.5946, 12.9716];
const DEFAULT_ZOOM = 13;

// Stable DOM id for the map container.
// Mappls Map() expects a string ID, NOT a DOM element reference.
const MAP_CONTAINER_ID = "mappls-map-root";

// ── Signal colour map ──────────────────────────────────────────────────────
const SIGNAL_COLOURS: Record<string, string> = {
  GREEN: "#00E676",
  RED:   "#FF2D2D",
  AMBER: "#FFB300",
};

// SVG dot icon factory (inline data-URI so no external asset needed)
function signalIconSvg(color: string, isOverride: boolean): string {
  const ring = isOverride ? `<circle cx="12" cy="12" r="11" fill="none" stroke="${color}" stroke-width="2" opacity="0.6"/>` : "";
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
      ${ring}
      <circle cx="12" cy="12" r="7" fill="${color}" opacity="0.9"/>
      <circle cx="12" cy="12" r="4" fill="white" opacity="0.35"/>
    </svg>`
  )}`;
}

function ambulanceIconSvg(): string {
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">
      <rect x="2" y="2" width="32" height="32" rx="6" fill="#2979FF" opacity="0.9"/>
      <text x="18" y="24" font-size="18" text-anchor="middle" fill="white">🚑</text>
    </svg>`
  )}`;
}

// ── Types ──────────────────────────────────────────────────────────────────
interface PulseOverlay {
  id: string;
  lat: number;
  lng: number;
}

// ── Component ──────────────────────────────────────────────────────────────
export default function MapplsMap() {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapplsMap | null>(null);

  // Marker caches — keyed by signal id / vehicle_id
  const signalMarkersRef = useRef<Map<string, MapplsMarker>>(new Map());
  const ambulanceMarkersRef = useRef<Map<string, MapplsMarker>>(new Map());
  const routePolylineRef = useRef<MapplsPolyline | null>(null);

  // SDK load state
  const [sdkReady, setSdkReady] = useState(false);
  // True only after the map fires its own internal "load" event — that is when
  // window.mappls.Marker and other constructors become available.
  const [mapLoaded, setMapLoaded] = useState(false);

  // Framer Motion pulse overlays (projected to CSS pixel coords)
  const [pulseOverlays, setPulseOverlays] = useState<PulseOverlay[]>([]);

  // Store subscriptions (shallow to avoid unnecessary re-renders)
  const signals = useRescueStore(useShallow(selectSignalList));
  const ambulances = useRescueStore(useShallow(selectAmbulanceList));
  const latestTrigger = useRescueStore((s) => s.latestTrigger);
  const triggeredSignalIds = useRescueStore((s) => s.triggeredSignalIds);

  // ── 1. Inject Mappls SDK script ──────────────────────────────────────────
  useEffect(() => {
    if (typeof window === "undefined") return;

    // Already loaded (previous mount or StrictMode remount)
    if (window.mappls) {
      setSdkReady(true);
      return;
    }

    // Already injecting from a previous effect call — wait for it
    if (_sdkScriptInjected) {
      const check = setInterval(() => {
        if (window.mappls) {
          clearInterval(check);
          setSdkReady(true);
        }
      }, 100);
      return () => clearInterval(check);
    }

    _sdkScriptInjected = true;
    const script = document.createElement("script");
    script.id = "mappls-sdk";
    script.src = MAPPLS_SDK_URL;
    script.async = true;
    script.onload = () => setSdkReady(true);
    script.onerror = () => {
      _sdkScriptInjected = false;
      console.error("[MapplsMap] Failed to load Mappls SDK.");
    };
    document.head.appendChild(script);
  }, []);

  // ── 2. Initialise map once SDK is ready ───────────────────────────────────
  useEffect(() => {
    if (!sdkReady || mapRef.current) return;

    // Mappls SDK's Map constructor requires a string element ID (not a DOM
    // element ref). We poll with rAF until the element is actually in the DOM.
    let raf: number;
    const init = () => {
      const container = document.getElementById(MAP_CONTAINER_ID);
      if (!container) {
        raf = requestAnimationFrame(init);
        return;
      }
      try {
        const m = new window.mappls.Map(MAP_CONTAINER_ID, {
          center: BLR_CENTER,
          zoom: DEFAULT_ZOOM,
          zoomControl: true,
          mapStyle: "raster",
          backgroundColor: "#0d1117",
        });
        mapRef.current = m;
        // The Mappls SDK fully registers Marker / Polyline / etc. only after the
        // map fires its own "load" event — gate all overlay creation on this.
        m.on("load", () => setMapLoaded(true));
      } catch (e) {
        console.error("[MapplsMap] Map init failed:", e);
      }
    };

    raf = requestAnimationFrame(init);
    return () => cancelAnimationFrame(raf);
  }, [sdkReady]);

  // ── 3. Sync signal markers ────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!mapLoaded || !map || !window.mappls) return;

    const sdk = window.mappls;
    const existing = signalMarkersRef.current;

    for (const sig of signals) {
      const color = SIGNAL_COLOURS[sig.status] ?? SIGNAL_COLOURS.RED;
      const iconUrl = signalIconSvg(color, sig.emergency_override ?? false);

      if (existing.has(sig.id)) {
        // Update position and icon
        const marker = existing.get(sig.id)!;
        marker.setPosition([sig.lng, sig.lat]);
        marker.setIcon({ url: iconUrl, width: 24, height: 24 });
      } else {
        // Create new marker
        const marker = new sdk.Marker({
          map,
          position: [sig.lng, sig.lat],
          icon: { url: iconUrl, width: 24, height: 24 },
          title: `${sig.junction_name} — ${sig.status}`,
        });
        existing.set(sig.id, marker);
      }
    }

    // Remove stale markers
    for (const [id, marker] of existing) {
      if (!signals.find((s) => s.id === id)) {
        marker.remove();
        existing.delete(id);
      }
    }
  }, [mapLoaded, signals]);

  // ── 4. Sync ambulance markers ─────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!mapLoaded || !map || !window.mappls) return;

    const sdk = window.mappls;
    const existing = ambulanceMarkersRef.current;

    for (const amb of ambulances) {
      const iconUrl = ambulanceIconSvg();
      if (existing.has(amb.vehicle_id)) {
        existing.get(amb.vehicle_id)!.setPosition([amb.lng, amb.lat]);
      } else {
        const marker = new sdk.Marker({
          map,
          position: [amb.lng, amb.lat],
          icon: { url: iconUrl, width: 36, height: 36 },
          title: amb.vehicle_id,
        });
        existing.set(amb.vehicle_id, marker);
      }
    }

    // Remove departed ambulances
    for (const [id, marker] of existing) {
      if (!ambulances.find((a) => a.vehicle_id === id)) {
        marker.remove();
        existing.delete(id);
      }
    }
  }, [mapLoaded, ambulances]);

  // ── 5. Draw route polyline on GREEN_WAVE_TRIGGER ──────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!mapLoaded || !map || !window.mappls || !latestTrigger) return;

    const sdk = window.mappls;

    // Remove previous route
    if (routePolylineRef.current) {
      routePolylineRef.current.remove();
      routePolylineRef.current = null;
    }

    const path = latestTrigger.route_polyline as [number, number][];
    if (path.length < 2) return;

    routePolylineRef.current = new sdk.Polyline({
      map,
      path,
      strokeColor: "#00E676",
      strokeOpacity: 0.85,
      strokeWeight: 4,
    });
  }, [mapLoaded, latestTrigger]);

  // ── 6. Build pulse overlay positions from triggered signals ───────────────
  useEffect(() => {
    if (triggeredSignalIds.size === 0) {
      setPulseOverlays([]);
      return;
    }

    const overlays: PulseOverlay[] = [];
    for (const sig of signals) {
      if (triggeredSignalIds.has(sig.id)) {
        overlays.push({ id: sig.id, lat: sig.lat, lng: sig.lng });
      }
    }
    setPulseOverlays(overlays);
  }, [triggeredSignalIds, signals]);

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="relative w-full h-full bg-[#0d1117]">
      {/* Map canvas — id is passed to new mappls.Map(id, ...) */}
      <div
        id={MAP_CONTAINER_ID}
        ref={mapContainerRef}
        className="w-full h-full"
      />

      {/* SDK loading overlay */}
      {!sdkReady && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/80 backdrop-blur-sm z-10">
          <div className="flex flex-col items-center gap-3">
            <div className="w-8 h-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
            <span className="text-muted-foreground text-sm font-mono">
              Loading Mappls SDK…
            </span>
          </div>
        </div>
      )}

      {/* Framer Motion green-wave pulse rings — rendered as DOM overlays */}
      <AnimatePresence>
        {pulseOverlays.map((overlay, i) => (
          <GreenWavePulse
            key={overlay.id}
            delaySeconds={i * 0.18}
          />
        ))}
      </AnimatePresence>

      {/* Compass / attribution strip */}
      <div className="absolute bottom-2 left-2 text-[10px] text-muted-foreground font-mono select-none pointer-events-none">
        RescueRoute • Powered by Mappls
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// GreenWavePulse — animated ring rendered over the map
// ---------------------------------------------------------------------------
interface GreenWavePulseProps {
  delaySeconds: number;
}

function GreenWavePulse({ delaySeconds }: GreenWavePulseProps) {
  return (
    <motion.div
      className="absolute inset-0 pointer-events-none flex items-center justify-center"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3, delay: delaySeconds }}
    >
      {/* Three staggered expanding rings */}
      {[0, 0.4, 0.8].map((ringDelay, idx) => (
        <motion.span
          key={idx}
          className="absolute rounded-full border-2 border-[#00E676]"
          style={{ width: 24, height: 24 }}
          initial={{ scale: 1, opacity: 0.9 }}
          animate={{ scale: 5.5, opacity: 0 }}
          transition={{
            duration: 1.8,
            delay: delaySeconds + ringDelay,
            ease: "easeOut",
            repeat: 2,
            repeatDelay: 0.5,
          }}
        />
      ))}
    </motion.div>
  );
}

// Re-export types consumed by this file
type MapplsMap = import("../types/mappls").MapplsMap extends infer T ? T : never;  // satisfies TS
type MapplsMarker = import("../types/mappls").MapplsMarker extends infer T ? T : never;
type MapplsPolyline = import("../types/mappls").MapplsPolyline extends infer T ? T : never;
