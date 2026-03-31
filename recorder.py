"""Audio recording via sounddevice.

Uses the Mixxx serialization pattern to avoid PortAudio/CoreAudio deadlocks
on macOS: instead of calling abort()/close() from the caller thread, the
callback raises CallbackAbort to stop the stream from the audio thread.
The finished_callback confirms the stream has fully stopped, then close()
is safe because Pa_CloseStream on an already-stopped stream skips
AudioDeviceStop (which is what deadlocks). A fresh stream is created for
each recording.

See: https://github.com/mixxxdj/mixxx/pull/14208
See: https://github.com/PortAudio/portaudio/issues/367
"""

import threading
import time

import numpy as np
import sounddevice as sd

from config import Config
from utils import get_logger

log = get_logger(__name__)


class Recorder:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._stream: sd.InputStream | None = None
        self._device_id: int | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._should_stop = False
        self._stream_finished = threading.Event()
        self.last_duration: float = 0.0
        self.last_rms: float = 0.0
        self.last_peak: float = 0.0
        self.recording_start_time: float = 0.0

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            log.warning("sounddevice status: %s", status)
        if self._should_stop:
            raise sd.CallbackAbort
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())

    def _on_stream_finished(self) -> None:
        """Called by PortAudio when the stream becomes inactive."""
        self._stream_finished.set()

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

    def _ensure_stream(self) -> None:
        """Create a fresh audio input stream.

        After _stop_stream(), self._stream is None (closed after confirmed
        stop). A new stream is created for each recording to avoid PortAudio
        callback reuse issues after CallbackAbort.

        If _stop_stream() timed out (fallback), the old stream is still alive
        and active — reuse it as-is.
        """
        device_id = self._pick_input_device()
        if device_id is None:
            raise RuntimeError("No usable input device")

        # Reuse existing active stream (timeout fallback or same session)
        if (self._stream is not None
                and self._device_id == device_id
                and self._stream.active):
            return

        # Clean up old stream if exists (device change or inactive leftover)
        if self._stream is not None:
            if self._device_id != device_id:
                log.info("Device changed — recreating stream")
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._should_stop = False
        self._stream_finished.clear()
        self._device_id = device_id
        self._stream = sd.InputStream(
            device=device_id,
            samplerate=self._config.sample_rate,
            channels=self._config.channels,
            dtype=self._config.dtype,
            callback=self._callback,
            finished_callback=self._on_stream_finished,
        )
        self._stream.start()
        log.info("Audio stream opened on device [%d]", device_id)

    def start(self) -> None:
        self._ensure_stream()

        with self._lock:
            self._chunks.clear()
            self._recording = True

        self.recording_start_time = time.monotonic()
        log.info("Recording started")

    @property
    def is_recording(self) -> bool:
        return self._recording

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False

        with self._lock:
            chunks = self._chunks.copy()
            self._chunks.clear()

        # Stop stream from audio thread (Mixxx pattern — avoids deadlock)
        self._stop_stream()

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

    def _stop_stream(self) -> None:
        """Stop and close the audio stream using the Mixxx serialization pattern.

        1. Signal the callback to raise CallbackAbort (stops from audio thread)
        2. Wait for finished_callback (confirms stream is fully stopped)
        3. Close the stopped stream (Pa_CloseStream on a stopped stream is safe
           because it skips AudioDeviceStop — which is what causes deadlocks)

        If finished_callback times out, the stream is kept alive as fallback
        (persistent stream, same as before). Next _ensure_stream will reuse it.
        """
        if self._stream is None or not self._stream.active:
            return

        self._stream_finished.clear()
        self._should_stop = True

        if self._stream_finished.wait(timeout=3.0):
            log.info("Stream stopped via finished_callback")
            # Stream is confirmed stopped — close() is safe (no deadlock)
            try:
                self._stream.close()
                log.info("Stream closed")
            except Exception:
                log.exception("Stream close after stop failed")
            self._stream = None
        else:
            log.warning("Stream finished_callback timed out (3s) — "
                        "stream kept alive as fallback")
            self._should_stop = False
