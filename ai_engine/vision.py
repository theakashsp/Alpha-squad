"""
Vision Module — YOLO-based NMS-free ambulance & fire-truck detector.

Design choices
──────────────
• Ultralytics YOLO with the RT-DETR (NMS-free, end-to-end transformer)
  backbone is the closest match to the "YOLO-26 NMS-free" specification.
  We fall back gracefully to YOLOv8n if the RT-DETR weight is unavailable.
• Inference runs in a dedicated thread so it never blocks the asyncio loop.
• Only COCO classes matching TARGET_CLASSES are forwarded.
• Output is a VisionDetection dataclass consumed by fusion.py.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# COCO class IDs we care about:
#   5  = bus  (wide capture net — many Indian ambulances mis-classified here)
#   7  = truck
#   (custom fine-tuned weights will use:  0 = ambulance, 1 = fire_truck)
TARGET_CLASSES: dict[int, str] = {
    0: "ambulance",
    1: "fire_truck",
    # Fallback COCO IDs when using stock weights
    5: "ambulance",   # bus → treated as ambulance proxy
    7: "fire_truck",  # truck → treated as fire_truck proxy
}

# Minimum confidence to forward a detection to the fusion layer
MIN_CONFIDENCE: float = 0.40

# Preferred model weight names (searched in ai_engine/weights/)
WEIGHT_CANDIDATES: list[str] = [
    "rtdetr-l.pt",   # RT-DETR large  (NMS-free, transformer decoder)
    "rtdetr-x.pt",   # RT-DETR extra-large
    "yolov8n.pt",    # Nano fallback  (downloads automatically)
]

WEIGHTS_DIR = Path(__file__).parent / "weights"
WEIGHTS_DIR.mkdir(exist_ok=True)

# Frame resize for inference (width × height)
INFER_SIZE: tuple[int, int] = (640, 640)

# GPU device — "0" for first CUDA GPU, "cpu" for CPU-only
DEVICE: str = "cpu"


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------
@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


@dataclass
class VisionDetection:
    """Single detection event forwarded to the fusion layer."""
    label: str                          # "ambulance" | "fire_truck"
    confidence: float                   # 0.0 – 1.0
    bbox: BoundingBox
    frame_timestamp: float              # time.monotonic()
    camera_id: str = "cam_0"
    raw_class_id: int = -1
    # Enriched by fusion layer
    ambulance_detected: bool = field(init=False)

    def __post_init__(self) -> None:
        self.ambulance_detected = self.label in ("ambulance", "fire_truck")

    def is_high_confidence(self, threshold: float = 0.80) -> bool:
        return self.confidence >= threshold


@dataclass
class VisionResult:
    """Aggregated result from one inference pass over a single frame."""
    detections: list[VisionDetection]
    inference_ms: float
    frame_timestamp: float
    camera_id: str = "cam_0"

    @property
    def best_detection(self) -> Optional[VisionDetection]:
        if not self.detections:
            return None
        return max(self.detections, key=lambda d: d.confidence)

    @property
    def max_confidence(self) -> float:
        return self.best_detection.confidence if self.best_detection else 0.0

    @property
    def emergency_vehicle_detected(self) -> bool:
        return any(d.ambulance_detected for d in self.detections)


# ---------------------------------------------------------------------------
# Model loader (lazy, thread-safe singleton)
# ---------------------------------------------------------------------------
class _ModelRegistry:
    _instance: Optional["_ModelRegistry"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._model_name: str = ""

    @classmethod
    def get(cls) -> "_ModelRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def load(self) -> None:
        """Load the best available YOLO/RT-DETR model weight."""
        from ultralytics import RTDETR, YOLO

        for candidate in WEIGHT_CANDIDATES:
            weight_path = WEIGHTS_DIR / candidate
            try:
                if "rtdetr" in candidate:
                    # RT-DETR is NMS-free by design
                    self._model = RTDETR(str(weight_path) if weight_path.exists() else candidate)
                else:
                    self._model = YOLO(str(weight_path) if weight_path.exists() else candidate)
                self._model_name = candidate
                logger.info(f"Vision model loaded: {candidate}  device={DEVICE}")
                return
            except Exception as exc:
                logger.warning(f"Could not load {candidate}: {exc}")

        raise RuntimeError(
            "No YOLO/RT-DETR model could be loaded. "
            "Place a weight file in ai_engine/weights/ or ensure internet access for auto-download."
        )

    @property
    def model(self):
        if self._model is None:
            self.load()
        return self._model

    @property
    def model_name(self) -> str:
        return self._model_name


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------
def _preprocess_frame(frame: np.ndarray) -> np.ndarray:
    """
    Resize to INFER_SIZE and apply adaptive histogram equalisation on the
    luminance channel to improve detection in low-light / night conditions.
    """
    resized = cv2.resize(frame, INFER_SIZE, interpolation=cv2.INTER_LINEAR)
    # CLAHE on L channel of LAB to enhance contrast without blowing highlights
    lab = cv2.cvtColor(resized, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _run_inference(frame: np.ndarray, camera_id: str = "cam_0") -> VisionResult:
    """
    Synchronous inference — runs inside a ThreadPoolExecutor.

    RT-DETR produces predictions without NMS:
      results[0].boxes contains Boxes objects with .xyxy, .conf, .cls
    """
    registry = _ModelRegistry.get()
    processed = _preprocess_frame(frame)

    t0 = time.perf_counter()
    results = registry.model.predict(
        processed,
        device=DEVICE,
        verbose=False,
        conf=MIN_CONFIDENCE,
    )
    inference_ms = (time.perf_counter() - t0) * 1000

    detections: list[VisionDetection] = []
    ts = time.monotonic()

    for result in results:
        if result.boxes is None:
            continue
        boxes = result.boxes
        # .xyxy: (N, 4) tensor, .conf: (N,), .cls: (N,)
        for xyxy, conf, cls_id in zip(
            boxes.xyxy.tolist(),
            boxes.conf.tolist(),
            boxes.cls.tolist(),
        ):
            cls_int = int(cls_id)
            if cls_int not in TARGET_CLASSES:
                continue
            label = TARGET_CLASSES[cls_int]
            detections.append(
                VisionDetection(
                    label=label,
                    confidence=float(conf),
                    bbox=BoundingBox(*xyxy),
                    frame_timestamp=ts,
                    camera_id=camera_id,
                    raw_class_id=cls_int,
                )
            )

    logger.debug(
        f"Vision inference  camera={camera_id}  detections={len(detections)}"
        f"  inference_ms={inference_ms:.1f}"
    )
    return VisionResult(
        detections=detections,
        inference_ms=inference_ms,
        frame_timestamp=ts,
        camera_id=camera_id,
    )


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vision_infer")


async def infer_frame(
    frame: np.ndarray,
    camera_id: str = "cam_0",
) -> VisionResult:
    """
    Non-blocking async wrapper around the synchronous YOLO inference.
    Call from the fusion loop without blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    result: VisionResult = await loop.run_in_executor(
        _executor, _run_inference, frame, camera_id
    )
    return result


