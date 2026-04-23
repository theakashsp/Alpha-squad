"""
WebSocket ConnectionManager.

Maintains three logical channels:
  • "frontend"   – browser dashboard clients
  • "ai_engine"  – the inference pipeline sending detection events
  • "simulator"  – the simulate_blr.py script (or real GPS feeds)

All inbound messages from ai_engine / simulator are parsed, processed by
the GreenWaveService, and the resulting GREEN_WAVE_TRIGGER is broadcast to
all connected frontend clients.
"""
import asyncio
import json
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import ValidationError

from backend.schemas import (
    AmbulanceUpdate,
    DashboardStats,
    ErrorMessage,
    GreenWaveTrigger,
    SignalUpdate,
)

if TYPE_CHECKING:
    from backend.green_wave import GreenWaveService


# Channel identifiers
CHANNEL_FRONTEND = "frontend"
CHANNEL_AI_ENGINE = "ai_engine"
CHANNEL_SIMULATOR = "simulator"

ALL_CHANNELS = {CHANNEL_FRONTEND, CHANNEL_AI_ENGINE, CHANNEL_SIMULATOR}


class ConnectionManager:
    """Thread-safe async WebSocket hub."""

    def __init__(self) -> None:
        # channel -> list of active websocket connections
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    async def connect(self, websocket: WebSocket, channel: str = CHANNEL_FRONTEND) -> None:
        if channel not in ALL_CHANNELS:
            channel = CHANNEL_FRONTEND
        await websocket.accept()
        async with self._lock:
            self._connections[channel].append(websocket)
        logger.info(f"WS connected  channel={channel}  total={self._total()}")

    async def disconnect(self, websocket: WebSocket, channel: str = CHANNEL_FRONTEND) -> None:
        async with self._lock:
            conns = self._connections.get(channel, [])
            if websocket in conns:
                conns.remove(websocket)
        logger.info(f"WS disconnected channel={channel} total={self._total()}")

    def _total(self) -> int:
        return sum(len(v) for v in self._connections.values())

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------
    async def broadcast(self, message: dict, channel: str = CHANNEL_FRONTEND) -> None:
        """Send a JSON message to all clients in a given channel."""
        payload = json.dumps(message, default=str)
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(channel, [])):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws, channel)

    async def broadcast_all(self, message: dict) -> None:
        """Broadcast to every connected client across all channels."""
        for channel in ALL_CHANNELS:
            await self.broadcast(message, channel)

    async def send_personal(self, message: dict, websocket: WebSocket) -> None:
        try:
            await websocket.send_text(json.dumps(message, default=str))
        except Exception as exc:
            logger.warning(f"Failed personal send: {exc}")

    # ------------------------------------------------------------------
    # Message router
    # ------------------------------------------------------------------
    async def handle_message(
        self,
        raw: str,
        sender: WebSocket,
        channel: str,
        green_wave_service: "GreenWaveService",
    ) -> None:
        """
        Parse an inbound WS frame and route it through the green-wave service.

        Expected inbound types:
          • AMBULANCE_UPDATE  – from simulator / ai_engine
          • SIGNAL_UPDATE     – manual signal state from ai_engine
        """
        try:
            data: dict = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_personal(
                ErrorMessage(code="PARSE_ERROR", detail="Invalid JSON").model_dump(),
                sender,
            )
            return

        msg_type: str = data.get("type", "UNKNOWN")

        try:
            if msg_type == "AMBULANCE_UPDATE":
                update = AmbulanceUpdate.model_validate(data)
                # Persist position and run green-wave logic
                trigger, restored = await green_wave_service.process_ambulance_update(
                    update.ambulance
                )
                # Broadcast restored signals (emergency_override cleared)
                for sig in restored:
                    await self.broadcast(
                        SignalUpdate(signal=sig).model_dump(), CHANNEL_FRONTEND
                    )
                if trigger:
                    await self.broadcast(trigger.model_dump(), CHANNEL_FRONTEND)
                    logger.info(
                        f"GREEN_WAVE_TRIGGER  vehicle={update.ambulance.vehicle_id}"
                        f"  signals={len(trigger.signals)}"
                    )
                # Echo position update to frontend for live map dot
                await self.broadcast(update.model_dump(), CHANNEL_FRONTEND)

            elif msg_type == "SIGNAL_UPDATE":
                update_sig = SignalUpdate.model_validate(data)
                await self.broadcast(update_sig.model_dump(), CHANNEL_FRONTEND)

            elif msg_type in ("PING", "ping"):
                # react-use-websocket heartbeat — acknowledge silently
                await self.send_personal({"type": "PONG"}, sender)

            else:
                logger.debug(f"Unhandled WS message type: {msg_type}")

        except ValidationError as exc:
            await self.send_personal(
                ErrorMessage(code="VALIDATION_ERROR", detail=str(exc)).model_dump(),
                sender,
            )

    # ------------------------------------------------------------------
    # Stats broadcast (called by background task every 5 s)
    # ------------------------------------------------------------------
    async def broadcast_stats(self, stats: DashboardStats) -> None:
        await self.broadcast(stats.model_dump(), CHANNEL_FRONTEND)


# Singleton – imported by main.py and green_wave.py
manager = ConnectionManager()
