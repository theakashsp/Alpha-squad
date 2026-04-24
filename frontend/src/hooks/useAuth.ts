"use client";
/**
 * useAuth
 *
 * Shift-based authentication for ambulance operators.
 *
 * - The active unit ID can be overridden per-browser via localStorage
 *   (useful when one browser/laptop is used to demo multiple units).
 * - The shift session is stored in sessionStorage → cleared when the
 *   tab/browser closes.
 * - Provides login(unitId?), logout(), and switchUnit(newId).
 */

import { useCallback, useEffect, useState } from "react";

/** Units available for selection on the login screen */
export const KNOWN_UNITS = [
  "BLR-AMB-001",
  "BLR-AMB-002",
  "BLR-AMB-003",
  "BLR-AMB-004",
  "BLR-AMB-005",
];

const ENV_UNIT_ID = process.env.NEXT_PUBLIC_AMBULANCE_ID ?? "BLR-AMB-001";
const SESSION_KEY = "rescueroute_session";
const UNIT_OVERRIDE_KEY = "rescueroute_active_unit";

export interface OperatorSession {
  operatorId: string;
  name: string;
  rank: string;
  loginAt: string;
}

function loadStoredUnitId(): string {
  if (typeof window === "undefined") return ENV_UNIT_ID;
  try {
    return localStorage.getItem(UNIT_OVERRIDE_KEY) ?? ENV_UNIT_ID;
  } catch {
    return ENV_UNIT_ID;
  }
}

function loadSession(): OperatorSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as OperatorSession) : null;
  } catch {
    return null;
  }
}

export function useAuth() {
  const [session, setSession] = useState<OperatorSession | null>(null);
  const [activeUnitId, setActiveUnitId] = useState<string>(ENV_UNIT_ID);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setActiveUnitId(loadStoredUnitId());
    setSession(loadSession());
    setHydrated(true);
  }, []);

  /** Begin shift as the given unit (defaults to activeUnitId). */
  const login = useCallback(
    (unitId?: string): { ok: boolean } => {
      const id = unitId ?? activeUnitId;
      const sess: OperatorSession = {
        operatorId: id,
        name: id,
        rank: "Ambulance Unit",
        loginAt: new Date().toISOString(),
      };
      try {
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(sess));
      } catch {
        // private browsing — still allow in-memory session
      }
      setSession(sess);
      return { ok: true };
    },
    [activeUnitId]
  );

  /** End the current shift and return to the login screen. */
  const logout = useCallback(() => {
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* ignore */
    }
    setSession(null);
  }, []);

  /**
   * Switch to a different unit ID without starting a new session.
   * Persists the choice to localStorage so subsequent logins default to it.
   * Automatically ends any active session so the user must press Begin Shift again.
   */
  const switchUnit = useCallback((newId: string) => {
    const id = newId.trim().toUpperCase() || ENV_UNIT_ID;
    try {
      localStorage.setItem(UNIT_OVERRIDE_KEY, id);
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* ignore */
    }
    setActiveUnitId(id);
    setSession(null);
  }, []);

  return {
    session,
    hydrated,
    login,
    logout,
    switchUnit,
    activeUnitId,
    isAuthenticated: !!session,
  };
}
