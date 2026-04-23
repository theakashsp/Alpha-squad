"""
Acoustics Module — YAMNet-based siren detector tuned for Bengaluru's soundscape.

Pipeline
────────
1.  Audio is captured from the microphone (or injected as a NumPy array for
    testing) in 0.96-second windows — the native YAMNet input length.
2.  Librosa extracts spectral features to isolate energy in the 1000–3000 Hz
    siren band *before* YAMNet inference, giving us a cheap pre-filter that
    rejects pure traffic noise and horn blasts outside the target band.
3.  YAMNet (loaded from TF-Hub) produces a 521-class embedding.  We map the
    relevant "Siren", "Emergency vehicle", and "Ambulance" classes to a
    single siren_confidence score.
4.  The final acoustic confidence = weighted(yamnet_confidence, band_energy_ratio).
5.  AcousticDetection is forwarded to fusion.py for gating.

YAMNet class IDs relevant to us (COCO-AudioSet taxonomy):
  388 – Siren
  389 – Civil defense siren
  400 – Ambulance (siren)
  401 – Fire engine, fire truck (siren)
  402 – Ambulance (horn)
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import librosa
import numpy as np
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAMPLE_RATE: int = 16_000          # YAMNet native sample rate
WINDOW_SECONDS: float = 0.96       # YAMNet patch length
HOP_SECONDS: float = 0.48          # overlap stride
WINDOW_SAMPLES: int = int(SAMPLE_RATE * WINDOW_SECONDS)

# Emergency siren frequency range (Hz)
SIREN_FREQ_LOW: float = 1_000.0
SIREN_FREQ_HIGH: float = 3_000.0

# YAMNet AudioSet class IDs for siren/emergency sounds
SIREN_CLASS_IDS: set[int] = {388, 389, 400, 401, 402}

# Bengaluru ambient noise profile: horn blasts peak at 400–800 Hz;
# engine rumble is < 300 Hz.  Our band-pass pre-filter targets 1–3 kHz only.
AMBIENT_NOISE_FLOOR_DB: float = -35.0   # typical Bengaluru intersection

# Weight for combining YAMNet score vs. band-energy ratio
YAMNET_WEIGHT: float = 0.65
BAND_ENERGY_WEIGHT: float = 0.35

YAMNET_MODEL_URL = "https://tfhub.dev/google/yamnet/1"

# Minimum score to report any detection
MIN_ACOUSTIC_CONFIDENCE: float = 0.30


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------
@dataclass
class AcousticDetection:
    """Single acoustic detection event forwarded to the fusion layer."""
    siren_confidence: float         # 0.0 – 1.0  (fused score)
    yamnet_confidence: float        # raw YAMNet top-siren class score
    band_energy_ratio: float        # fraction of spectral energy in 1–3 kHz
    dominant_freq_hz: float         # peak frequency within siren band
    window_timestamp: float         # time.monotonic() when window was captured
    microphone_id: str = "mic_0"

    def is_high_confidence(self, threshold: float = 0.85) -> bool:
        return self.siren_confidence >= threshold

    @property
    def siren_detected(self) -> bool:
        return self.siren_confidence >= MIN_ACOUSTIC_CONFIDENCE


# ---------------------------------------------------------------------------
# YAMNet model loader (lazy singleton, loaded once)
# ---------------------------------------------------------------------------
class _YAMNetRegistry:
    _instance: Optional["_YAMNetRegistry"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._class_names: list[str] = []

    @classmethod
    def get(cls) -> "_YAMNetRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def load(self) -> None:
        import tensorflow_hub as hub  # type: ignore

        logger.info(f"Loading YAMNet from TF-Hub: {YAMNET_MODEL_URL}")
        self._model = hub.load(YAMNET_MODEL_URL)
        # Load AudioSet class map to get human-readable names
        try:
            import csv
            import io
            import urllib.request

            csv_url = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
            with urllib.request.urlopen(csv_url, timeout=10) as resp:
                reader = csv.DictReader(io.TextIOWrapper(resp))
                self._class_names = [row["display_name"] for row in reader]
        except Exception:
            self._class_names = [f"class_{i}" for i in range(521)]
        logger.info("YAMNet loaded successfully.")

    @property
    def model(self):
        if self._model is None:
            self.load()
        return self._model

    @property
    def class_names(self) -> list[str]:
        if not self._class_names:
            self.load()
        return self._class_names


# ---------------------------------------------------------------------------
# Spectral band-energy pre-filter (runs on CPU, very fast)
# ---------------------------------------------------------------------------
def _compute_band_energy_ratio(
    audio: np.ndarray,
    sr: int = SAMPLE_RATE,
    low_hz: float = SIREN_FREQ_LOW,
    high_hz: float = SIREN_FREQ_HIGH,
) -> tuple[float, float]:
    """
    Return (band_energy_ratio, dominant_freq_hz).

    band_energy_ratio = energy in [low_hz, high_hz] / total energy
    dominant_freq_hz  = frequency bin with max magnitude within siren band
    """
    # Short-time Fourier transform
    n_fft = 2048
    hop_length = 512
    stft = np.abs(librosa.stft(audio.astype(np.float32), n_fft=n_fft, hop_length=hop_length))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    # Band mask
    band_mask = (freqs >= low_hz) & (freqs <= high_hz)
    total_energy = float(np.sum(stft ** 2)) + 1e-9
    band_energy = float(np.sum(stft[band_mask, :] ** 2))
    ratio = min(band_energy / total_energy, 1.0)

    # Dominant frequency in band
    band_stft = stft[band_mask, :]
    if band_stft.size > 0:
        max_bin = int(np.argmax(np.mean(band_stft, axis=1)))
        dominant_freq = float(freqs[band_mask][max_bin])
    else:
        dominant_freq = 0.0

    return ratio, dominant_freq


def _apply_bengaluru_noise_filter(audio: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """
    Attenuate frequencies below 800 Hz and above 4 kHz (horn + engine noise
    dominant in Bengaluru traffic) using a simple FFT-based notch approach.
    This improves the signal-to-noise ratio for siren detection.
    """
    fft = np.fft.rfft(audio.astype(np.float32))
    freqs = np.fft.rfftfreq(len(audio), d=1.0 / sr)

    # Suppress everything outside the 800–4000 Hz band
    mask = (freqs < 800.0) | (freqs > 4000.0)
    # Gentle attenuation rather than a hard zero to avoid ringing artefacts
    fft[mask] *= 0.15

    return np.fft.irfft(fft, n=len(audio)).astype(np.float32)


# ---------------------------------------------------------------------------
# Core inference
# ---------------------------------------------------------------------------
def _run_yamnet_inference(audio: np.ndarray, microphone_id: str = "mic_0") -> AcousticDetection:
    """
    Synchronous YAMNet inference over one audio window.
    Runs inside a ThreadPoolExecutor (called from the async wrapper).
    """
    import tensorflow as tf  # type: ignore

    registry = _YAMNetRegistry.get()
    ts = time.monotonic()

    # 1. Spectral pre-filter
    filtered = _apply_bengaluru_noise_filter(audio)
    band_ratio, dominant_freq = _compute_band_energy_ratio(filtered)

    # 2. YAMNet forward pass
    # yamnet returns (scores, embeddings, spectrogram)
    # scores shape: (num_frames, 521)
    waveform_tensor = tf.constant(filtered, dtype=tf.float32)
    scores, _embeddings, _spectrogram = registry.model(waveform_tensor)
    scores_np: np.ndarray = scores.numpy()  # (frames, 521)

    # 3. Aggregate siren class scores
    yamnet_siren_score = 0.0
    for class_id in SIREN_CLASS_IDS:
        if class_id < scores_np.shape[1]:
            # Max across frames for this class
            yamnet_siren_score = max(yamnet_siren_score, float(scores_np[:, class_id].max()))

    # 4. Fused confidence
    # Up-weight if band energy is concentrated in the siren range
    fused = (
        YAMNET_WEIGHT * yamnet_siren_score
        + BAND_ENERGY_WEIGHT * band_ratio
    )
    # Bengaluru penalty: strong low-frequency content likely means heavy traffic
    # horns, not sirens — penalise slightly.
    low_freq_mask = np.fft.rfftfreq(len(audio), d=1.0 / SAMPLE_RATE) < 800.0
    low_fft = np.abs(np.fft.rfft(audio.astype(np.float32)))
    low_energy_ratio = float(np.sum(low_fft[low_freq_mask] ** 2)) / (
        float(np.sum(low_fft ** 2)) + 1e-9
    )
    if low_energy_ratio > 0.70:
        fused *= 0.80   # likely horn-dominated noise

    fused = float(np.clip(fused, 0.0, 1.0))

    logger.debug(
        f"Acoustic  mic={microphone_id}  yamnet={yamnet_siren_score:.3f}"
        f"  band_ratio={band_ratio:.3f}  dominant_hz={dominant_freq:.0f}"
        f"  fused={fused:.3f}"
    )

    return AcousticDetection(
        siren_confidence=fused,
        yamnet_confidence=yamnet_siren_score,
        band_energy_ratio=band_ratio,
        dominant_freq_hz=dominant_freq,
        window_timestamp=ts,
        microphone_id=microphone_id,
    )


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------
_executor = asyncio.get_event_loop if False else None   # lazy placeholder


async def analyse_audio_window(
    audio: np.ndarray,
    microphone_id: str = "mic_0",
) -> AcousticDetection:
    """
    Async wrapper.  Pass a 16 kHz mono float32 NumPy array of length
    WINDOW_SAMPLES (0.96 s × 16 000 = 15 360 samples).
    """
    loop = asyncio.get_event_loop()
    if len(audio) != WINDOW_SAMPLES:
        # Pad or trim to the canonical length
        if len(audio) < WINDOW_SAMPLES:
            audio = np.pad(audio, (0, WINDOW_SAMPLES - len(audio)))
        else:
            audio = audio[:WINDOW_SAMPLES]

    result: AcousticDetection = await loop.run_in_executor(
        None, _run_yamnet_inference, audio, microphone_id
    )
    return result


# ---------------------------------------------------------------------------
# Live microphone capture (used by fusion.py in live mode)
# ---------------------------------------------------------------------------
class MicrophoneStream:
    """
    Captures audio from the system microphone using sounddevice and queues
    0.96-second windows for the inference loop.
    """

    def __init__(
        self,
        device: Optional[int] = None,
        microphone_id: str = "mic_0",
    ) -> None:
        self._device = device
        self._microphone_id = microphone_id
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)
        self._stream = None
        self._running = False

    def start(self) -> None:
        import sounddevice as sd  # type: ignore

        self._running = True
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=WINDOW_SAMPLES,
            device=self._device,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info(f"MicrophoneStream started  device={self._device}  mic={self._microphone_id}")

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: object,
    ) -> None:
        if status:
            logger.warning(f"Audio stream status: {status}")
        audio_flat = indata[:, 0].copy()  # mono
        if not self._queue.full():
            self._queue.put_nowait(audio_flat)

    async def get_window(self) -> Optional[np.ndarray]:
        """Non-blocking pull of the next audio window."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: self._queue.get(timeout=2.0)
            )
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
        logger.info("MicrophoneStream stopped.")


# ---------------------------------------------------------------------------
# Synthetic siren generator (for unit testing without a microphone)
# ---------------------------------------------------------------------------
def generate_synthetic_siren(
    duration_s: float = WINDOW_SECONDS,
    base_freq: float = 1500.0,
    sweep_hz: float = 800.0,
    sr: int = SAMPLE_RATE,
) -> np.ndarray:
    """
    Produce a synthetic FM-swept siren tone in the 1–3 kHz band.
    Useful for offline integration tests.
    """
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    # Linear frequency sweep from base_freq to base_freq + sweep_hz and back
    half = len(t) // 2
    freq = np.concatenate([
        np.linspace(base_freq, base_freq + sweep_hz, half),
        np.linspace(base_freq + sweep_hz, base_freq, len(t) - half),
    ])
    phase = 2 * np.pi * np.cumsum(freq) / sr
    siren = 0.6 * np.sin(phase).astype(np.float32)
    # Add mild Bengaluru-style horn noise at 600 Hz
    horn = 0.15 * np.sin(2 * np.pi * 600.0 * t).astype(np.float32)
    return (siren + horn).astype(np.float32)
