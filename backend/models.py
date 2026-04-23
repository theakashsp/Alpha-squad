"""
SQLModel table definitions.

PostGIS Geography columns are declared via GeoAlchemy2 — SQLModel/Alembic
will map them correctly as `GEOGRAPHY(POINT, 4326)`.

Column naming mirrors the REST/WebSocket schemas for a 1-to-1 mapping.
"""
import enum
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from sqlalchemy import Column, Enum as SAEnum, text
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class SignalStatus(str, enum.Enum):
    RED = "RED"
    GREEN = "GREEN"
    AMBER = "AMBER"


class VehicleType(str, enum.Enum):
    AMBULANCE = "ambulance"
    FIRE_TRUCK = "fire_truck"


class RescueStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# TrafficLight
# ---------------------------------------------------------------------------
class TrafficLightBase(SQLModel):
    junction_name: str = Field(index=True, max_length=120)
    current_status: SignalStatus = Field(
        default=SignalStatus.RED,
        sa_column=Column(SAEnum(SignalStatus, name="signalstatus"), nullable=False),
    )
    # Seconds remaining on current phase
    timer_seconds: int = Field(default=30, ge=0)
    # Is this signal currently in emergency-override mode?
    emergency_override: bool = Field(default=False)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": text("now()"), "onupdate": datetime.utcnow},
    )


class TrafficLight(TrafficLightBase, table=True):
    __tablename__ = "traffic_lights"

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )
    # PostGIS Geography(Point, 4326) — stored as (longitude, latitude)
    location: object = Field(
        default=None,
        sa_column=Column(
            Geography(geometry_type="POINT", srid=4326),
            nullable=False,
        ),
    )

    def to_lat_lng(self) -> tuple[float, float]:
        """Return (latitude, longitude) from the PostGIS geography point."""
        point = to_shape(self.location)
        return point.y, point.x  # shapely Point: x=lng, y=lat


class TrafficLightRead(TrafficLightBase):
    id: UUID
    lat: float
    lng: float


# ---------------------------------------------------------------------------
# Ambulance
# ---------------------------------------------------------------------------
class AmbulanceBase(SQLModel):
    vehicle_id: str = Field(index=True, max_length=20)
    vehicle_type: VehicleType = Field(
        default=VehicleType.AMBULANCE,
        sa_column=Column(SAEnum(VehicleType, name="vehicletype"), nullable=False),
    )
    speed_kmh: float = Field(default=0.0, ge=0.0)
    heading: float = Field(default=0.0, ge=0.0, le=360.0)  # degrees
    is_active: bool = Field(default=True)
    last_seen: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": text("now()")},
    )


class Ambulance(AmbulanceBase, table=True):
    __tablename__ = "ambulances"

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )
    current_location: object = Field(
        default=None,
        sa_column=Column(
            Geography(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
    )

    def to_lat_lng(self) -> tuple[float, float] | None:
        if self.current_location is None:
            return None
        point = to_shape(self.current_location)
        return point.y, point.x


class AmbulanceRead(AmbulanceBase):
    id: UUID
    lat: Optional[float] = None
    lng: Optional[float] = None


# ---------------------------------------------------------------------------
# ActiveRescue – tracks each dispatch from origin to destination
# ---------------------------------------------------------------------------
class ActiveRescueBase(SQLModel):
    vehicle_id: str = Field(max_length=20)
    origin_label: str = Field(max_length=200)
    destination_label: str = Field(max_length=200)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)
    eta_seconds: int = Field(default=0)
    minutes_saved: float = Field(default=0.0)
    status: RescueStatus = Field(
        default=RescueStatus.ACTIVE,
        sa_column=Column(SAEnum(RescueStatus, name="rescuestatus"), nullable=False),
    )
    signals_cleared: int = Field(default=0)


class ActiveRescue(ActiveRescueBase, table=True):
    __tablename__ = "active_rescues"

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )


class ActiveRescueRead(ActiveRescueBase):
    id: UUID
