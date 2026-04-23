"""
Green Wave Service.

Core algorithm:
  1. Receive an AmbulancePositionSchema tick (lat, lng, speed).
  2. Query PostGIS for all TrafficLight records within SIGNAL_SEARCH_RADIUS_METERS.
  3. For each signal, compute haversine ETA (distance / ambulance speed).
  4. Any signal whose ETA <= GREEN_WAVE_LEAD_SECONDS gets flipped to GREEN
     (emergency override) and a GREEN_WAVE_TRIGGER is emitted to the frontend.
  5. Signals whose ETA has elapsed are restored to their normal cycle.
"""
import asyncio
import math
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from geoalchemy2.functions import ST_DWithin, ST_GeogFromText
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import Ambulance, SignalStatus, TrafficLight
from backend.schemas import (
    AmbulancePositionSchema,
    GreenWaveTrigger,
    SignalSchema,
    SignalStatus as SchemaSignalStatus,
)

# How many upcoming junctions to fetch ETA for
JUNCTION_LOOKAHEAD = 3

# Polyline resolution: number of intermediate points between origin and dest
_POLYLINE_STEPS = 12

# Average urban speed factor applied on top of ambulance speed to approximate
# road-network distance (~1.35x straight-line gives a reasonable urban detour)
_ROAD_FACTOR = 1.35


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class RoutingClient:
    """
    Offline routing client — uses haversine ETA and linear interpolation for
    polylines.  Drop-in replacement for the Mappls REST client; identical async
    interface so the GreenWaveService needs no changes.
    """

    async def get_route_eta(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        speed_kmh: float = 40.0,
    ) -> Optional[int]:
        """
        Returns estimated travel time in seconds.
        Uses straight-line distance scaled by a road-network factor.
        """
        dist_m = _haversine_m(origin_lat, origin_lng, dest_lat, dest_lng)
        road_dist_m = dist_m * _ROAD_FACTOR
        speed_ms = max(speed_kmh, 1.0) * 1000 / 3600
        return int(road_dist_m / speed_ms)

    async def get_route_polyline(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
    ) -> list[list[float]]:
        """
        Returns [[lng, lat], ...] with _POLYLINE_STEPS intermediate points
        linearly interpolated between origin and destination.
        """
        points: list[list[float]] = []
        for i in range(_POLYLINE_STEPS + 1):
            t = i / _POLYLINE_STEPS
            lng = origin_lng + t * (dest_lng - origin_lng)
            lat = origin_lat + t * (dest_lat - origin_lat)
            points.append([round(lng, 6), round(lat, 6)])
        return points

    async def aclose(self) -> None:
        pass  # no HTTP client to close


