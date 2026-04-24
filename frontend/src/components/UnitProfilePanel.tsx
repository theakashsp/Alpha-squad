"use client";
/**
 * UnitProfilePanel
 *
 * Per-device settings panel where the ambulance operator can configure:
 *   • Their name / callsign
 *   • Their default hospital (pre-filled every time Dispatch opens)
 *
 * Settings are saved to localStorage so they survive page reloads.
 * Rendered as a portal so it always appears above the Leaflet map.
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";

import { useUnitProfile } from "@/hooks/useUnitProfile";
import type { Hospital } from "@/lib/types";

interface UnitProfilePanelProps {
  onClose: () => void;
}

export default function UnitProfilePanel({ onClose }: UnitProfilePanelProps) {
  const { profile, saveProfile, UNIT_ID } = useUnitProfile();

  // Local form state (committed on Save)
  const [operatorName,    setOperatorName]    = useState(profile.operatorName);
  const [defaultHospital, setDefaultHospital] = useState(profile.defaultHospital);
  const [hospitals, setHospitals]             = useState<Hospital[]>([]);
  const [hospitalSearch, setHospitalSearch]   = useState(profile.defaultHospital);
  const [dropdownOpen, setDropdownOpen]       = useState(false);
  const [saved, setSaved]                     = useState(false);
  const dropdownRef                           = useRef<HTMLDivElement>(null);

  // Keep local form in sync if profile loads asynchronously
  useEffect(() => {
    setOperatorName(profile.operatorName);
    setDefaultHospital(profile.defaultHospital);
    setHospitalSearch(profile.defaultHospital);
  }, [profile.operatorName, profile.defaultHospital]);

  // Fetch hospital list
  useEffect(() => {
    fetch("/api/hospitals")
      .then((r) => r.json())
      .then(setHospitals)
      .catch(() => {});
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node))
        setDropdownOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const filteredHospitals = hospitals
    .filter((h) => h.name.toLowerCase().includes(hospitalSearch.toLowerCase()))
    .slice(0, 8);

  const handleSave = () => {
    saveProfile({ operatorName, defaultHospital });
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
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
          className="w-full max-w-md bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden"
          initial={{ scale: 0.93, opacity: 0, y: 20 }}
          animate={{ scale: 1, opacity: 1, y: 0 }}
          exit={{ scale: 0.93, opacity: 0, y: 20 }}
          transition={{ type: "spring", stiffness: 320, damping: 28 }}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700 bg-gradient-to-r from-slate-900 to-slate-800">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-cyan-500/20 border border-cyan-500/40 flex items-center justify-center text-lg">
                🪪
              </div>
              <div>
                <h2 className="text-white font-bold text-base leading-tight">Unit Profile</h2>
                <p className="text-slate-400 text-xs">Settings saved on this device only</p>
              </div>
            </div>
            <button
              onClick={onClose}
              className="w-8 h-8 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-white transition-colors flex items-center justify-center text-lg"
            >
              ×
            </button>
          </div>

          <div className="px-6 py-5 space-y-5">

            {/* Unit ID — read-only */}
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
                🚑 Unit ID
              </label>
              <div className="flex items-center gap-3 bg-slate-800/60 border border-slate-700/50 rounded-xl px-4 py-3">
                <span className="font-mono font-bold text-white text-sm flex-1">{UNIT_ID}</span>
                <span className="text-[10px] text-slate-500 bg-slate-700/60 rounded-full px-2 py-0.5">
                  Set via ENV · read-only
                </span>
              </div>
            </div>

            {/* Operator name */}
            <div>
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
                👤 Operator Name
              </label>
              <input
                type="text"
                value={operatorName}
                onChange={(e) => setOperatorName(e.target.value)}
                placeholder="e.g. Rajesh Kumar · EMT"
                className="w-full bg-slate-800 border border-slate-600 rounded-xl px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 focus:border-cyan-500/60 transition-colors"
              />
            </div>

            {/* Default hospital */}
            <div ref={dropdownRef} className="relative">
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">
                🏥 Default Hospital
              </label>
              <p className="text-[10px] text-slate-500 mb-1.5">
                Pre-selected every time you open Dispatch → Go to Hospital
              </p>

              {/* Selected hospital pill */}
              {defaultHospital && (
                <div className="flex items-center gap-2 mb-2 bg-emerald-500/10 border border-emerald-500/30 rounded-lg px-3 py-2">
                  <span className="text-emerald-400 text-sm">🏥</span>
                  <span className="text-emerald-300 text-sm font-medium flex-1 truncate">{defaultHospital}</span>
                  <span className="text-[10px] text-emerald-500">current default</span>
                </div>
              )}

              {/* Search input */}
              <div className="relative">
                <input
                  type="text"
                  value={hospitalSearch}
                  onChange={(e) => { setHospitalSearch(e.target.value); setDropdownOpen(true); }}
                  onFocus={() => setDropdownOpen(true)}
                  placeholder="Search hospital…"
                  className="w-full bg-slate-800 border border-slate-600 rounded-xl px-4 py-2.5 pr-8 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 focus:border-emerald-500/60 transition-colors"
                />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none text-xs">▼</span>
              </div>

              {/* Dropdown */}
              <AnimatePresence>
                {dropdownOpen && filteredHospitals.length > 0 && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    transition={{ duration: 0.12 }}
                    className="absolute z-50 mt-1 w-full bg-[#1e2433] border border-slate-600/80 rounded-xl shadow-2xl overflow-hidden max-h-52 overflow-y-auto"
                  >
                    {filteredHospitals.map((h) => (
                      <button
                        key={h.name}
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setDefaultHospital(h.name);
                          setHospitalSearch(h.name);
                          setDropdownOpen(false);
                        }}
                        className={`w-full text-left px-4 py-2.5 hover:bg-slate-700/60 transition-colors border-b border-slate-700/30 last:border-0 ${
                          defaultHospital === h.name ? "bg-emerald-500/10" : ""
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm">🏥</span>
                          <div className="min-w-0">
                            <p className="text-sm text-white font-medium leading-tight truncate">{h.name}</p>
                            <p className="text-xs text-slate-400">
                              {h.type.charAt(0).toUpperCase() + h.type.slice(1)} · {h.beds} beds
                            </p>
                          </div>
                          {defaultHospital === h.name && (
                            <span className="ml-auto text-emerald-400 text-xs shrink-0">✓</span>
                          )}
                        </div>
                      </button>
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {/* Save button */}
            <div className="flex gap-3 pt-1 pb-1">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2.5 rounded-xl border border-slate-600 text-slate-300 hover:bg-slate-800 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSave}
                className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-bold transition-all ${
                  saved
                    ? "bg-emerald-600 text-white"
                    : "bg-cyan-600 hover:bg-cyan-500 text-white shadow-lg shadow-cyan-500/20"
                }`}
              >
                {saved ? "✓ Saved!" : "💾 Save Profile"}
              </button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  if (typeof window === "undefined") return null;
  return createPortal(modal, document.body);
}
