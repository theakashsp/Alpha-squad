"""
simulate_blr.py — RescueRoute End-to-End Simulation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Simulates an ambulance journey from Silk Board Junction to Manipal Hospital
(Old Airport Road, Bengaluru) by emitting AMBULANCE_UPDATE WebSocket messages
to the RescueRoute backend.

The backend's GreenWaveService receives each position tick, runs PostGIS
ST_DWithin queries, calculates Mappls ETAs, and broadcasts GREEN_WAVE_TRIGGER
messages to all connected frontend clients — demonstrating fully automated
signal switching.

Usage
─────
    uv run python simulate_blr.py
    uv run python simulate_blr.py --speed 55 --vehicle KA-01-F-9999
    uv run python simulate_blr.py --demo --speed 80 --repeat 3

Options
────────
  --vehicle   Vehicle ID reported to the backend  (default: BLR-AMB-001)
  --speed     Cruising speed in km/h              (default: 45)
  --ws-url    Backend WebSocket URL               (default: ws://localhost:8000/ws)
  --tick-hz   Position updates per second         (default: 2)
  --repeat    How many times to run the route     (default: 1)
  --demo      Accelerate time (5× speed) for demo purposes
  --dry-run   Print coordinates only, no WS connection
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Colour codes for terminal output ──────────────────────────────────────
_RESET  = "\033[0m"
_GREEN  = "\033[92m"
_CYAN   = "\033[96m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"


def _c(text: str, code: str) -> str:
    """Wrap text in ANSI colour (skipped on non-TTY)."""
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


# ── Route definition ────────────────────────────────────────────────────────
@dataclass
class Waypoint:
    name: str
    lat: float
    lng: float
    speed_limit_kmh: float = 50.0    # posted speed; ambulance may exceed this
    notes: str = ""


# Silk Board → Koramangala → Domlur → Airport Road → Manipal Hospital
# Coordinates traced along the actual road corridor (EPSG:4326)
ROUTE: list[Waypoint] = [
    Waypoint("Silk Board Junction",         12.9172, 77.6231, 40,  "Heavy confluence; slow start"),
    Waypoint("HSR Layout 27th Main",        12.9198, 77.6310, 50),
    Waypoint("Agara Junction",              12.9245, 77.6385, 50),
    Waypoint("Sony Signal",                 12.9282, 77.6415, 50),
    Waypoint("Koramangala 5th Block",       12.9340, 77.6338, 50),
    Waypoint("Koramangala 80ft Road",       12.9395, 77.6260, 50),
    Waypoint("Ejipura Signal",              12.9411, 77.6195, 45),
    Waypoint("Jyothi Nivas College",        12.9462, 77.6215, 50),
    Waypoint("Cambridge Layout",            12.9511, 77.6280, 50),
    Waypoint("Domlur Flyover",              12.9592, 77.6386, 60,  "Open stretch; higher speed"),
    Waypoint("Domlur Service Road",         12.9614, 77.6420, 55),
    Waypoint("Airport Road / HAL 2nd St",   12.9630, 77.6450, 55),
    Waypoint("HAL Old Airport Road",        12.9650, 77.6465, 60),
    Waypoint("Manipal Hospital Gate",       12.9698, 77.6490, 20,  "Destination — slow approach"),
]

ORIGIN_NAME      = ROUTE[0].name
DESTINATION_NAME = ROUTE[-1].name

# Total straight-line distance (km) along the corridor
def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lng2 - lng1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _total_route_km() -> float:
    total = 0.0
    for i in range(len(ROUTE) - 1):
        total += _haversine_km(
            ROUTE[i].lat, ROUTE[i].lng,
            ROUTE[i + 1].lat, ROUTE[i + 1].lng,
        )
    return total


# ── Interpolation ───────────────────────────────────────────────────────────
@dataclass
class PositionTick:
    lat: float
    lng: float
    speed_kmh: float
    heading: float                   # degrees, 0 = north
    segment_name: str
    elapsed_s: float
    distance_covered_km: float
    distance_remaining_km: float


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """True bearing from point-1 to point-2 (degrees, 0 = north)."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δλ = math.radians(lng2 - lng1)
    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def interpolate_route(
    base_speed_kmh: float = 45.0,
    tick_hz: float = 2.0,
    demo_multiplier: float = 1.0,
) -> list[PositionTick]:
    """
    Generate a list of PositionTick objects at `tick_hz` resolution along
    the route, respecting per-segment speed limits.

    demo_multiplier > 1 compresses time (same positions, shorter intervals).
    """
    ticks: list[PositionTick] = []
    total_km = _total_route_km()
    elapsed = 0.0
    covered_km = 0.0
    interval_s = 1.0 / tick_hz / demo_multiplier

    # Walk segment by segment
    for seg_idx in range(len(ROUTE) - 1):
        wp_a = ROUTE[seg_idx]
        wp_b = ROUTE[seg_idx + 1]
        seg_km = _haversine_km(wp_a.lat, wp_a.lng, wp_b.lat, wp_b.lng)
        bearing = _bearing(wp_a.lat, wp_a.lng, wp_b.lat, wp_b.lng)

        # Speed for this segment: min of base_speed and posted limit (+10 % emergency)
        seg_speed = min(base_speed_kmh, wp_b.speed_limit_kmh * 1.10)
        seg_speed_ms = seg_speed / 3.6

        # How many ticks to fill this segment
        seg_duration_s = (seg_km * 1000) / seg_speed_ms
        n_ticks = max(1, int(seg_duration_s * tick_hz * demo_multiplier))

        for i in range(n_ticks):
            t = i / n_ticks
            lat  = wp_a.lat + (wp_b.lat - wp_a.lat) * t
            lng  = wp_a.lng + (wp_b.lng - wp_a.lng) * t
            dist = covered_km + seg_km * t

            ticks.append(
                PositionTick(
                    lat=lat,
                    lng=lng,
                    speed_kmh=seg_speed,
                    heading=bearing,
                    segment_name=f"{wp_a.name} → {wp_b.name}",
                    elapsed_s=elapsed + (seg_duration_s * t / demo_multiplier),
                    distance_covered_km=dist,
                    distance_remaining_km=total_km - dist,
                )
            )

        elapsed += seg_duration_s / demo_multiplier
        covered_km += seg_km

    # Final waypoint
    last = ROUTE[-1]
    ticks.append(
        PositionTick(
            lat=last.lat, lng=last.lng,
            speed_kmh=0.0, heading=0.0,
            segment_name="Arrived at " + last.name,
            elapsed_s=elapsed,
            distance_covered_km=total_km,
            distance_remaining_km=0.0,
        )
    )
    return ticks