# ---------------------------------------------------------------------------
# GreenWaveService
# ---------------------------------------------------------------------------
class GreenWaveService:
    """
    Stateful service â€“ one instance lives for the lifetime of the FastAPI app.

    State tracked:
      â€¢ ambulance DB records (upserted on every position tick)
      â€¢ set of signal UUIDs currently under emergency override
    """

    def __init__(self) -> None:
        self.mappls = RoutingClient()
        # vehicle_id -> set of signal UUIDs in green override
        self._active_overrides: dict[str, set[UUID]] = {}

    # ------------------------------------------------------------------
    # Main entry point called by the ConnectionManager
    # Returns (trigger_or_None, list_of_restored_signals)
    # ------------------------------------------------------------------
    async def process_ambulance_update(
        self, pos: AmbulancePositionSchema
    ) -> tuple[Optional[GreenWaveTrigger], list[SignalSchema]]:
        async with AsyncSessionLocal() as session:
            # 1. Upsert ambulance record
            await self._upsert_ambulance(session, pos)

            # 2. Find signals within radius
            nearby_signals = await self._find_nearby_signals(session, pos)

            # ── AUTO-RESTORE signals the ambulance has moved away from ──────
            already_active = self._active_overrides.get(pos.vehicle_id, set())
            nearby_ids = {sig.id for (sig, _, _) in nearby_signals}
            to_restore  = {uid for uid in already_active if uid not in nearby_ids}
            restored_schemas: list[SignalSchema] = []
            if to_restore:
                restored_schemas = await self._restore_specific_signals(session, list(to_restore))
                self._active_overrides[pos.vehicle_id] -= to_restore
                await session.commit()
                logger.info(f"Restored {len(to_restore)} signals for {pos.vehicle_id}")

            if not nearby_signals:
                return None, restored_schemas

            # 3. Calculate ETAs and determine which signals to override
            speed = pos.speed_kmh if pos.speed_kmh and pos.speed_kmh > 0 else 40.0
            eta_tasks = [
                self.mappls.get_route_eta(pos.lat, pos.lng, sig_lat, sig_lng, speed_kmh=speed)
                for (_, sig_lat, sig_lng) in nearby_signals[:JUNCTION_LOOKAHEAD]
            ]
            etas = await asyncio.gather(*eta_tasks)

            signals_to_override: list[UUID] = []
            schema_signals: list[SignalSchema] = []
            triggered_signals: list[SignalSchema] = []

            for (sig, sig_lat, sig_lng), eta in zip(
                nearby_signals[:JUNCTION_LOOKAHEAD], etas
            ):
                eta_secs = eta if eta is not None else 999
                should_override = eta_secs <= settings.green_wave_lead_seconds

                schema_sig = SignalSchema(
                    id=str(sig.id),
                    junction_name=sig.junction_name,
                    lat=sig_lat,
                    lng=sig_lng,
                    status=SchemaSignalStatus.GREEN if should_override else SchemaSignalStatus(sig.current_status.value),
                    timer_seconds=sig.timer_seconds,
                    emergency_override=should_override,
                    eta_seconds=eta_secs,
                )
                schema_signals.append(schema_sig)
                if should_override:
                    signals_to_override.append(sig.id)
                    triggered_signals.append(schema_sig)

            if not triggered_signals:
                return None, restored_schemas

            # Only emit a new trigger for newly-overridden signals
            newly_triggered = [s for s in triggered_signals
                               if s.id not in {str(uid) for uid in already_active}]
            if not newly_triggered:
                return None, restored_schemas

            # 4. Persist GREEN override + commit
            await self._set_emergency_green(session, signals_to_override, pos.vehicle_id)
            await session.commit()

            # 5. Track overrides
            self._active_overrides.setdefault(pos.vehicle_id, set()).update(signals_to_override)

            # 6. Route polyline from ambulance to last triggered signal
            last_sig = triggered_signals[-1]
            polyline = await self.mappls.get_route_polyline(
                pos.lat, pos.lng, last_sig.lat, last_sig.lng
            )

            trigger = GreenWaveTrigger(
                ambulance=pos,
                signals=schema_signals,
                route_polyline=polyline,
                eta_to_destination_seconds=etas[0] if etas[0] else 0,
            )
            return trigger, restored_schemas

    # ------------------------------------------------------------------
    # PostGIS radius query
    # ------------------------------------------------------------------
    async def _find_nearby_signals(
        self, session: AsyncSession, pos: AmbulancePositionSchema
    ) -> list[tuple["TrafficLight", float, float]]:
        """
        Return list of (TrafficLight, lat, lng) within SIGNAL_SEARCH_RADIUS_METERS,
        ordered by distance (closest first).
        """
        point_wkt = f"POINT({pos.lng} {pos.lat})"
        radius = settings.signal_search_radius_meters

        stmt = (
            select(TrafficLight)
            .where(
                ST_DWithin(
                    TrafficLight.location,
                    ST_GeogFromText(point_wkt),
                    radius,
                )
            )
            .order_by(
                # ST_Distance for ordering â€“ use raw text for simplicity
                __import__("sqlalchemy").text(
                    f"ST_Distance(location, ST_GeogFromText('{point_wkt}'))"
                )
            )
            .limit(JUNCTION_LOOKAHEAD)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        out = []
        for sig in rows:
            lat, lng = sig.to_lat_lng()
            out.append((sig, lat, lng))
        return out

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    async def _upsert_ambulance(
        self, session: AsyncSession, pos: AmbulancePositionSchema
    ) -> Ambulance:
        from geoalchemy2.elements import WKTElement

        stmt = select(Ambulance).where(Ambulance.vehicle_id == pos.vehicle_id).limit(1)
        result = await session.execute(stmt)
        ambulance = result.scalars().first()

        wkt_point = WKTElement(f"POINT({pos.lng} {pos.lat})", srid=4326)

        if ambulance is None:
            ambulance = Ambulance(
                vehicle_id=pos.vehicle_id,
                current_location=wkt_point,
                speed_kmh=pos.speed_kmh,
                heading=pos.heading,
                last_seen=datetime.utcnow(),
            )
            session.add(ambulance)
        else:
            ambulance.current_location = wkt_point
            ambulance.speed_kmh = pos.speed_kmh
            ambulance.heading = pos.heading
            ambulance.last_seen = datetime.utcnow()

        return ambulance

    async def _set_emergency_green(
        self,
        session: AsyncSession,
        signal_ids: list[UUID],
        vehicle_id: str,
    ) -> None:
        if not signal_ids:
            return
        stmt = (
            update(TrafficLight)
            .where(TrafficLight.id.in_(signal_ids))  # type: ignore[attr-defined]
            .values(
                current_status=SignalStatus.GREEN,
                emergency_override=True,
                updated_at=datetime.utcnow(),
            )
        )
        await session.execute(stmt)
        logger.info(
            f"Emergency GREEN set  vehicle={vehicle_id}  signals={signal_ids}"
        )

    async def _restore_specific_signals(
        self, session: AsyncSession, signal_ids: list[UUID]
    ) -> list[SignalSchema]:
        """Reset listed signals to RED and return their schemas for broadcast."""
        if not signal_ids:
            return []
        stmt = (
            update(TrafficLight)
            .where(TrafficLight.id.in_(signal_ids))
            .values(current_status=SignalStatus.RED, emergency_override=False, updated_at=datetime.utcnow())
            .returning(TrafficLight)
        )
        result = await session.execute(stmt)
        restored = result.scalars().all()
        schemas = []
        for sig in restored:
            lat, lng = sig.to_lat_lng()
            schemas.append(SignalSchema(
                id=str(sig.id),
                junction_name=sig.junction_name,
                lat=lat, lng=lng,
                status=SchemaSignalStatus.RED,
                timer_seconds=sig.timer_seconds,
                emergency_override=False,
                eta_seconds=None,
            ))
        return schemas

    async def restore_overrides(self, vehicle_id: str, session: AsyncSession) -> None:
        """Restore signals to RED once the ambulance has passed."""
        ids = list(self._active_overrides.pop(vehicle_id, set()))
        if not ids:
            return
        stmt = (
            update(TrafficLight)
            .where(TrafficLight.id.in_(ids))  # type: ignore[attr-defined]
            .values(
                current_status=SignalStatus.RED,
                emergency_override=False,
                updated_at=datetime.utcnow(),
            )
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"Signals restored to RED  vehicle={vehicle_id}  count={len(ids)}")

    # ------------------------------------------------------------------
    # Fallback ETA (haversine / speed estimate when Mappls is unavailable)
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_eta(pos: AmbulancePositionSchema, sig_lat: float, sig_lng: float) -> int:
        import math

        R = 6_371_000
        lat1_r = math.radians(pos.lat)
        lat2_r = math.radians(sig_lat)
        dlat = math.radians(sig_lat - pos.lat)
        dlng = math.radians(sig_lng - pos.lng)
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
        dist = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        speed_ms = max(pos.speed_kmh / 3.6, 1.0)  # avoid div-by-zero
        return int(dist / speed_ms)

    async def aclose(self) -> None:
        await self.mappls.aclose()


# Singleton
green_wave_service = GreenWaveService()

