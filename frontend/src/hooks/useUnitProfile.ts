"use client";
/**
 * useUnitProfile
 *
 * Persists this ambulance unit's editable profile to localStorage.
 * Each device has its own profile keyed by NEXT_PUBLIC_AMBULANCE_ID.
 *
 * Stored fields:
 *   unitId          – fixed env var (never changes)
 *   operatorName    – driver / EMT name
 *   defaultHospital – pre-selected hospital in the Dispatch panel
 */

import { useCallback, useEffect, useState } from "react";

const UNIT_ID =
  process.env.NEXT_PUBLIC_AMBULANCE_ID ?? "BLR-AMB-001";

export interface UnitProfile {
  unitId: string;
  operatorName: string;
  defaultHospital: string;
}

// Hardcoded fallback defaults per unit (same mapping as DispatchPanel)
const UNIT_DEFAULTS: Record<string, string> = {
  "BLR-AMB-001": "Manipal Hospital",
  "BLR-AMB-002": "Bowring & Lady Curzon Hospital",
  "BLR-AMB-003": "Fortis Hospital Bannerghatta",
  "BLR-AMB-004": "Apollo Hospital Bannerghatta",
  "BLR-AMB-005": "St. John's Medical College Hospital",
  "BLR-AMB-DEMO": "Manipal Hospital",
};

const STORAGE_KEY = `rescueroute_profile_${UNIT_ID}`;

function loadProfile(): UnitProfile {
  if (typeof window === "undefined") {
    return {
      unitId: UNIT_ID,
      operatorName: "",
      defaultHospital: UNIT_DEFAULTS[UNIT_ID] ?? "Manipal Hospital",
    };
  }
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as UnitProfile;
  } catch {
    // corrupt entry — fall through to default
  }
  return {
    unitId: UNIT_ID,
    operatorName: "",
    defaultHospital: UNIT_DEFAULTS[UNIT_ID] ?? "Manipal Hospital",
  };
}

export function useUnitProfile() {
  const [profile, setProfile] = useState<UnitProfile>(loadProfile);

  // Re-read from localStorage after hydration (SSR safe)
  useEffect(() => {
    setProfile(loadProfile());
  }, []);

  const saveProfile = useCallback((updates: Partial<Omit<UnitProfile, "unitId">>) => {
    setProfile((prev) => {
      const next: UnitProfile = { ...prev, ...updates };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        // storage quota exceeded — ignore
      }
      return next;
    });
  }, []);

  return { profile, saveProfile, UNIT_ID };
}
