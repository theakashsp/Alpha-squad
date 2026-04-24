"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { KNOWN_UNITS } from "@/hooks/useAuth";

interface LoginScreenProps {
  activeUnitId: string;
  onLogin: (unitId?: string) => { ok: boolean };
  onSwitchUnit: (newId: string) => void;
}

export default function LoginScreen({
  activeUnitId,
  onLogin,
  onSwitchUnit,
}: LoginScreenProps) {
  const [loading, setLoading] = useState(false);
  const [showSwitch, setShowSwitch] = useState(false);
  const [customId, setCustomId] = useState("");

  /* ── Begin Shift ── */
  const handleBeginShift = () => {
    setLoading(true);
    setTimeout(() => {
      onLogin();
    }, 500);
  };

  /* ── Switch Account ── */
  const handlePickUnit = (id: string) => {
    onSwitchUnit(id);
    setShowSwitch(false);
    setCustomId("");
  };

  const handleCustomSwitch = () => {
    const id = customId.trim().toUpperCase();
    if (!id) return;
    handlePickUnit(id);
  };

  return (
    <div className="fixed inset-0 bg-[#0d1117] flex items-center justify-center overflow-hidden">
      {/* Grid background */}
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage:
            "linear-gradient(#00E676 1px, transparent 1px), linear-gradient(90deg, #00E676 1px, transparent 1px)",
          backgroundSize: "40px 40px",
        }}
      />
      {/* Red radial glow */}
      <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
        <div
          className="w-[600px] h-[600px] rounded-full opacity-10"
          style={{ background: "radial-gradient(circle, #FF2D2D 0%, transparent 70%)" }}
        />
      </div>

      <motion.div
        className="relative z-10 w-full max-w-sm px-4"
        initial={{ opacity: 0, y: 28 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: "easeOut" }}
      >
        {/* Brand */}
        <div className="text-center mb-8">
          <motion.div
            className="text-5xl mb-3"
            animate={{ y: [0, -4, 0] }}
            transition={{ repeat: Infinity, duration: 2.5, ease: "easeInOut" }}
          >
            🚑
          </motion.div>
          <h1 className="text-2xl font-black text-white tracking-widest uppercase">
            RescueRoute
          </h1>
          <p className="text-slate-500 text-xs font-mono mt-1 tracking-wider">
            Rolling Green Wave · Bengaluru Emergency Services
          </p>
        </div>

        {/* Card */}
        <div className="bg-slate-900 border border-slate-700/80 rounded-2xl shadow-2xl overflow-hidden">

          {/* ── Unit badge ── */}
          <div className="px-6 pt-6 pb-4 text-center space-y-4">
            <p className="text-slate-400 text-xs uppercase tracking-wider font-semibold">
              This Device
            </p>

            <div className="flex flex-col items-center gap-2">
              <div className="w-16 h-16 rounded-2xl bg-red-500/15 border border-red-500/30 flex items-center justify-center text-3xl">
                🚑
              </div>
              <p className="text-white font-mono font-black text-2xl tracking-widest">
                {activeUnitId}
              </p>
              <span className="inline-flex items-center gap-1.5 text-[11px] text-emerald-400 bg-emerald-400/10 border border-emerald-400/20 rounded-full px-3 py-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse inline-block" />
                Online · GPS Ready
              </span>
            </div>

            {/* Begin Shift */}
            <div className="border-t border-slate-800 pt-4">
              <button
                type="button"
                onClick={handleBeginShift}
                disabled={loading}
                className={`w-full py-3.5 rounded-xl font-bold text-base transition-all flex items-center justify-center gap-2 ${
                  loading
                    ? "bg-slate-700 text-slate-500 cursor-not-allowed"
                    : "bg-gradient-to-r from-red-600 to-red-500 hover:from-red-500 hover:to-red-400 text-white shadow-lg shadow-red-500/25"
                }`}
              >
                {loading ? (
                  <>
                    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
                    </svg>
                    Starting…
                  </>
                ) : (
                  "🚀 Begin Shift"
                )}
              </button>
            </div>

            {/* Switch account toggle */}
            <button
              type="button"
              onClick={() => setShowSwitch((v) => !v)}
              className="text-[11px] text-slate-500 hover:text-slate-300 transition-colors underline underline-offset-2"
            >
              {showSwitch ? "Cancel" : "Not this unit? Switch account →"}
            </button>
          </div>

          {/* ── Switch Account panel ── */}
          <AnimatePresence>
            {showSwitch && (
              <motion.div
                key="switch"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.25 }}
                className="overflow-hidden border-t border-slate-800"
              >
                <div className="px-5 py-4 space-y-3">
                  <p className="text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    Select Unit
                  </p>

                  {/* Known unit quick-pick */}
                  <div className="grid grid-cols-3 gap-2">
                    {KNOWN_UNITS.map((uid) => (
                      <button
                        key={uid}
                        type="button"
                        onClick={() => handlePickUnit(uid)}
                        className={`py-2 px-1 rounded-lg text-[11px] font-mono font-bold border transition-all ${
                          uid === activeUnitId
                            ? "border-red-500/60 bg-red-500/15 text-red-300"
                            : "border-slate-700 text-slate-400 hover:border-slate-500 hover:text-white hover:bg-slate-800"
                        }`}
                      >
                        {uid}
                      </button>
                    ))}
                  </div>

                  {/* Custom ID entry */}
                  <div className="flex gap-2 pt-1">
                    <input
                      type="text"
                      value={customId}
                      onChange={(e) => setCustomId(e.target.value.toUpperCase())}
                      onKeyDown={(e) => e.key === "Enter" && handleCustomSwitch()}
                      placeholder="Custom ID e.g. BLR-AMB-009"
                      className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-white placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/60"
                    />
                    <button
                      type="button"
                      onClick={handleCustomSwitch}
                      disabled={!customId.trim()}
                      className="px-3 py-2 rounded-lg text-xs font-bold bg-cyan-600/20 border border-cyan-500/40 text-cyan-400 hover:bg-cyan-600/30 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                    >
                      Set
                    </button>
                  </div>

                  <p className="text-slate-600 text-[10px] font-mono leading-relaxed">
                    Switching unit ends any active session. The choice is saved to this browser only.
                  </p>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <p className="text-center text-slate-700 text-[10px] font-mono mt-5">
          BBMP Emergency Services · Namma 108 Network · v2.0
        </p>
      </motion.div>
    </div>
  );
}
