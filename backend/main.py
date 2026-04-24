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
    AmbulanceCompleted,
    DashboardStats,
    DispatchRequest,
    DispatchResponse,
    SignalOverrideRequest,
    SignalSchema,
    SignalStatus as SchemaSignalStatus,
)

# ---------------------------------------------------------------------------
# Bengaluru junction seed data (Silk Board â†’ Manipal Hospital corridor)
# ---------------------------------------------------------------------------
BLR_JUNCTIONS: list[dict] = [
    # ── DEMO route: Silk Board → Manipal Hospital (13) ────────────────────
    {"name": "Silk Board Junction",           "lat": 12.9172, "lng": 77.6231},
    {"name": "HSR Layout 27th Main",          "lat": 12.9198, "lng": 77.6310},
    {"name": "Agara Junction",                "lat": 12.9245, "lng": 77.6385},
    {"name": "Sony Signal",                   "lat": 12.9282, "lng": 77.6415},
    {"name": "Koramangala 5th Block",         "lat": 12.9340, "lng": 77.6338},
    {"name": "Koramangala 80ft Road",         "lat": 12.9395, "lng": 77.6260},
    {"name": "Ejipura Signal",                "lat": 12.9411, "lng": 77.6195},
    {"name": "Jyothi Nivas College",          "lat": 12.9462, "lng": 77.6215},
    {"name": "Cambridge Layout",              "lat": 12.9511, "lng": 77.6280},
    {"name": "Domlur Flyover",                "lat": 12.9592, "lng": 77.6386},
    {"name": "Airport Road / HAL 2nd St",     "lat": 12.9630, "lng": 77.6450},
    {"name": "HAL Old Airport Road",          "lat": 12.9650, "lng": 77.6465},
    {"name": "Manipal Hospital Gate",         "lat": 12.9698, "lng": 77.6490},

    # ── AMB-001: Jayanagar → Victoria Hospital (7) ────────────────────────
    {"name": "Jayanagar 4th Block",           "lat": 12.9249, "lng": 77.5936},
    {"name": "Jayanagar 3rd Block",           "lat": 12.9283, "lng": 77.5913},
    {"name": "Lalbagh West Gate",             "lat": 12.9350, "lng": 77.5880},
    {"name": "Lalbagh Road Junction",         "lat": 12.9432, "lng": 77.5861},
    {"name": "Lalbagh North Gate",            "lat": 12.9508, "lng": 77.5848},
    {"name": "Minerva Circle",                "lat": 12.9571, "lng": 77.5793},
    {"name": "Victoria Hospital Gate",        "lat": 12.9635, "lng": 77.5742},

    # ── AMB-002: Indiranagar → Whitefield (7) ─────────────────────────────
    {"name": "Indiranagar 100ft Road",        "lat": 12.9784, "lng": 77.6408},
    {"name": "Indiranagar CMH Road",          "lat": 12.9744, "lng": 77.6560},
    {"name": "Domlur Link Road",              "lat": 12.9680, "lng": 77.6700},
    {"name": "Marathahalli Bridge",           "lat": 12.9699, "lng": 77.6900},
    {"name": "Marathahalli Junction",         "lat": 12.9543, "lng": 77.7012},
    {"name": "Kundalahalli Gate",             "lat": 12.9432, "lng": 77.7144},
    {"name": "Whitefield Main Road",          "lat": 12.9321, "lng": 77.7276},

    # ── AMB-003: MG Road → Manipal Hospital (4) ───────────────────────────
    {"name": "MG Road Metro Station",         "lat": 12.9756, "lng": 77.6097},
    {"name": "Ulsoor Road",                   "lat": 12.9730, "lng": 77.6175},
    {"name": "Trinity Circle",                "lat": 12.9690, "lng": 77.6250},
    {"name": "Old Airport Road Junction",     "lat": 12.9660, "lng": 77.6360},

    # ── Central Bangalore / CBD (10) ──────────────────────────────────────
    {"name": "KSR Railway Station (Majestic)", "lat": 12.9775, "lng": 77.5713},
    {"name": "Town Hall Circle",              "lat": 12.9735, "lng": 77.5954},
    {"name": "Shivajinagar Bus Stand",        "lat": 12.9868, "lng": 77.6010},
    {"name": "Richmond Circle",               "lat": 12.9620, "lng": 77.6005},
    {"name": "Infantry Road Junction",        "lat": 12.9837, "lng": 77.5927},
    {"name": "Vidhana Soudha Gate",           "lat": 12.9793, "lng": 77.5905},
    {"name": "Mekhri Circle",                 "lat": 13.0068, "lng": 77.5810},
    {"name": "Cunningham Road Signal",        "lat": 12.9940, "lng": 77.5899},
    {"name": "Sadashivanagar Circle",         "lat": 13.0076, "lng": 77.5703},
    {"name": "Palace Road Signal",            "lat": 12.9984, "lng": 77.5820},

    # ── North Bangalore / Hebbal (10) ─────────────────────────────────────
    {"name": "Hebbal Flyover",                "lat": 13.0352, "lng": 77.5947},
    {"name": "Nagawara Junction",             "lat": 13.0438, "lng": 77.6236},
    {"name": "Bellary Road Junction",         "lat": 13.0200, "lng": 77.5952},
    {"name": "BEL Circle",                    "lat": 13.0319, "lng": 77.5764},
    {"name": "RT Nagar Main Road",            "lat": 13.0238, "lng": 77.5944},
    {"name": "Kogilu Cross",                  "lat": 13.0678, "lng": 77.6022},
    {"name": "Thanisandra Main Road",         "lat": 13.0507, "lng": 77.6302},
    {"name": "Yelahanka New Town Signal",     "lat": 13.1009, "lng": 77.5960},
    {"name": "Bagalur Cross",                 "lat": 13.1458, "lng": 77.5850},
    {"name": "Doddaballapur Road Junction",   "lat": 13.1200, "lng": 77.5870},

    # ── Outer Ring Road (10) ──────────────────────────────────────────────
    {"name": "Tin Factory Junction",          "lat": 12.9982, "lng": 77.6640},
    {"name": "KR Puram Bridge",               "lat": 13.0082, "lng": 77.6906},
    {"name": "Mahadevapura Junction",         "lat": 12.9894, "lng": 77.7040},
    {"name": "Bellandur Signal",              "lat": 12.9261, "lng": 77.6786},
    {"name": "Sarjapur Road ORR Junction",    "lat": 12.9114, "lng": 77.6721},
    {"name": "HSR ORR Junction",              "lat": 12.9072, "lng": 77.6482},
    {"name": "Kadubeesanahalli",              "lat": 12.9555, "lng": 77.7011},
    {"name": "Iblur Junction",                "lat": 12.9229, "lng": 77.6641},
    {"name": "Carmelaram Junction",           "lat": 12.8934, "lng": 77.7035},
    {"name": "Haralur Road Junction",         "lat": 12.8930, "lng": 77.6857},

    # ── Bannerghatta Road / South Bangalore (8) ───────────────────────────
    {"name": "Jayadeva Hospital Signal",      "lat": 12.9297, "lng": 77.5943},
    {"name": "IIM Bangalore Signal",          "lat": 12.9095, "lng": 77.5962},
    {"name": "JP Nagar 3rd Phase",            "lat": 12.9059, "lng": 77.5855},
    {"name": "JP Nagar 7th Phase",            "lat": 12.8789, "lng": 77.5866},
    {"name": "Gottigere Signal",              "lat": 12.8559, "lng": 77.5952},
    {"name": "Arekere Gate",                  "lat": 12.8795, "lng": 77.6113},
    {"name": "Uttarahalli Junction",          "lat": 12.8925, "lng": 77.5573},
    {"name": "Bannerghatta Road / Nice Road", "lat": 12.8520, "lng": 77.5868},

    # ── Hosur Road / Electronic City (8) ──────────────────────────────────
    {"name": "Bommanahalli Signal",           "lat": 12.8907, "lng": 77.6186},
    {"name": "Kudlu Gate",                    "lat": 12.8936, "lng": 77.6380},
    {"name": "Hosa Road Junction",            "lat": 12.8693, "lng": 77.6432},
    {"name": "Singasandra Signal",            "lat": 12.8714, "lng": 77.6311},
    {"name": "Electronic City Phase 1",       "lat": 12.8452, "lng": 77.6613},
    {"name": "Electronic City Phase 2",       "lat": 12.8340, "lng": 77.6680},
    {"name": "Bommasandra Industrial Area",   "lat": 12.8131, "lng": 77.6694},
    {"name": "Hongasandra Signal",            "lat": 12.8718, "lng": 77.6063},

    # ── Mysore Road / West Bangalore (7) ──────────────────────────────────
    {"name": "Nayandahalli Signal",           "lat": 12.9367, "lng": 77.5280},
    {"name": "Rajarajeshwari Nagar Signal",   "lat": 12.9229, "lng": 77.5125},
    {"name": "Kengeri Signal",                "lat": 12.9086, "lng": 77.4826},
    {"name": "Nagarbhavi Circle",             "lat": 12.9567, "lng": 77.5135},
    {"name": "Vijayanagar 4th Stage",         "lat": 12.9612, "lng": 77.5267},
    {"name": "Chord Road Mysore Road",        "lat": 12.9730, "lng": 77.5388},
    {"name": "Attiguppe Signal",              "lat": 12.9565, "lng": 77.5394},

    # ── Tumkur Road / North-West (7) ──────────────────────────────────────
    {"name": "Yeshwantpur Circle",            "lat": 13.0281, "lng": 77.5437},
    {"name": "Peenya Industrial Estate",      "lat": 13.0282, "lng": 77.5196},
    {"name": "Jalahalli Cross",               "lat": 13.0485, "lng": 77.5334},
    {"name": "Hesaraghatta Road Junction",    "lat": 13.0652, "lng": 77.5183},
    {"name": "Rajajinagar 1st Block",         "lat": 12.9922, "lng": 77.5551},
    {"name": "RPC Layout Signal",             "lat": 12.9780, "lng": 77.5519},
    {"name": "Kamakshipalya Signal",          "lat": 12.9750, "lng": 77.5406},

    # ── Whitefield Extended (6) ───────────────────────────────────────────
    {"name": "ITPL Main Road",                "lat": 12.9856, "lng": 77.7275},
    {"name": "Hope Farm Junction",            "lat": 13.0071, "lng": 77.7448},
    {"name": "Hoodi Junction",                "lat": 12.9980, "lng": 77.7117},
    {"name": "Varthur Road Chowk",            "lat": 12.9390, "lng": 77.7447},
    {"name": "Brookefield Signal",            "lat": 12.9724, "lng": 77.7481},
    {"name": "Kadugodi Signal",               "lat": 13.0027, "lng": 77.7647},

    # ── Hennur / Kalyan Nagar / Horamavu (7) ─────────────────────────────
    {"name": "Kammanahalli Signal",           "lat": 13.0064, "lng": 77.6394},
    {"name": "Horamavu Signal",               "lat": 13.0229, "lng": 77.6604},
    {"name": "Banaswadi Circle",              "lat": 12.9995, "lng": 77.6459},
    {"name": "Kalyan Nagar Signal",           "lat": 13.0360, "lng": 77.6465},
    {"name": "Hennur Road Junction",          "lat": 13.0278, "lng": 77.6322},
    {"name": "HBR Layout Signal",             "lat": 13.0213, "lng": 77.6503},
    {"name": "Ramamurthy Nagar Signal",       "lat": 13.0134, "lng": 77.6683},

    # ── Sarjapur Road / Bellandur (5) ────────────────────────────────────
    {"name": "Sarjapur Main Road Signal",     "lat": 12.8678, "lng": 77.7043},
    {"name": "Adarsh Palm Meadows Junction",  "lat": 12.9617, "lng": 77.7176},
    {"name": "HSR 5th Sector",                "lat": 12.9093, "lng": 77.6397},
    {"name": "Agara Lake Road",               "lat": 12.9193, "lng": 77.6501},
    {"name": "Bellandur Lake Road",           "lat": 12.9356, "lng": 77.6863},
]


