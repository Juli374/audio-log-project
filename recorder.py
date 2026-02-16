"""Audio recording via sounddevice."""

import threading

import numpy as np
import sounddevice as sd

from config import Config
from utils import get_logger

log = get_logger(__name__)


class Recorder:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._device_logged = False
        self.last_duration: float = 0.0
        self.last_rms: float = 0.0
        self.last_peak: float = 0.0

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            log.warning("sounddevice status: %s", status)
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())

    def _pick_input_device(self) -> int | None:
        """Pick input device: use macOS default, but skip 'Find My' garbage."""
        devices = sd.query_devices()
        default_idx = sd.default.device[0]

        # Check if macOS default is usable (not a "Find My" pseudo-device)
        if default_idx is not None and default_idx < len(devices):
            default_dev = devices[default_idx]
            if (default_dev["max_input_channels"] > 0
                    and "find my" not in default_dev["name"].lower()):
                log.info("Audio input: [%d] %s (system default)",
                         default_idx, default_dev["name"])
                return default_idx

        # Default is bad — find best alternative
        builtin = None
        any_mic = None
        for i, d in enumerate(devices):
            if d["max_input_channels"] < 1:
                continue
            name = d["name"].lower()
            if "find my" in name:
                continue
            if "macbook" in name or "built-in" in name:
                builtin = i
            elif any_mic is None:
                any_mic = i

        chosen = builtin if builtin is not None else any_mic
        if chosen is not None:
            log.info("Audio input: [%d] %s (auto-selected, default was unusable)",
                     chosen, devices[chosen]["name"])
        else:
            log.error("No usable input device found!")
        return chosen

    def start(self) -> None:
        if not self._device_logged:
            self._device_id = self._pick_input_device()
            self._device_logged = True

        with self._lock:
            self._chunks.clear()
            self._recording = True

        self._stream = sd.InputStream(
            device=self._device_id,
            samplerate=self._config.sample_rate,
            channels=self._config.channels,
            dtype=self._config.dtype,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Recording started")

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False

        if self._stream is not None:
            try:
                self._stream.abort()   # abort — не блокирует, в отличие от stop()
                self._stream.close()
            except Exception:
                log.warning("Error closing audio stream", exc_info=True)
            self._stream = None

        with self._lock:
            chunks = self._chunks.copy()
            self._chunks.clear()

        if not chunks:
            log.warning("No audio recorded")
            return np.array([], dtype=np.float32)

        audio = np.concatenate(chunks, axis=0).flatten()
        duration = len(audio) / self._config.sample_rate

        # Audio diagnostics
        rms = float(np.sqrt(np.mean(audio ** 2)))
        peak = float(np.max(np.abs(audio)))
        log.info("Recorded %.1fs (%d samples) | RMS=%.4f peak=%.4f %s",
                 duration, len(audio), rms, peak,
                 "⚠️ VERY QUIET" if peak < 0.01 else "OK")

        self.last_duration = duration
        self.last_rms = rms
        self.last_peak = peak

        return audio
