"""
Pydantic schemas used for WebSocket message serialisation and REST responses.

These are intentionally separate from SQLModel table models so the API
contract can evolve independently of the DB schema.
"""
from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------
class SignalStatus(str, Enum):
    RED = "RED"
    GREEN = "GREEN"
    AMBER = "AMBER"


class LatLng(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)


# ---------------------------------------------------------------------------
# Traffic signal
# ---------------------------------------------------------------------------
class SignalSchema(BaseModel):
    id: str
    junction_name: str
    lat: float
    lng: float
    status: SignalStatus
    timer_seconds: int
    emergency_override: bool = False
    eta_seconds: Optional[int] = None  # seconds until ambulance arrives


# ---------------------------------------------------------------------------
# Ambulance position
# ---------------------------------------------------------------------------
class AmbulancePositionSchema(BaseModel):
    vehicle_id: str
    lat: float
    lng: float
    speed_kmh: float = 0.0
    heading: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # Dispatch metadata (populated by ambient + demo simulations)
    mission: Optional[str] = None           # "TO_PATIENT" | "TO_HOSPITAL"
    origin: Optional[str] = None            # human-readable start label
    destination: Optional[str] = None       # human-readable end label
    eta_seconds: Optional[int] = None       # seconds to destination
    route_polyline: list[list[float]] = Field(default_factory=list)  # [[lng, lat], ...]


# ---------------------------------------------------------------------------
# WebSocket message envelopes
# ---------------------------------------------------------------------------
class GreenWaveTrigger(BaseModel):
    """
    Broadcast when the green-wave service clears a corridor of signals.
    The frontend animates the pulse across `signals` on the map.
    """
    type: Literal["GREEN_WAVE_TRIGGER"] = "GREEN_WAVE_TRIGGER"
    ambulance: AmbulancePositionSchema
    signals: list[SignalSchema]
    # Mappls route geometry: list of [lng, lat] pairs
    route_polyline: list[list[float]] = Field(default_factory=list)
    eta_to_destination_seconds: int = 0
    rescue_id: Optional[str] = None


class AmbulanceUpdate(BaseModel):
    """Raw position tick from the simulation / edge sensor."""
    type: Literal["AMBULANCE_UPDATE"] = "AMBULANCE_UPDATE"
    ambulance: AmbulancePositionSchema


class SignalUpdate(BaseModel):
    """Single signal state change (e.g. manual override cleared)."""
    type: Literal["SIGNAL_UPDATE"] = "SIGNAL_UPDATE"
    signal: SignalSchema


class DashboardStats(BaseModel):
    """Periodic heartbeat with aggregated KPI data for the dashboard."""
    type: Literal["DASHBOARD_STATS"] = "DASHBOARD_STATS"
    active_rescues: int = 0
    average_minutes_saved: float = 0.0
    golden_hour_survival_rate: float = 0.0  # 0-100 %
    signals_cleared_today: int = 0


class ErrorMessage(BaseModel):
    type: Literal["ERROR"] = "ERROR"
    code: str
    detail: str


# Discriminated union used by the ConnectionManager to parse inbound frames
WSMessage = Union[
    GreenWaveTrigger,
    AmbulanceUpdate,
    SignalUpdate,
    DashboardStats,
    ErrorMessage,
]


# ---------------------------------------------------------------------------
# REST request / response helpers
# ---------------------------------------------------------------------------
class AmbulanceUpsertRequest(BaseModel):
    vehicle_id: str
    lat: float
    lng: float
    speed_kmh: float = 0.0
    heading: float = 0.0


class SignalOverrideRequest(BaseModel):
    signal_id: UUID
    new_status: SignalStatus
    duration_seconds: int = Field(default=30, ge=5, le=300)


class ActiveRescueCreateRequest(BaseModel):
    vehicle_id: str
    origin_lat: float
    origin_lng: float
    origin_label: str
    destination_lat: float
    destination_lng: float
    destination_label: str
