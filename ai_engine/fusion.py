"""
Sensor Fusion Module.

Gate logic (from spec)
──────────────────────
  Positive detection  ←→  (vision_confidence > 0.80)  OR  (acoustic_confidence > 0.85)

When a positive detection fires, the FusionEngine emits an AMBULANCE_UPDATE
WebSocket message to the backend, which then runs the Green-Wave Service.

Additional hardening
─────────────────────
• Temporal smoothing: a rolling window of the last N detections is kept;
  confidence is the exponentially-weighted moving average (EWMA) to prevent
  single noisy frames from triggering false positives.
• Cooldown gate: once a positive detection has been emitted, a configurable
  cooldown (default 3 s) must elapse before another can fire — prevents
  flood-triggering the backend on consecutive frames.
• Simulation / stub mode: when `simulation_mode=True`, the fusion engine
  accepts injected position ticks directly (used by simulate_blr.py).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
from loguru import logger

from ai_engine.acoustics import (
    AcousticDetection,
    MicrophoneStream,
    WINDOW_SAMPLES,
    analyse_audio_window,
)
from ai_engine.vision import (
    CameraStream,
    VisionDetection,
    VisionResult,
    infer_frame,
)

# ---------------------------------------------------------------------------
# Configurable thresholds (mirror .env values; can be overridden at runtime)
# ---------------------------------------------------------------------------
VISION_GATE: float = 0.80
ACOUSTIC_GATE: float = 0.85

# EWMA smoothing factor (α): higher = more responsive, less smooth
EWMA_ALPHA: float = 0.4

# Number of recent detections to keep in the rolling window
WINDOW_SIZE: int = 5

# Minimum seconds between consecutive positive-detection emissions
COOLDOWN_SECONDS: float = 3.0

# Backend WebSocket URL
DEFAULT_WS_URL: str = "ws://localhost:8000/ws/ai_engine"

# Vehicle ID reported by this AI engine node
DEFAULT_VEHICLE_ID: str = "BLR-AMB-001"


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------
@dataclass
class FusionState:
    """Rolling state for one camera+microphone pair."""
    camera_id: str
    microphone_id: str

    # EWMA-smoothed confidence scores
    vision_ewma: float = 0.0
    acoustic_ewma: float = 0.0

    # Recent raw detections
    vision_window: deque = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))
    acoustic_window: deque = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))

    # Cooldown bookkeeping
    last_emission_ts: float = 0.0

    def update_vision(self, confidence: float) -> None:
        self.vision_window.append(confidence)
        self.vision_ewma = EWMA_ALPHA * confidence + (1 - EWMA_ALPHA) * self.vision_ewma

    def update_acoustic(self, confidence: float) -> None:
        self.acoustic_window.append(confidence)
        self.acoustic_ewma = EWMA_ALPHA * confidence + (1 - EWMA_ALPHA) * self.acoustic_ewma

    @property
    def gate_open(self) -> bool:
        """True if the fused signal exceeds the detection threshold."""
        return (
            self.vision_ewma > VISION_GATE
            or self.acoustic_ewma > ACOUSTIC_GATE
        )

    @property
    def cooldown_elapsed(self) -> bool:
        return (time.monotonic() - self.last_emission_ts) >= COOLDOWN_SECONDS

    @property
    def should_emit(self) -> bool:
        return self.gate_open and self.cooldown_elapsed


@dataclass
class FusionResult:
    """Published detection event."""
    vision_confidence: float
    acoustic_confidence: float
    vehicle_detected: bool
    vehicle_label: str               # "ambulance" | "fire_truck"
    camera_id: str
    microphone_id: str
    fused_timestamp: float
    trigger_reason: str              # "VISION" | "ACOUSTIC" | "BOTH"

    @property
    def trigger_reason_label(self) -> str:
        v = self.vision_confidence > VISION_GATE
        a = self.acoustic_confidence > ACOUSTIC_GATE
        if v and a:
            return "BOTH"
        if v:
            return "VISION"
        return "ACOUSTIC"


# ---------------------------------------------------------------------------
# WebSocket connection helper
# ---------------------------------------------------------------------------
class BackendWSClient:
    """
    Persistent WebSocket connection to the FastAPI backend.
    Reconnects automatically with exponential back-off.
    """

    def __init__(self, url: str = DEFAULT_WS_URL) -> None:
        self._url = url
        self._ws = None
        self._connected = False

    async def connect(self) -> None:
        backoff = 1.0
        while True:
            try:
                self._ws = await websockets.connect(self._url, ping_interval=20)
                self._connected = True
                logger.info(f"FusionEngine connected to backend: {self._url}")
                return
            except Exception as exc:
                logger.warning(f"Backend WS connect failed ({exc}), retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def send(self, message: dict) -> None:
        if not self._connected or self._ws is None:
            await self.connect()
        try:
            await self._ws.send(json.dumps(message, default=str))  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning(f"Backend WS send failed: {exc}, reconnecting…")
            self._connected = False
            await self.connect()
            await self._ws.send(json.dumps(message, default=str))  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()


# ---------------------------------------------------------------------------
# Fusion Engine
# ---------------------------------------------------------------------------
class FusionEngine:
    """
    Orchestrates one or more camera/microphone pairs.

    Usage (live mode)
    -----------------
        engine = FusionEngine(ws_url="ws://localhost:8000/ws/ai_engine")
        await engine.run_forever(camera_source=0, vehicle_id="BLR-AMB-001")

    Usage (simulation / test mode)
    --------------------------------
        engine = FusionEngine(simulation_mode=True)
        await engine.inject_position(lat=12.917, lng=77.623, speed_kmh=40)
    """

    def __init__(
        self,
        ws_url: str = DEFAULT_WS_URL,
        simulation_mode: bool = False,
        vehicle_id: str = DEFAULT_VEHICLE_ID,
    ) -> None:
        self._ws_client = BackendWSClient(ws_url)
        self._simulation_mode = simulation_mode
        self._vehicle_id = vehicle_id
        self._state = FusionState(camera_id="cam_0", microphone_id="mic_0")

    # ------------------------------------------------------------------
    # Position injection (simulation / GPS feed mode)
    # ------------------------------------------------------------------
    async def inject_position(
        self,
        lat: float,
        lng: float,
        speed_kmh: float = 40.0,
        heading: float = 0.0,
    ) -> None:
        """
        Directly emit an AMBULANCE_UPDATE message to the backend.
        Used by simulate_blr.py and any external GPS feed.
        """
        message = {
            "type": "AMBULANCE_UPDATE",
            "ambulance": {
                "vehicle_id": self._vehicle_id,
                "lat": lat,
                "lng": lng,
                "speed_kmh": speed_kmh,
                "heading": heading,
            },
        }
        await self._ws_client.send(message)
        logger.debug(f"Position injected  vehicle={self._vehicle_id}  lat={lat}  lng={lng}")

    # ------------------------------------------------------------------
    # Core fusion loop (live camera + microphone)
    # ------------------------------------------------------------------
    async def run_forever(
        self,
        camera_source: int | str = 0,
        audio_device: Optional[int] = None,
    ) -> None:
        """
        Starts camera capture, microphone capture, and runs the inference
        loop until cancelled.
        """
        await self._ws_client.connect()

        camera = CameraStream(camera_source)
        mic = MicrophoneStream(device=audio_device, microphone_id="mic_0")

        camera.start()
        mic.start()

        logger.info(
            f"FusionEngine running  vehicle={self._vehicle_id}"
            f"  camera={camera_source}  simulation={self._simulation_mode}"
        )

        try:
            await asyncio.gather(
                self._vision_loop(camera),
                self._acoustic_loop(mic),
            )
        except asyncio.CancelledError:
            logger.info("FusionEngine shutting down…")
        finally:
            camera.stop()
            mic.stop()
            await self._ws_client.close()

    async def _vision_loop(self, camera: CameraStream) -> None:
        """Continuously grab frames and update the fusion state."""
        while True:
            frame = camera.get_latest_frame()
            if frame is None:
                await asyncio.sleep(0.033)  # ~30 fps target
                continue

            result: VisionResult = await infer_frame(frame, camera_id="cam_0")
            best: Optional[VisionDetection] = result.best_detection

            confidence = best.confidence if best else 0.0
            self._state.update_vision(confidence)

            if self._state.should_emit:
                label = best.label if best else "ambulance"
                await self._emit_detection(label)

            await asyncio.sleep(0.033)

    async def _acoustic_loop(self, mic: MicrophoneStream) -> None:
        """Continuously process audio windows and update the fusion state."""
        while True:
            audio = await mic.get_window()
            if audio is None:
                continue

            detection: AcousticDetection = await analyse_audio_window(
                audio, microphone_id="mic_0"
            )
            self._state.update_acoustic(detection.siren_confidence)

            if self._state.should_emit:
                await self._emit_detection("ambulance")

            await asyncio.sleep(0.0)

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------
    async def _emit_detection(self, label: str) -> None:
        """
        Fire a positive detection to the backend.
        Records the timestamp to enforce the cooldown gate.
        """
        self._state.last_emission_ts = time.monotonic()

        reason = "BOTH"
        if self._state.vision_ewma > VISION_GATE and self._state.acoustic_ewma <= ACOUSTIC_GATE:
            reason = "VISION"
        elif self._state.acoustic_ewma > ACOUSTIC_GATE and self._state.vision_ewma <= VISION_GATE:
            reason = "ACOUSTIC"

        result = FusionResult(
            vision_confidence=round(self._state.vision_ewma, 4),
            acoustic_confidence=round(self._state.acoustic_ewma, 4),
            vehicle_detected=True,
            vehicle_label=label,
            camera_id=self._state.camera_id,
            microphone_id=self._state.microphone_id,
            fused_timestamp=time.monotonic(),
            trigger_reason=reason,
        )

        logger.info(
            f"POSITIVE DETECTION  vehicle={self._vehicle_id}"
            f"  vision={result.vision_confidence:.3f}"
            f"  acoustic={result.acoustic_confidence:.3f}"
            f"  reason={reason}"
        )

        # The backend expects an AMBULANCE_UPDATE; the green-wave service
        # will pull the stored position from the DB.
        message = {
            "type": "AMBULANCE_UPDATE",
            "ambulance": {
                "vehicle_id": self._vehicle_id,
                "lat": 0.0,    # position supplied by GPS/simulator separately
                "lng": 0.0,
                "speed_kmh": 0.0,
                "heading": 0.0,
                "_detection_meta": {
                    "vision_confidence": result.vision_confidence,
                    "acoustic_confidence": result.acoustic_confidence,
                    "trigger_reason": reason,
                    "label": label,
                },
            },
        }
        await self._ws_client.send(message)

    # ------------------------------------------------------------------
    # Standalone test: run fusion on a single injected frame + audio window
    # ------------------------------------------------------------------
    async def evaluate_once(
        self,
        frame=None,
        audio=None,
    ) -> FusionResult:
        """
        One-shot evaluation for unit testing.
        Pass numpy arrays for frame (H×W×3 BGR) and/or audio (float32, 16 kHz).
        """
        import numpy as np

        if frame is not None:
            result: VisionResult = await infer_frame(frame, camera_id="cam_0")
            best = result.best_detection
            self._state.update_vision(best.confidence if best else 0.0)

        if audio is not None:
            det: AcousticDetection = await analyse_audio_window(audio)
            self._state.update_acoustic(det.siren_confidence)

        label = "ambulance"
        if frame is not None:
            result_v: VisionResult = await infer_frame(frame)
            if result_v.best_detection:
                label = result_v.best_detection.label

        reason = "BOTH"
        if self._state.vision_ewma > VISION_GATE and self._state.acoustic_ewma <= ACOUSTIC_GATE:
            reason = "VISION"
        elif self._state.acoustic_ewma > ACOUSTIC_GATE and self._state.vision_ewma <= VISION_GATE:
            reason = "ACOUSTIC"

        return FusionResult(
            vision_confidence=round(self._state.vision_ewma, 4),
            acoustic_confidence=round(self._state.acoustic_ewma, 4),
            vehicle_detected=self._state.gate_open,
            vehicle_label=label,
            camera_id=self._state.camera_id,
            microphone_id=self._state.microphone_id,
            fused_timestamp=time.monotonic(),
            trigger_reason=reason,
        )

    async def close(self) -> None:
        await self._ws_client.close()


# ---------------------------------------------------------------------------
# CLI entry point  (python -m ai_engine.fusion --vehicle BLR-AMB-001)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RescueRoute AI Fusion Engine")
    parser.add_argument("--vehicle", default=DEFAULT_VEHICLE_ID, help="Vehicle ID")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="Backend WS URL")
    parser.add_argument("--camera", default=0, help="Camera index or RTSP URL")
    parser.add_argument("--audio-device", type=int, default=None, help="Microphone device index")
    args = parser.parse_args()

    engine = FusionEngine(ws_url=args.ws_url, vehicle_id=args.vehicle)

    try:
        asyncio.run(engine.run_forever(camera_source=args.camera, audio_device=args.audio_device))
    except KeyboardInterrupt:
        logger.info("Fusion engine stopped by user.")