# ── Progress bar ────────────────────────────────────────────────────────────
def _progress_bar(fraction: float, width: int = 30) -> str:
    filled = int(width * fraction)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {fraction * 100:5.1f}%"


# ── WebSocket sender ────────────────────────────────────────────────────────
async def _connect_with_retry(url: str, max_attempts: int = 15) -> object:
    """Return a connected websockets client, retrying with back-off."""
    try:
        import websockets
    except ImportError:
        print(_c("  ✗ 'websockets' not installed. Run: uv sync", _RED))
        sys.exit(1)

    for attempt in range(1, max_attempts + 1):
        try:
            ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
            return ws
        except Exception as exc:
            wait = min(2 ** attempt, 20)
            print(
                _c(f"  ↻ Backend not ready (attempt {attempt}/{max_attempts}): {exc}", _YELLOW),
                end="\r",
                flush=True,
            )
            await asyncio.sleep(wait)
    print(_c(f"\n  ✗ Could not connect to {url} after {max_attempts} attempts.", _RED))
    sys.exit(1)


async def run_simulation(
    vehicle_id: str,
    base_speed_kmh: float,
    ws_url: str,
    tick_hz: float,
    demo: bool,
    dry_run: bool,
    repeat: int,
) -> None:
    demo_multiplier = 5.0 if demo else 1.0
    interval_s = 1.0 / tick_hz           # real-wall-clock interval

    total_km = _total_route_km()
    ticks = interpolate_route(base_speed_kmh, tick_hz, demo_multiplier)

    print()
    print(_c("━" * 60, _BOLD))
    print(_c("  🚑  RescueRoute — Bengaluru Simulation", _BOLD))
    print(_c("━" * 60, _BOLD))
    print(f"  Route    : {_c(ORIGIN_NAME, _CYAN)} → {_c(DESTINATION_NAME, _GREEN)}")
    print(f"  Vehicle  : {_c(vehicle_id, _YELLOW)}")
    print(f"  Speed    : {base_speed_kmh} km/h{' (5× demo mode)' if demo else ''}")
    print(f"  Distance : {total_km:.2f} km  |  {len(ticks)} ticks  |  {tick_hz} Hz")
    print(f"  Backend  : {_c(ws_url if not dry_run else 'DRY-RUN (no connection)', _DIM)}")
    print(_c("━" * 60, _BOLD))

    ws = None
    if not dry_run:
        print(f"\n  Connecting to backend…", end="", flush=True)
        ws = await _connect_with_retry(ws_url)
        print(_c("  ✓ Connected\n", _GREEN))

        # Register a new rescue via REST (best-effort)
        try:
            import httpx
            origin  = ROUTE[0]
            dest    = ROUTE[-1]
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    "http://localhost:8000/api/rescues",
                    json={
                        "vehicle_id":        vehicle_id,
                        "origin_lat":        origin.lat,
                        "origin_lng":        origin.lng,
                        "origin_label":      origin.name,
                        "destination_lat":   dest.lat,
                        "destination_lng":   dest.lng,
                        "destination_label": dest.name,
                    },
                )
                if resp.status_code == 201:
                    rescue_id = resp.json().get("id", "")
                    print(_c(f"  ✓ Rescue registered  id={rescue_id}\n", _GREEN))
        except Exception as exc:
            print(_c(f"  ⚠ Rescue registration skipped: {exc}\n", _YELLOW))

    try:
        for run_no in range(1, repeat + 1):
            if repeat > 1:
                print(_c(f"\n── Run {run_no}/{repeat} ──", _BOLD))

            run_start = time.monotonic()
            prev_segment = ""

            for idx, tick in enumerate(ticks):
                t_wall_start = time.monotonic()

                # ── Terminal output ──────────────────────────────────────
                fraction = tick.distance_covered_km / total_km
                bar = _progress_bar(fraction)

                if tick.segment_name != prev_segment:
                    print()
                    print(_c(f"  ▶  {tick.segment_name}", _CYAN))
                    prev_segment = tick.segment_name

                print(
                    f"  {bar}  "
                    f"lat={tick.lat:.6f} lng={tick.lng:.6f}  "
                    f"{_c(f'{tick.speed_kmh:.0f} km/h', _YELLOW)}  "
                    f"hdg={tick.heading:.0f}°  "
                    f"η={tick.distance_remaining_km:.2f} km",
                    end="\r",
                    flush=True,
                )

                # ── WebSocket emit ───────────────────────────────────────
                if ws is not None:
                    message = {
                        "type": "AMBULANCE_UPDATE",
                        "ambulance": {
                            "vehicle_id": vehicle_id,
                            "lat":        tick.lat,
                            "lng":        tick.lng,
                            "speed_kmh":  tick.speed_kmh,
                            "heading":    tick.heading,
                        },
                    }
                    try:
                        await ws.send(json.dumps(message))

                        # Non-blocking receive — print any GREEN_WAVE_TRIGGER back
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                            resp_data = json.loads(raw)
                            if resp_data.get("type") == "GREEN_WAVE_TRIGGER":
                                n_sigs = len(resp_data.get("signals", []))
                                print(
                                    f"\n  {_c('🟢 GREEN_WAVE_TRIGGER', _GREEN)}  "
                                    f"{_c(vehicle_id, _YELLOW)}  "
                                    f"{n_sigs} signal(s) cleared  "
                                    f"ETA {resp_data.get('eta_to_destination_seconds', '?')}s"
                                )
                        except asyncio.TimeoutError:
                            pass

                    except Exception as exc:
                        print(_c(f"\n  ⚠ WS send error: {exc} — reconnecting…", _YELLOW))
                        ws = await _connect_with_retry(ws_url, max_attempts=5)

                # ── Sleep to maintain tick_hz ────────────────────────────
                elapsed = time.monotonic() - t_wall_start
                sleep_s = max(0.0, interval_s - elapsed)
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)

            # Newline after last \r
            print()

            run_elapsed = time.monotonic() - run_start
            print(
                f"\n  {_c('✓ Journey complete', _GREEN)}"
                f"  wall-clock {run_elapsed:.1f}s"
                f"  sim-time ~{ticks[-1].elapsed_s:.0f}s"
            )

            if run_no < repeat:
                print(_c("  ↺  Restarting in 3 s…", _DIM))
                await asyncio.sleep(3.0)

    finally:
        if ws is not None:
            # Mark rescue completed
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    rescues = await client.get(
                        "http://localhost:8000/api/rescues"
                    )
                    if rescues.status_code == 200:
                        active = [
                            r for r in rescues.json()
                            if r["vehicle_id"] == vehicle_id and r["status"] == "ACTIVE"
                        ]
                        if active:
                            rescue_id = active[0]["id"]
                            await client.patch(
                                f"http://localhost:8000/api/rescues/{rescue_id}/complete"
                            )
                            print(_c(f"\n  ✓ Rescue {rescue_id[:8]}… marked COMPLETED", _GREEN))
            except Exception:
                pass

            await ws.close()
            print(_c("  Connection closed.\n", _DIM))