# ---------------------------------------------------------------------------
# Live camera reader (optional — used by fusion.py in camera mode)
# ---------------------------------------------------------------------------
class CameraStream:
    """
    Reads frames from a V4L2 / RTSP / file source in a background thread
    and exposes the latest frame via `get_latest_frame()`.
    """

    def __init__(self, source: int | str = 0) -> None:
        self._source = source
        self._cap: Optional[cv2.VideoCapture] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._cap = cv2.VideoCapture(self._source)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open video source: {self._source}")
        # Optimise buffer: keep only the freshest frame
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        logger.info(f"CameraStream started  source={self._source}")

    def _read_loop(self) -> None:
        while self._running and self._cap and self._cap.isOpened():
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._latest_frame = frame
            else:
                time.sleep(0.01)

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._cap:
            self._cap.release()
        logger.info("CameraStream stopped.")


# ---------------------------------------------------------------------------
# Annotated frame renderer (debug / demo)
# ---------------------------------------------------------------------------
def annotate_frame(frame: np.ndarray, result: VisionResult) -> np.ndarray:
    """Draw bounding boxes and confidence labels onto a frame copy."""
    out = frame.copy()
    colour_map = {
        "ambulance":  (0, 230, 118),   # green
        "fire_truck": (0, 120, 255),   # orange-blue
    }
    for det in result.detections:
        colour = colour_map.get(det.label, (200, 200, 200))
        x1, y1, x2, y2 = (
            int(det.bbox.x1), int(det.bbox.y1),
            int(det.bbox.x2), int(det.bbox.y2),
        )
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)
        label_text = f"{det.label} {det.confidence:.2f}"
        cv2.putText(
            out, label_text, (x1, max(y1 - 6, 10)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2, cv2.LINE_AA,
        )
    fps_text = f"Inf: {result.inference_ms:.0f}ms"
    cv2.putText(out, fps_text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    return out
