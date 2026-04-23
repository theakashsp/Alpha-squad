"""
RescueRoute FastAPI application entry point.

Endpoints
â”€â”€â”€â”€â”€â”€â”€â”€â”€
WebSocket
  WS  /ws/{channel}              â€“ bi-directional stream (frontend / ai_engine / simulator)

REST (JSON)
  POST /api/signals/seed          â€“ seed Bengaluru junction fixtures
  GET  /api/signals               â€“ list all traffic signals
  POST /api/signals/override      â€“ manual signal override
  GET  /api/ambulances            â€“ list active ambulances
  POST /api/rescues               â€“ create a new rescue dispatch
  GET  /api/rescues               â€“ list rescues
  GET  /api/stats                 â€“ current dashboard KPIs
  GET  /health                    â€“ liveness probe
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from geoalchemy2.elements import WKTElement
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import AsyncSessionLocal, close_db, get_session, init_db
from backend.green_wave import green_wave_service
from backend.manager import CHANNEL_AI_ENGINE, CHANNEL_FRONTEND, CHANNEL_SIMULATOR, manager
from backend.models import (
    ActiveRescue,
    Ambulance,
    RescueStatus,
    SignalStatus,
    TrafficLight,
)
from backend.models import (
    ActiveRescueRead,
    AmbulanceRead,
    TrafficLightRead,
)
from backend.schemas import (
    ActiveRescueCreateRequest,
    DashboardStats,
    SignalOverrideRequest,
    SignalSchema,
    SignalStatus as SchemaSignalStatus,
)

# ---------------------------------------------------------------------------
# Bengaluru junction seed data (Silk Board â†’ Manipal Hospital corridor)
# ---------------------------------------------------------------------------
BLR_JUNCTIONS: list[dict] = [
    # ── DEMO route: Silk Board → Manipal Hospital ─────────────────────────
    {"name": "Silk Board Junction",       "lat": 12.9172, "lng": 77.6231},
    {"name": "HSR Layout 27th Main",      "lat": 12.9198, "lng": 77.6310},
    {"name": "Agara Junction",            "lat": 12.9245, "lng": 77.6385},
    {"name": "Sony Signal",               "lat": 12.9282, "lng": 77.6415},
    {"name": "Koramangala 5th Block",     "lat": 12.9340, "lng": 77.6338},
    {"name": "Koramangala 80ft Road",     "lat": 12.9395, "lng": 77.6260},
    {"name": "Ejipura Signal",            "lat": 12.9411, "lng": 77.6195},
    {"name": "Jyothi Nivas College",      "lat": 12.9462, "lng": 77.6215},
    {"name": "Cambridge Layout",          "lat": 12.9511, "lng": 77.6280},
    {"name": "Domlur Flyover",            "lat": 12.9592, "lng": 77.6386},
    {"name": "Airport Road / HAL 2nd St", "lat": 12.9630, "lng": 77.6450},
    {"name": "HAL Old Airport Road",      "lat": 12.9650, "lng": 77.6465},
    {"name": "Manipal Hospital Gate",     "lat": 12.9698, "lng": 77.6490},

    # ── BLR-AMB-001 route: Jayanagar → Victoria Hospital ─────────────────
    {"name": "Jayanagar 4th Block",       "lat": 12.9249, "lng": 77.5936},
    {"name": "Jayanagar 3rd Block",       "lat": 12.9283, "lng": 77.5913},
    {"name": "Lalbagh West Gate",         "lat": 12.9350, "lng": 77.5880},
    {"name": "Lalbagh Road Junction",     "lat": 12.9432, "lng": 77.5861},
    {"name": "Lalbagh North Gate",        "lat": 12.9508, "lng": 77.5848},
    {"name": "Minerva Circle",            "lat": 12.9571, "lng": 77.5793},
    {"name": "Victoria Hospital Gate",    "lat": 12.9635, "lng": 77.5742},

    # ── BLR-AMB-002 route: Indiranagar → Whitefield ──────────────────────
    {"name": "Indiranagar 100ft Road",    "lat": 12.9784, "lng": 77.6408},
    {"name": "Indiranagar CMH Road",      "lat": 12.9744, "lng": 77.6560},
    {"name": "Domlur Link Road",          "lat": 12.9680, "lng": 77.6700},
    {"name": "Marathahalli Bridge",       "lat": 12.9699, "lng": 77.6900},
    {"name": "Marathahalli Junction",     "lat": 12.9543, "lng": 77.7012},
    {"name": "Kundalahalli Gate",         "lat": 12.9432, "lng": 77.7144},
    {"name": "Whitefield Main Road",      "lat": 12.9321, "lng": 77.7276},

    # ── BLR-AMB-003 route: MG Road → Manipal Hospital ────────────────────
    {"name": "MG Road Metro Station",     "lat": 12.9756, "lng": 77.6097},
    {"name": "Ulsoor Road",               "lat": 12.9730, "lng": 77.6175},
    {"name": "Trinity Circle",            "lat": 12.9690, "lng": 77.6250},
    {"name": "Old Airport Road Junction", "lat": 12.9660, "lng": 77.6360},
    # (HAL Airport Road & Manipal Hospital Gate already listed above)
]


# ---------------------------------------------------------------------------
# Background task â€“ broadcast dashboard stats every 5 s
# ---------------------------------------------------------------------------
async def _stats_broadcaster() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                active_count = await session.scalar(
                    select(func.count(ActiveRescue.id)).where(
                        ActiveRescue.status == RescueStatus.ACTIVE
                    )
                ) or 0

                avg_saved = await session.scalar(
                    select(func.avg(ActiveRescue.minutes_saved)).where(
                        ActiveRescue.status == RescueStatus.COMPLETED
                    )
                ) or 0.0

                cleared_today = await session.scalar(
                    select(func.sum(ActiveRescue.signals_cleared)).where(
                        ActiveRescue.started_at >= datetime.utcnow().replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                    )
                ) or 0

            stats = DashboardStats(
                active_rescues=int(active_count),
                average_minutes_saved=round(float(avg_saved), 1),
                golden_hour_survival_rate=min(60.0 + float(avg_saved) * 2, 98.0),
                signals_cleared_today=int(cleared_today),
            )
            await manager.broadcast_stats(stats)
        except Exception as exc:
            logger.warning(f"Stats broadcaster error: {exc}")
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Ambient ambulance simulation  (3 vehicles, loop forever)
# ---------------------------------------------------------------------------
_AMBIENT_ROUTES = [
    {
        "vehicle_id": "BLR-AMB-001",
        "mission_fwd": "TO_HOSPITAL",
        "mission_rev": "TO_PATIENT",
        "origin":      "Jayanagar 4th Block",
        "destination": "Victoria Hospital",
        "speed_kmh":   36.0,
        "waypoints": [
            (12.9249, 77.5936, "Jayanagar 4th Block"),
            (12.9350, 77.5880, "Lalbagh West Gate"),
            (12.9508, 77.5848, "Lalbagh North"),
            (12.9635, 77.5742, "Victoria Hospital"),
        ],
    },
    {
        "vehicle_id": "BLR-AMB-002",
        "mission_fwd": "TO_PATIENT",
        "mission_rev": "TO_HOSPITAL",
        "origin":      "Indiranagar 100ft Rd",
        "destination": "Whitefield Accident",
        "speed_kmh":   44.0,
        "waypoints": [
            (12.9784, 77.6408, "Indiranagar 100ft Rd"),
            (12.9699, 77.6900, "Marathahalli Bridge"),
            (12.9543, 77.7012, "Marathahalli Junction"),
            (12.9321, 77.7276, "Whitefield Accident"),
        ],
    },
    {
        "vehicle_id": "BLR-AMB-003",
        "mission_fwd": "TO_HOSPITAL",
        "mission_rev": "TO_PATIENT",
        "origin":      "MG Road Metro",
        "destination": "Manipal Hospital",
        "speed_kmh":   40.0,
        "waypoints": [
            (12.9756, 77.6097, "MG Road Metro"),
            (12.9690, 77.6250, "Trinity Circle"),
            (12.9630, 77.6450, "HAL Airport Rd"),
            (12.9698, 77.6490, "Manipal Hospital"),
        ],
    },
]


async def _fetch_osrm_road_coords(
    waypoints: list[tuple[float, float, str]],
) -> list[tuple[float, float]] | None:
    """
    Fetch a realistic road-following route from the public OSRM demo server.
    Returns a list of (lat, lng) road coordinates, or None on failure.
    Falls back gracefully so the animation always works.
    """
    import json
    import urllib.request
    coord_str = ";".join(f"{lng},{lat}" for lat, lng, _ in waypoints)
    url = (
        f"http://router.project-osrm.org/route/v1/driving/{coord_str}"
        "?overview=full&geometries=geojson"
    )
    loop = asyncio.get_event_loop()

    def _get() -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "RescueRoute/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())

    try:
        data = await loop.run_in_executor(None, _get)
        coords = data["routes"][0]["geometry"]["coordinates"]  # [[lng, lat], ...]
        return [(lat, lng) for lng, lat in coords]             # → [(lat, lng), ...]
    except Exception as exc:
        logger.warning(f"OSRM route fetch failed ({exc}) — using linear interpolation")
        return None


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    y = math.sin(math.radians(lng2 - lng1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(math.radians(lng2 - lng1)))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


async def _animate_one_ambulance(cfg: dict) -> None:
    """
    Animate one ambulance back-and-forth along its route forever.
    Uses OSRM road geometry when available, falls back to linear interpolation.
    """
    from backend.schemas import AmbulancePositionSchema, AmbulanceUpdate

    forward = True
    TICK = 1.0  # seconds between position broadcasts

    # Pre-fetch road routes for both directions (do once, reuse on each loop)
    fwd_road: list[tuple[float, float]] | None = await _fetch_osrm_road_coords(cfg["waypoints"])
    rev_road: list[tuple[float, float]] | None = (
        list(reversed(fwd_road)) if fwd_road else None
    )
    logger.info(
        f"[{cfg['vehicle_id']}] road route: "
        f"{'OSRM (' + str(len(fwd_road)) + ' pts)' if fwd_road else 'linear fallback'}"
    )

    while True:
        wpts     = cfg["waypoints"] if forward else list(reversed(cfg["waypoints"]))
        road_pts = fwd_road if forward else rev_road
        mission  = cfg["mission_fwd"] if forward else cfg["mission_rev"]
        origin   = wpts[0][2]
        destination = wpts[-1][2]
        speed_ms = cfg["speed_kmh"] * 1000 / 3600

        if road_pts and len(road_pts) >= 2:
            # ── Road-following animation ──────────────────────────────────
            # Compute cumulative distances along road points
            cum: list[float] = [0.0]
            for k in range(1, len(road_pts)):
                cum.append(cum[-1] + _haversine_m(
                    road_pts[k-1][0], road_pts[k-1][1],
                    road_pts[k][0],   road_pts[k][1],
                ))
            total_dist = cum[-1]
            n_ticks = max(1, int(total_dist / (speed_ms * TICK)))

            for t in range(n_ticks):
                target_dist = (t / n_ticks) * total_dist

                # Binary-search for the segment containing target_dist
                lo, hi = 0, len(road_pts) - 2
                while lo < hi:
                    mid = (lo + hi) // 2
                    if cum[mid + 1] < target_dist:
                        lo = mid + 1
                    else:
                        hi = mid
                seg = lo
                seg_len = cum[seg + 1] - cum[seg]
                frac = (target_dist - cum[seg]) / seg_len if seg_len > 0 else 0.0
                lat1r, lng1r = road_pts[seg]
                lat2r, lng2r = road_pts[seg + 1]
                lat = lat1r + frac * (lat2r - lat1r)
                lng = lng1r + frac * (lng2r - lng1r)
                hdg = _bearing(lat1r, lng1r, lat2r, lng2r)

                # Remaining polyline = current pos + remaining road pts
                remaining: list[list[float]] = [[round(lng, 6), round(lat, 6)]]
                for k in range(seg + 1, len(road_pts)):
                    remaining.append([round(road_pts[k][1], 6), round(road_pts[k][0], 6)])

                dist_remaining = total_dist - target_dist
                eta_secs = int(dist_remaining / speed_ms)

                pos = AmbulancePositionSchema(
                    vehicle_id=cfg["vehicle_id"],
                    lat=lat, lng=lng,
                    speed_kmh=cfg["speed_kmh"], heading=hdg,
                    mission=mission, origin=origin, destination=destination,
                    eta_seconds=eta_secs, route_polyline=remaining,
                )
                await manager.broadcast(
                    AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos).model_dump(),
                    "frontend",
                )
                await asyncio.sleep(TICK)

        else:
            # ── Linear-interpolation fallback ─────────────────────────────
            for i in range(len(wpts) - 1):
                lat1, lng1, _ = wpts[i]
                lat2, lng2, _ = wpts[i + 1]
                seg_dist = _haversine_m(lat1, lng1, lat2, lng2)
                hdg = _bearing(lat1, lng1, lat2, lng2)
                n_ticks = max(1, int(seg_dist / (speed_ms * TICK)))

                for t in range(n_ticks):
                    frac = t / n_ticks
                    lat = lat1 + frac * (lat2 - lat1)
                    lng = lng1 + frac * (lng2 - lng1)

                    remaining: list[list[float]] = [
                        [round(lng, 6), round(lat, 6)],
                        [round(lng2, 6), round(lat2, 6)],
                    ]
                    for j in range(i + 2, len(wpts)):
                        wlat, wlng, _ = wpts[j]
                        remaining.append([round(wlng, 6), round(wlat, 6)])

                    dist_remaining = (1 - frac) * seg_dist
                    for j in range(i + 1, len(wpts) - 1):
                        dist_remaining += _haversine_m(
                            wpts[j][0], wpts[j][1], wpts[j+1][0], wpts[j+1][1]
                        )

                    pos = AmbulancePositionSchema(
                        vehicle_id=cfg["vehicle_id"],
                        lat=lat, lng=lng,
                        speed_kmh=cfg["speed_kmh"], heading=hdg,
                        mission=mission, origin=origin, destination=destination,
                        eta_seconds=int(dist_remaining / speed_ms),
                        route_polyline=remaining,
                    )
                    await manager.broadcast(
                        AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos).model_dump(),
                        "frontend",
                    )
                    await asyncio.sleep(TICK)

        forward = not forward  # reverse direction at each end


async def _ambient_ambulances_task() -> None:
    """Run all ambient ambulances concurrently."""
    await asyncio.gather(*[_animate_one_ambulance(cfg) for cfg in _AMBIENT_ROUTES])


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("RescueRoute backend starting…")
    await init_db()
    stats_task   = asyncio.create_task(_stats_broadcaster())
    ambient_task = asyncio.create_task(_ambient_ambulances_task())
    yield
    stats_task.cancel()
    ambient_task.cancel()
    await green_wave_service.aclose()
    await close_db()
    logger.info("RescueRoute backend shutdown complete.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="RescueRoute API",
    description="Multimodal AI Green-Wave orchestration for Bengaluru ambulances",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str) -> None:
    await manager.connect(websocket, channel)
    try:
        while True:
            raw = await websocket.receive_text()
            await manager.handle_message(raw, websocket, channel, green_wave_service)
    except WebSocketDisconnect:
        await manager.disconnect(websocket, channel)


# Convenience alias for the simulator (connects to /ws without a channel slug)
@app.websocket("/ws")
async def websocket_default(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket, CHANNEL_SIMULATOR)


# ---------------------------------------------------------------------------
# REST â€“ Health
# ---------------------------------------------------------------------------
@app.get("/health", tags=["infra"])
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# REST â€“ Signals
# ---------------------------------------------------------------------------
@app.post("/api/signals/seed", tags=["signals"], status_code=status.HTTP_201_CREATED)
async def seed_signals(session: AsyncSession = Depends(get_session)) -> dict:
    """Idempotently seed Bengaluru junction fixtures."""
    created = 0
    for j in BLR_JUNCTIONS:
        existing = await session.scalar(
            select(TrafficLight).where(TrafficLight.junction_name == j["name"])
        )
        if existing:
            continue
        sig = TrafficLight(
            junction_name=j["name"],
            current_status=SignalStatus.RED,
            timer_seconds=30,
            location=WKTElement(f"POINT({j['lng']} {j['lat']})", srid=4326),
        )
        session.add(sig)
        created += 1
    return {"seeded": created, "total": len(BLR_JUNCTIONS)}


@app.get("/api/signals", response_model=list[SignalSchema], tags=["signals"])
async def list_signals(session: AsyncSession = Depends(get_session)) -> list[SignalSchema]:
    result = await session.execute(select(TrafficLight))
    signals = result.scalars().all()
    out = []
    for s in signals:
        lat, lng = s.to_lat_lng()
        out.append(
            SignalSchema(
                id=str(s.id),
                junction_name=s.junction_name,
                lat=lat,
                lng=lng,
                status=SchemaSignalStatus(s.current_status.value),
                timer_seconds=s.timer_seconds,
                emergency_override=s.emergency_override,
            )
        )
    return out


@app.post("/api/signals/override", tags=["signals"])
async def override_signal(
    body: SignalOverrideRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    sig = await session.get(TrafficLight, body.signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    sig.current_status = SignalStatus(body.new_status.value)
    sig.timer_seconds = body.duration_seconds
    sig.emergency_override = body.new_status == SchemaSignalStatus.GREEN
    sig.updated_at = datetime.utcnow()
    session.add(sig)
    return {"updated": str(sig.id), "new_status": sig.current_status}


# ---------------------------------------------------------------------------
# REST â€“ Ambulances
# ---------------------------------------------------------------------------
@app.get("/api/ambulances", response_model=list[AmbulanceRead], tags=["ambulances"])
async def list_ambulances(session: AsyncSession = Depends(get_session)) -> list[AmbulanceRead]:
    result = await session.execute(
        select(Ambulance).where(Ambulance.is_active == True)  # noqa: E712
    )
    rows = result.scalars().all()
    out = []
    for a in rows:
        coords = a.to_lat_lng()
        out.append(
            AmbulanceRead(
                **a.model_dump(exclude={"current_location"}),
                lat=coords[0] if coords else None,
                lng=coords[1] if coords else None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# REST â€“ Active Rescues
# ---------------------------------------------------------------------------
@app.post(
    "/api/rescues",
    response_model=ActiveRescueRead,
    status_code=status.HTTP_201_CREATED,
    tags=["rescues"],
)
async def create_rescue(
    body: ActiveRescueCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> ActiveRescueRead:
    rescue = ActiveRescue(
        vehicle_id=body.vehicle_id,
        origin_label=body.origin_label,
        destination_label=body.destination_label,
        started_at=datetime.utcnow(),
        status=RescueStatus.ACTIVE,
    )
    session.add(rescue)
    await session.commit()
    await session.refresh(rescue)
    return ActiveRescueRead(**rescue.model_dump())


@app.get("/api/rescues", response_model=list[ActiveRescueRead], tags=["rescues"])
async def list_rescues(session: AsyncSession = Depends(get_session)) -> list[ActiveRescueRead]:
    result = await session.execute(
        select(ActiveRescue).order_by(ActiveRescue.started_at.desc()).limit(50)
    )
    return [ActiveRescueRead(**r.model_dump()) for r in result.scalars().all()]


@app.patch("/api/rescues/{rescue_id}/complete", tags=["rescues"])
async def complete_rescue(
    rescue_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    rescue = await session.get(ActiveRescue, rescue_id)
    if not rescue:
        raise HTTPException(status_code=404, detail="Rescue not found")
    rescue.status = RescueStatus.COMPLETED
    rescue.completed_at = datetime.utcnow()
    if rescue.started_at:
        elapsed = (rescue.completed_at - rescue.started_at).total_seconds() / 60
        rescue.minutes_saved = max(0.0, round(5.0 - elapsed * 0.1, 1))
    session.add(rescue)
    # Restore any overridden signals for this vehicle
    await green_wave_service.restore_overrides(rescue.vehicle_id, session)
    return {"status": "COMPLETED", "id": str(rescue_id)}


# ---------------------------------------------------------------------------
# REST â€“ Stats (snapshot for initial page load before WS kicks in)
# ---------------------------------------------------------------------------
@app.get("/api/stats", response_model=DashboardStats, tags=["stats"])
async def get_stats(session: AsyncSession = Depends(get_session)) -> DashboardStats:
    active_count = await session.scalar(
        select(func.count(ActiveRescue.id)).where(
            ActiveRescue.status == RescueStatus.ACTIVE
        )
    ) or 0

    avg_saved = await session.scalar(
        select(func.avg(ActiveRescue.minutes_saved)).where(
            ActiveRescue.status == RescueStatus.COMPLETED
        )
    ) or 0.0

    cleared_today = await session.scalar(
        select(func.sum(ActiveRescue.signals_cleared)).where(
            ActiveRescue.started_at >= datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        )
    ) or 0

    return DashboardStats(
        active_rescues=int(active_count),
        average_minutes_saved=round(float(avg_saved), 1),
        golden_hour_survival_rate=min(60.0 + float(avg_saved) * 2, 98.0),
        signals_cleared_today=int(cleared_today),
    )


# ---------------------------------------------------------------------------
# Demo mode — runs simulate_blr.py in-process as an asyncio background task
# ---------------------------------------------------------------------------
_demo_task: asyncio.Task | None = None


async def _run_demo_simulation() -> None:
    """
    Replay the Silk Board → Manipal Hospital route in-process.
    Mirrors simulate_blr.py but uses asyncio directly so it runs inside
    the FastAPI event loop — no subprocess needed.
    """
    import math
    from backend.schemas import AmbulancePositionSchema, AmbulanceUpdate

    ROUTE = [
        (12.9172, 77.6231, "Silk Board Junction"),
        (12.9198, 77.6310, "HSR Layout 27th Main"),
        (12.9245, 77.6385, "Agara Junction"),
        (12.9282, 77.6415, "Sony Signal"),
        (12.9340, 77.6338, "Koramangala 5th Block"),
        (12.9395, 77.6260, "Koramangala 80ft Road"),
        (12.9411, 77.6195, "Ejipura Signal"),
        (12.9462, 77.6215, "Jyothi Nivas College"),
        (12.9511, 77.6280, "Cambridge Layout"),
        (12.9592, 77.6386, "Domlur Flyover"),
        (12.9630, 77.6450, "Airport Road / HAL 2nd St"),
        (12.9650, 77.6465, "HAL Old Airport Road"),
        (12.9698, 77.6490, "Manipal Hospital Gate"),
    ]
    SPEED_KMH = 45.0
    DEMO_FACTOR = 6        # 6× real-time so the demo completes in ~2 minutes
    TICK_HZ = 10.0         # 10 position ticks/s → silky ambulance movement
    VEHICLE_ID = "BLR-AMB-DEMO"

    def haversine(lat1, lng1, lat2, lng2):
        R = 6_371_000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lng2 - lng1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def bearing(lat1, lng1, lat2, lng2):
        y = math.sin(math.radians(lng2-lng1)) * math.cos(math.radians(lat2))
        x = math.cos(math.radians(lat1))*math.sin(math.radians(lat2)) - \
            math.sin(math.radians(lat1))*math.cos(math.radians(lat2))*math.cos(math.radians(lng2-lng1))
        return (math.degrees(math.atan2(y, x)) + 360) % 360

    # Register rescue
    async with AsyncSessionLocal() as session:
        from backend.models import ActiveRescue, RescueStatus
        rescue = ActiveRescue(
            vehicle_id=VEHICLE_ID,
            origin="Silk Board Junction",
            destination="Manipal Hospital",
            status=RescueStatus.ACTIVE,
            started_at=datetime.utcnow(),
        )
        session.add(rescue)
        await session.commit()

    speed_ms = SPEED_KMH * 1000 / 3600
    tick_interval = 1.0 / TICK_HZ / DEMO_FACTOR

    for i in range(len(ROUTE) - 1):
        seg_lat1, seg_lng1, seg_name = ROUTE[i]
        seg_lat2, seg_lng2, _ = ROUTE[i + 1]
        dist = haversine(seg_lat1, seg_lng1, seg_lat2, seg_lng2)
        hdg = bearing(seg_lat1, seg_lng1, seg_lat2, seg_lng2)
        n_ticks = max(1, int(dist / (speed_ms / TICK_HZ)))

        for t in range(n_ticks):
                frac = t / n_ticks
                lat = seg_lat1 + frac * (seg_lat2 - seg_lat1)
                lng = seg_lng1 + frac * (seg_lng2 - seg_lng1)

                # Remaining route for the demo ambulance
                demo_remaining: list[list[float]] = [
                    [round(lng, 6), round(lat, 6)],
                    [round(seg_lng2, 6), round(seg_lat2, 6)],
                ]
                for k in range(i + 2, len(ROUTE)):
                    demo_remaining.append([round(ROUTE[k][1], 6), round(ROUTE[k][0], 6)])

                dist_rem = (1 - frac) * haversine(seg_lat1, seg_lng1, seg_lat2, seg_lng2)
                for k in range(i + 1, len(ROUTE) - 1):
                    dist_rem += haversine(ROUTE[k][0], ROUTE[k][1], ROUTE[k+1][0], ROUTE[k+1][1])
                demo_eta = int(dist_rem / (SPEED_KMH * 1000 / 3600))

                pos = AmbulancePositionSchema(
                    vehicle_id=VEHICLE_ID,
                    lat=lat, lng=lng,
                    speed_kmh=SPEED_KMH,
                    heading=hdg,
                    mission="TO_HOSPITAL",
                    origin="Silk Board Junction",
                    destination="Manipal Hospital",
                    eta_seconds=demo_eta,
                    route_polyline=demo_remaining,
                )
                update = AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos)

                # Run green-wave logic
                trigger, restored = await green_wave_service.process_ambulance_update(pos)
                # Broadcast restored signals first
                for sig in restored:
                    from backend.schemas import SignalUpdate
                    await manager.broadcast(SignalUpdate(signal=sig).model_dump(), "frontend")
                # Broadcast position + trigger
                await manager.broadcast(update.model_dump(), "frontend")
                if trigger:
                    await manager.broadcast(trigger.model_dump(), "frontend")
                    logger.info(f"[DEMO] GREEN_WAVE_TRIGGER signals={len(trigger.signals)}")

                await asyncio.sleep(tick_interval)

    logger.info("[DEMO] Simulation complete")


@app.post("/api/demo/start", tags=["demo"])
async def demo_start() -> dict:
    global _demo_task
    if _demo_task and not _demo_task.done():
        return {"status": "already_running"}
    _demo_task = asyncio.create_task(_run_demo_simulation())
    return {"status": "started"}


@app.post("/api/demo/stop", tags=["demo"])
async def demo_stop() -> dict:
    global _demo_task
    if _demo_task and not _demo_task.done():
        _demo_task.cancel()
    _demo_task = None
    return {"status": "stopped"}


@app.get("/api/demo/status", tags=["demo"])
async def demo_status() -> dict:
    running = _demo_task is not None and not _demo_task.done()
    return {"running": running}