# ── Dry-run mode (no WS) ────────────────────────────────────────────────────
async def dry_run_print(base_speed_kmh: float, tick_hz: float) -> None:
    ticks = interpolate_route(base_speed_kmh, tick_hz)
    print(f"\n{'idx':>5}  {'lat':>10}  {'lng':>10}  {'spd':>6}  {'hdg':>6}  segment")
    print("─" * 75)
    for i, t in enumerate(ticks[::max(1, len(ticks) // 20)]):  # print ~20 sample rows
        print(
            f"{i:>5}  {t.lat:>10.6f}  {t.lng:>10.6f}  "
            f"{t.speed_kmh:>5.0f}k  {t.heading:>5.0f}°  {t.segment_name}"
        )
    print(f"\n  Total ticks: {len(ticks)}")


# ── CLI ─────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RescueRoute — Bengaluru ambulance journey simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--vehicle",  default="BLR-AMB-001",               help="Vehicle ID")
    p.add_argument("--speed",    type=float, default=45.0,             help="Base speed km/h")
    p.add_argument("--ws-url",   default="ws://localhost:8000/ws",     help="Backend WebSocket URL")
    p.add_argument("--tick-hz",  type=float, default=2.0,              help="Updates per second")
    p.add_argument("--repeat",   type=int,   default=1,                help="Loop count")
    p.add_argument("--demo",     action="store_true",                  help="5× time compression")
    p.add_argument("--dry-run",  action="store_true",                  help="Print ticks only")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.dry_run:
        asyncio.run(dry_run_print(args.speed, args.tick_hz))
    else:
        asyncio.run(
            run_simulation(
                vehicle_id=args.vehicle,
                base_speed_kmh=args.speed,
                ws_url=args.ws_url,
                tick_hz=args.tick_hz,
                demo=args.demo,
                dry_run=False,
                repeat=args.repeat,
            )
        )
