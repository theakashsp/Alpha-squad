// ─────────────────────────────────────────────
//  Shared WebSocket message types (mirrors backend Pydantic schemas)
// ─────────────────────────────────────────────

export type SignalStatus = "RED" | "GREEN" | "AMBER";

export interface TrafficSignal {
  id: string;
  junction_name: string;
  lat: number;
  lng: number;
  status: SignalStatus;
  timer_seconds: number;
  emergency_override: boolean;
  eta_seconds: number | null;
}

export type MissionType = "TO_PATIENT" | "TO_HOSPITAL";

export type IncidentType = "CARDIAC_ARREST" | "ROAD_ACCIDENT" | "STROKE" | "TRAUMA";

export interface AmbulancePosition {
  vehicle_id: string;
  lat: number;
  lng: number;
  speed_kmh: number;
  heading: number;
  timestamp: string; // ISO-8601
  // Dispatch metadata (from ambient/demo simulation)
  mission?: MissionType;
  origin?: string;
  destination?: string;
  eta_seconds?: number;
  route_polyline?: [number, number][]; // [lng, lat] pairs — swap for Leaflet
  incident_type?: IncidentType | string;
}

export interface GreenWaveTrigger {
  type: "GREEN_WAVE_TRIGGER";
  ambulance: AmbulancePosition;
  signals: TrafficSignal[];
  route_polyline: [number, number][]; // [lng, lat] pairs for Mappls
  eta_to_destination_seconds: number;
}

export interface AmbulanceUpdate {
  type: "AMBULANCE_UPDATE";
  ambulance: AmbulancePosition;
}

export interface SignalUpdate {
  type: "SIGNAL_UPDATE";
  signal: TrafficSignal;
}

export interface ActiveRescue {
  vehicle_id: string;
  origin: string;
  destination: string;
  started_at: string;
  eta_seconds: number;
  minutes_saved: number;
  status: "ACTIVE" | "COMPLETED" | "CANCELLED";
}

export interface DashboardStats {
  active_rescues: number;
  average_minutes_saved: number;
  golden_hour_survival_rate: number; // percentage 0-100
  signals_cleared_today: number;
}

export interface AmbulanceCompleted {
  type: "AMBULANCE_COMPLETED";
  vehicle_id: string;
  minutes_saved: number;
  destination_label: string;
}

export type WSMessage =
  | GreenWaveTrigger
  | AmbulanceUpdate
  | SignalUpdate
  | AmbulanceCompleted;

export type HospitalType = "government" | "private" | "specialty";

export interface Hospital {
  name: string;
  lat: number;
  lng: number;
  type: HospitalType;
  beds: number;
}