# ---------------------------------------------------------------------------
# Bengaluru major hospitals — static reference data served via /api/hospitals
# type: "government" | "private" | "specialty"
# ---------------------------------------------------------------------------
BLR_HOSPITALS: list[dict] = [
    # ── Government / Public ───────────────────────────────────────────────
    {"name": "Victoria Hospital",                          "lat": 12.9635, "lng": 77.5742, "type": "government", "beds": 1250},
    {"name": "Bowring & Lady Curzon Hospital",             "lat": 12.9740, "lng": 77.6080, "type": "government", "beds": 800},
    {"name": "NIMHANS",                                    "lat": 12.9437, "lng": 77.5959, "type": "government", "beds": 800},
    {"name": "Kidwai Memorial Cancer Institute",           "lat": 12.9297, "lng": 77.5943, "type": "government", "beds": 450},
    {"name": "Jayadeva Institute of Cardiovascular Sciences","lat": 12.9295,"lng": 77.5938, "type": "government", "beds": 650},
    {"name": "Indira Gandhi Institute of Child Health",    "lat": 12.9622, "lng": 77.5748, "type": "government", "beds": 300},
    {"name": "Rajiv Gandhi Institute of Chest Diseases",   "lat": 12.9562, "lng": 77.6012, "type": "government", "beds": 350},
    {"name": "Sri Shankara Cancer Hospital",               "lat": 12.9383, "lng": 77.5841, "type": "government", "beds": 200},
    {"name": "KR Hospital (Mysore Road)",                  "lat": 12.9545, "lng": 77.5660, "type": "government", "beds": 400},

    # ── Major Private — Multi-Specialty ───────────────────────────────────
    {"name": "Manipal Hospital (HAL Airport Road)",        "lat": 12.9698, "lng": 77.6490, "type": "private",    "beds": 600},
    {"name": "Manipal Hospital (Whitefield)",              "lat": 12.9731, "lng": 77.7402, "type": "private",    "beds": 280},
    {"name": "Apollo Hospitals (Bannerghatta Road)",       "lat": 12.9115, "lng": 77.5970, "type": "private",    "beds": 500},
    {"name": "Apollo Hospitals (Jayanagar)",               "lat": 12.9232, "lng": 77.5929, "type": "private",    "beds": 350},
    {"name": "Fortis Hospital (Cunningham Road)",          "lat": 12.9937, "lng": 77.5899, "type": "private",    "beds": 280},
    {"name": "Fortis Hospital (Bannerghatta Road)",        "lat": 12.8891, "lng": 77.5972, "type": "private",    "beds": 350},
    {"name": "Narayana Health City (Bommasandra)",         "lat": 12.8444, "lng": 77.6553, "type": "private",    "beds": 1800},
    {"name": "Sakra World Hospital (Marathahalli)",        "lat": 12.9554, "lng": 77.7031, "type": "private",    "beds": 300},
    {"name": "Aster CMI Hospital (Hebbal)",                "lat": 13.0648, "lng": 77.5944, "type": "private",    "beds": 450},
    {"name": "Columbia Asia Hospital (Hebbal)",            "lat": 13.0345, "lng": 77.5861, "type": "private",    "beds": 270},
    {"name": "BGS Gleneagles Hospital (Kengeri)",          "lat": 12.9067, "lng": 77.4847, "type": "private",    "beds": 400},
    {"name": "Sparsh Hospital (Infantry Road)",            "lat": 12.9837, "lng": 77.5927, "type": "private",    "beds": 150},
    {"name": "Cloudnine Hospital (Old Airport Road)",      "lat": 12.9638, "lng": 77.6452, "type": "private",    "beds": 100},
    {"name": "Rainbow Children's Hospital (Marathahalli)", "lat": 12.9480, "lng": 77.6871, "type": "private",    "beds": 200},
    {"name": "Aster Whitefield Hospital",                  "lat": 12.9713, "lng": 77.7508, "type": "private",    "beds": 200},
    {"name": "Narayana Multispeciality (HSR Layout)",      "lat": 12.9108, "lng": 77.6393, "type": "private",    "beds": 100},
    {"name": "Vydehi Hospital (Whitefield)",               "lat": 12.9763, "lng": 77.7510, "type": "private",    "beds": 750},
    {"name": "People Tree Hospital (Yeshwantpur)",         "lat": 13.0248, "lng": 77.5431, "type": "private",    "beds": 250},
    {"name": "Hosmat Hospital (Richmond Road)",            "lat": 12.9583, "lng": 77.6133, "type": "private",    "beds": 200},
    {"name": "Baptist Hospital (Bellary Road)",            "lat": 13.0119, "lng": 77.5940, "type": "private",    "beds": 300},
    {"name": "Wockhardt Hospital (Kalyan Nagar)",          "lat": 13.0241, "lng": 77.6487, "type": "private",    "beds": 230},
    {"name": "Sagar Apollo Hospital (Jayanagar)",          "lat": 12.9164, "lng": 77.5983, "type": "private",    "beds": 300},
    {"name": "St. Martha's Hospital (Nrupathunga Rd)",     "lat": 12.9790, "lng": 77.5940, "type": "private",    "beds": 350},
    {"name": "HCG Cancer Centre (Kalinga Rao Road)",       "lat": 12.9855, "lng": 77.5858, "type": "specialty",  "beds": 200},
    {"name": "Motherhood Hospital (Indiranagar)",          "lat": 12.9788, "lng": 77.6391, "type": "private",    "beds": 100},
    {"name": "KIMS Hospital (Whitefield)",                 "lat": 12.9728, "lng": 77.7452, "type": "private",    "beds": 350},
    {"name": "BMS Hospital (Seshadripuram)",               "lat": 12.9944, "lng": 77.5749, "type": "private",    "beds": 150},

    # ── Medical College Hospitals ──────────────────────────────────────────
    {"name": "MS Ramaiah Memorial Hospital",               "lat": 13.0374, "lng": 77.5604, "type": "specialty",  "beds": 850},
    {"name": "St. John's Medical College Hospital",        "lat": 12.9250, "lng": 77.6040, "type": "specialty",  "beds": 1260},
    {"name": "Bangalore Medical College & RI (Vani Vilas)","lat": 12.9660, "lng": 77.5745, "type": "specialty",  "beds": 1100},
    {"name": "Kempegowda Institute of Medical Sciences",   "lat": 12.9235, "lng": 77.5603, "type": "specialty",  "beds": 1000},
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
                ) or 4.2  # realistic default until a rescue completes

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
                # 78 % baseline + 2.5 pts per minute saved -> ~88.5 % with 4.2 avg
                golden_hour_survival_rate=min(78.0 + float(avg_saved) * 2.5, 98.0),
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
        "vehicle_id":    "BLR-AMB-001",
        "mission_fwd":   "TO_HOSPITAL",
        "mission_rev":   "TO_PATIENT",
        "origin":        "Jayanagar 4th Block",
        "destination":   "Victoria Hospital",
        "incident_type": "CARDIAC_ARREST",
        "speed_kmh":     36.0,
        "waypoints": [
            (12.9249, 77.5936, "Jayanagar 4th Block"),
            (12.9350, 77.5880, "Lalbagh West Gate"),
            (12.9508, 77.5848, "Lalbagh North"),
            (12.9635, 77.5742, "Victoria Hospital"),
        ],
    },
    {
        "vehicle_id":    "BLR-AMB-002",
        "mission_fwd":   "TO_PATIENT",
        "mission_rev":   "TO_HOSPITAL",
        "origin":        "Indiranagar 100ft Rd",
        "destination":   "Whitefield Accident",
        "incident_type": "ROAD_ACCIDENT",
        "speed_kmh":     44.0,
        "waypoints": [
            (12.9784, 77.6408, "Indiranagar 100ft Rd"),
            (12.9699, 77.6900, "Marathahalli Bridge"),
            (12.9543, 77.7012, "Marathahalli Junction"),
            (12.9321, 77.7276, "Whitefield Accident"),
        ],
    },
    {
        "vehicle_id":    "BLR-AMB-003",
        "mission_fwd":   "TO_HOSPITAL",
        "mission_rev":   "TO_PATIENT",
        "origin":        "MG Road Metro",
        "destination":   "Manipal Hospital",
        "incident_type": "STROKE",
        "speed_kmh":     40.0,
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
    Each position tick also runs through the green wave service so signals
    ahead of the ambulance are cleared in real time.
    """
    from backend.schemas import AmbulancePositionSchema, AmbulanceUpdate, SignalUpdate

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
                        incident_type=cfg.get("incident_type"),
                    )
                await manager.broadcast(
                    AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos).model_dump(),
                    "frontend",
                )
                # Run green-wave logic for this ambulance
                try:
                    trigger, restored = await green_wave_service.process_ambulance_update(pos)
                    for sig in restored:
                        await manager.broadcast(SignalUpdate(signal=sig).model_dump(), "frontend")
                    if trigger:
                        await manager.broadcast(trigger.model_dump(), "frontend")
                except Exception as _gw_err:
                    logger.debug(f"[{cfg['vehicle_id']}] green wave: {_gw_err}")
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
                        incident_type=cfg.get("incident_type"),
                    )
                    await manager.broadcast(
                        AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos).model_dump(),
                        "frontend",
                    )
                    # Run green-wave logic for this ambulance
                    try:
                        trigger, restored = await green_wave_service.process_ambulance_update(pos)
                        for sig in restored:
                            await manager.broadcast(SignalUpdate(signal=sig).model_dump(), "frontend")
                        if trigger:
                            await manager.broadcast(trigger.model_dump(), "frontend")
                    except Exception as _gw_err:
                        logger.debug(f"[{cfg['vehicle_id']}] green wave: {_gw_err}")
                    await asyncio.sleep(TICK)

        forward = not forward  # reverse direction at each end


async def _ambient_ambulances_task() -> None:
    """Run all ambient ambulances concurrently."""
    await asyncio.gather(*[_animate_one_ambulance(cfg) for cfg in _AMBIENT_ROUTES])


# ---------------------------------------------------------------------------
# Startup helpers — seed realistic demo data into the database
# ---------------------------------------------------------------------------
async def _seed_ambient_rescues() -> None:
    """Create or refresh ActiveRescue rows for the 3 ambient ambulances."""
    async with AsyncSessionLocal() as session:
        for cfg in _AMBIENT_ROUTES:
            existing = await session.scalar(
                select(ActiveRescue).where(ActiveRescue.vehicle_id == cfg["vehicle_id"])
            )
            if existing:
                existing.status = RescueStatus.ACTIVE
                existing.started_at = datetime.utcnow()
                existing.signals_cleared = 0
            else:
                rescue = ActiveRescue(
                    vehicle_id=cfg["vehicle_id"],
                    origin_label=cfg["origin"],
                    destination_label=cfg["destination"],
                    status=RescueStatus.ACTIVE,
                    started_at=datetime.utcnow(),
                )
                session.add(rescue)
        await session.commit()
    logger.info("Ambient rescue records seeded.")


async def _seed_historical_rescues() -> None:
    """Seed completed historical rescues so dashboard stats look live from day one."""
    from datetime import timedelta
    HISTORY = [
        {"vid": "BLR-HIS-001", "origin": "Koramangala 4th Block", "dest": "Victoria Hospital",  "mins": 5.2, "sigs": 4},
        {"vid": "BLR-HIS-002", "origin": "Silk Board Junction",   "dest": "Manipal Hospital",   "mins": 4.8, "sigs": 5},
        {"vid": "BLR-HIS-003", "origin": "MG Road Metro",         "dest": "St. John's Hospital","mins": 6.1, "sigs": 3},
        {"vid": "BLR-HIS-004", "origin": "Jayanagar 4th Block",   "dest": "Victoria Hospital",  "mins": 3.9, "sigs": 4},
        {"vid": "BLR-HIS-005", "origin": "Indiranagar 100ft Rd",  "dest": "Manipal Hospital",   "mins": 4.4, "sigs": 3},
        {"vid": "BLR-HIS-006", "origin": "Whitefield Main Rd",    "dest": "Sakra World Hospital","mins": 5.8, "sigs": 5},
    ]
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count(ActiveRescue.id)).where(
                ActiveRescue.status == RescueStatus.COMPLETED
            )
        ) or 0
        if count > 0:
            return  # already seeded
        from datetime import timedelta
        for h in HISTORY:
            import random
            hours_ago = random.randint(1, 9)
            started = datetime.utcnow() - timedelta(hours=hours_ago, minutes=random.randint(0, 59))
            rescue = ActiveRescue(
                vehicle_id=h["vid"],
                origin_label=h["origin"],
                destination_label=h["dest"],
                started_at=started,
                completed_at=started + timedelta(minutes=random.randint(8, 14)),
                status=RescueStatus.COMPLETED,
                minutes_saved=h["mins"],
                signals_cleared=h["sigs"],
            )
            session.add(rescue)
        await session.commit()
    logger.info("Historical rescue records seeded.")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
async def _seed_junctions() -> None:
    """Idempotently ensure every entry in BLR_JUNCTIONS exists in the DB."""
    async with AsyncSessionLocal() as session:
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
        await session.commit()
    logger.info(f"Junction seed: {created} new signals added ({len(BLR_JUNCTIONS)} total defined).")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("RescueRoute backend starting...")
    await init_db()
    # Clear any emergency-GREEN signals left over from a previous crash/restart
    await green_wave_service.sync_overrides_from_db()
    # Seed realistic demo data before starting broadcast loops
    await _seed_junctions()
    await _seed_historical_rescues()
    await _seed_ambient_rescues()
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
# REST — Hospitals (static reference, no DB)
# ---------------------------------------------------------------------------
@app.get("/api/hospitals", tags=["hospitals"])
async def list_hospitals() -> list[dict]:
    """Return all major Bengaluru hospitals with coordinates, type, and bed count."""
    return BLR_HOSPITALS


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
    ) or 4.2  # realistic default

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
        golden_hour_survival_rate=min(78.0 + float(avg_saved) * 2.5, 98.0),
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

    # Register rescue — use correct model field names
    async with AsyncSessionLocal() as session:
        rescue = ActiveRescue(
            vehicle_id=VEHICLE_ID,
            origin_label="Silk Board Junction",
            destination_label="Manipal Hospital",
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
                    incident_type="TRAUMA",
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


# ---------------------------------------------------------------------------
# Ambulance Dispatch — operator enters source + destination, system drives it
# ---------------------------------------------------------------------------

_dispatch_counter: int = 0
_dispatched_tasks: dict[str, asyncio.Task] = {}


async def _run_dispatched_ambulance(
    vehicle_id: str,
    rescue_id: "UUID",
    origin: tuple[float, float, str],
    destination: tuple[float, float, str],
    incident_type: str,
    speed_kmh: float,
) -> None:
    """
    Animate a manually-dispatched ambulance from origin to destination along
    a real OSRM road route, triggering the green wave as it moves.
    On arrival the rescue is marked COMPLETED and the frontend is notified.
    """
    import math
    from backend.schemas import AmbulancePositionSchema, AmbulanceUpdate, SignalUpdate

    start_time = datetime.utcnow()

    # ── 1. Fetch OSRM road geometry ──────────────────────────────────────────
    road_pts = await _fetch_osrm_road_coords([origin, destination])

    if not road_pts or len(road_pts) < 2:
        # Fall back to straight-line waypoints
        road_pts = [
            (origin[0], origin[1]),
            (destination[0], destination[1]),
        ]

    # ── 2. Build cumulative-distance lookup table ─────────────────────────────
    cum: list[float] = [0.0]
    for k in range(1, len(road_pts)):
        cum.append(
            cum[-1]
            + _haversine_m(
                road_pts[k - 1][0], road_pts[k - 1][1],
                road_pts[k][0],     road_pts[k][1],
            )
        )
    total_dist_m = cum[-1]

    # ── 3. Drive the ambulance tick-by-tick at 1 Hz ──────────────────────────
    speed_ms = speed_kmh * 1000.0 / 3600.0
    TICK = 1.0
    n_ticks = max(2, int(total_dist_m / (speed_ms * TICK)))
    all_pts_ll = [(p[0], p[1]) for p in road_pts]

    for t in range(n_ticks):
        target_dist = (t / n_ticks) * total_dist_m

        # Interpolate position
        seg = 0
        for i in range(1, len(cum)):
            if cum[i] >= target_dist:
                seg = i - 1
                break
        else:
            seg = len(cum) - 2

        frac = (
            (target_dist - cum[seg]) / (cum[seg + 1] - cum[seg])
            if cum[seg + 1] > cum[seg]
            else 0.0
        )
        lat = road_pts[seg][0] + frac * (road_pts[seg + 1][0] - road_pts[seg][0])
        lng = road_pts[seg][1] + frac * (road_pts[seg + 1][1] - road_pts[seg][1])

        # Heading
        dlat = road_pts[seg + 1][0] - road_pts[seg][0]
        dlng = road_pts[seg + 1][1] - road_pts[seg][1]
        hdg = (math.degrees(math.atan2(dlng, dlat)) + 360) % 360

        # Remaining route as [[lng, lat], …]
        remaining_start = target_dist
        remaining_pts: list[list[float]] = []
        for i, pt in enumerate(all_pts_ll):
            if cum[i] >= remaining_start:
                remaining_pts.append([pt[1], pt[0]])

        eta_secs = max(0, int((total_dist_m - target_dist) / speed_ms))

        pos = AmbulancePositionSchema(
            vehicle_id=vehicle_id,
            lat=lat,
            lng=lng,
            speed_kmh=speed_kmh,
            heading=hdg,
            mission="TO_HOSPITAL",
            origin=origin[2],
            destination=destination[2],
            incident_type=incident_type,
            eta_seconds=eta_secs,
            route_polyline=remaining_pts,
        )

        await manager.broadcast(
            AmbulanceUpdate(type="AMBULANCE_UPDATE", ambulance=pos).model_dump(),
            "frontend",
        )

        # Green-wave service
        try:
            trigger, restored = await green_wave_service.process_ambulance_update(pos)
            for sig in restored:
                await manager.broadcast(
                    SignalUpdate(signal=sig).model_dump(), "frontend"
                )
            if trigger:
                await manager.broadcast(trigger.model_dump(), "frontend")
        except Exception as exc:
            logger.debug(f"[DISPATCH][{vehicle_id}] green wave err: {exc}")

        await asyncio.sleep(TICK)

    # ── 4. Ambulance arrived ─────────────────────────────────────────────────
    elapsed_min = (datetime.utcnow() - start_time).total_seconds() / 60.0
    # Baseline: Bengaluru city congestion at ~25 km/h without green wave
    baseline_min = (total_dist_m / 1000.0) / 25.0 * 60.0
    minutes_saved = max(0.0, round(baseline_min - elapsed_min, 1))

    # Mark rescue COMPLETED in DB and restore any remaining green signals
    async with AsyncSessionLocal() as session:
        rescue = await session.get(ActiveRescue, rescue_id)
        if rescue:
            rescue.status = RescueStatus.COMPLETED
            rescue.completed_at = datetime.utcnow()
            rescue.minutes_saved = minutes_saved
            await session.commit()

        # Restore any signals that are still GREEN for this vehicle
        from sqlalchemy import update as sa_update
        from backend.models import SignalStatus
        await session.execute(
            sa_update(TrafficLight)
            .where(TrafficLight.emergency_override == True)  # noqa: E712
            .values(
                current_status=SignalStatus.RED,
                emergency_override=False,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()

    # Notify frontend: ambulance completed its journey
    await manager.broadcast(
        AmbulanceCompleted(
            vehicle_id=vehicle_id,
            minutes_saved=minutes_saved,
            destination_label=destination[2],
        ).model_dump(),
        "frontend",
    )

    _dispatched_tasks.pop(vehicle_id, None)
    logger.info(
        f"[DISPATCH] {vehicle_id} arrived at {destination[2]}. "
        f"Saved {minutes_saved:.1f} min."
    )


@app.post("/api/dispatch", response_model=DispatchResponse, tags=["dispatch"])
async def dispatch_ambulance(req: DispatchRequest) -> DispatchResponse:
    """
    Create a new ambulance dispatch: compute OSRM route, create rescue record,
    and start animating the vehicle while triggering the green wave.
    """
    global _dispatch_counter
    _dispatch_counter += 1

    vehicle_id = req.vehicle_id or f"BLR-DISP-{_dispatch_counter:03d}"

    if vehicle_id in _dispatched_tasks and not _dispatched_tasks[vehicle_id].done():
        raise HTTPException(400, detail=f"{vehicle_id} is already on a mission.")

    # ── Pre-compute route for ETA and route_points ────────────────────────────
    origin = (req.origin_lat, req.origin_lng, req.origin_label)
    destination = (req.destination_lat, req.destination_lng, req.destination_label)

    road_pts = await _fetch_osrm_road_coords([origin, destination])
    if not road_pts or len(road_pts) < 2:
        road_pts = [
            (req.origin_lat, req.origin_lng),
            (req.destination_lat, req.destination_lng),
        ]

    total_dist_m = sum(
        _haversine_m(
            road_pts[k - 1][0], road_pts[k - 1][1],
            road_pts[k][0],     road_pts[k][1],
        )
        for k in range(1, len(road_pts))
    )
    eta_seconds = max(1, int(total_dist_m / (req.speed_kmh * 1000 / 3600)))

    # ── Create ActiveRescue record ────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        rescue = ActiveRescue(
            vehicle_id=vehicle_id,
            origin_lat=req.origin_lat,
            origin_lng=req.origin_lng,
            origin_label=req.origin_label,
            destination_lat=req.destination_lat,
            destination_lng=req.destination_lng,
            destination_label=req.destination_label,
            status=RescueStatus.ACTIVE,
            started_at=datetime.utcnow(),
        )
        session.add(rescue)
        await session.commit()
        await session.refresh(rescue)
        rescue_id = rescue.id

    # ── Launch async task ─────────────────────────────────────────────────────
    task = asyncio.create_task(
        _run_dispatched_ambulance(
            vehicle_id=vehicle_id,
            rescue_id=rescue_id,
            origin=origin,
            destination=destination,
            incident_type=req.incident_type,
            speed_kmh=req.speed_kmh,
        )
    )
    _dispatched_tasks[vehicle_id] = task

    logger.info(
        f"[DISPATCH] {vehicle_id} dispatched: {req.origin_label} → "
        f"{req.destination_label} | ETA {eta_seconds}s | {len(road_pts)} pts"
    )

    return DispatchResponse(
        vehicle_id=vehicle_id,
        rescue_id=str(rescue_id),
        origin_label=req.origin_label,
        destination_label=req.destination_label,
        incident_type=req.incident_type,
        eta_seconds=eta_seconds,
        route_points=len(road_pts),
        status="dispatched",
    )


@app.delete("/api/dispatch/{vehicle_id}", tags=["dispatch"])
async def cancel_dispatch(vehicle_id: str) -> dict:
    """Cancel an in-progress dispatch."""
    task = _dispatched_tasks.get(vehicle_id)
    if task and not task.done():
        task.cancel()
        _dispatched_tasks.pop(vehicle_id, None)
        return {"status": "cancelled", "vehicle_id": vehicle_id}
    return {"status": "not_found", "vehicle_id": vehicle_id}


@app.get("/api/dispatch", tags=["dispatch"])
async def list_dispatches() -> list[dict]:
    """List currently active manual dispatches."""
    return [
        {"vehicle_id": vid, "running": not task.done()}
        for vid, task in _dispatched_tasks.items()
    ]

