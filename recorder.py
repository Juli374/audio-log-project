"""Audio recording via sounddevice.

Uses the Mixxx serialization pattern to avoid PortAudio/CoreAudio deadlocks
on macOS: instead of calling abort()/close() from the caller thread, the
callback raises CallbackStop to stop the stream from the audio thread.
The finished_callback confirms the stream has fully stopped, then close()
is safe because Pa_CloseStream on an already-stopped stream skips
AudioDeviceStop (which is what deadlocks). A fresh stream is created for
each recording.

Tail-preservation notes:
- The callback appends indata BEFORE checking _should_stop so the last
  in-flight buffer (the one that initiates the stop) is captured, not
  dropped.
- `stop()` waits synchronously for `finished_callback` before snapshotting
  self._chunks — this guarantees every callback that was going to fire has
  already fired.
- CallbackStop (= PortAudio paComplete) is used instead of CallbackAbort
  (= paAbort). For input streams the distinction is small, but paComplete
  is the documented way to end a stream cleanly.
- 150 ms of silence is appended to the returned audio. Whisper's decoder
  needs a natural segment boundary or it may drop the final word; see
  huggingface/transformers#23231 and whisper.cpp VAD speech_pad_ms=30.

See: https://github.com/mixxxdj/mixxx/pull/14208
See: https://github.com/PortAudio/portaudio/issues/367
See: https://portaudio.com/docs/v19-doxydocs/start_stop_abort.html
"""

import threading
import time

import numpy as np
import sounddevice as sd

from config import Config
from utils import get_logger

log = get_logger(__name__)


# Substrings that identify non-physical input devices we never want to
# auto-select for dictation. Matched case-insensitively against the device
# name reported by PortAudio.
_VIRTUAL_DEVICE_SUBSTRINGS = (
    "find my",         # Apple's "Find My" pseudo-device
    "blackhole",       # BlackHole 2ch / 16ch / 64ch
    "soundflower",
    "loopback",        # Rogue Amoeba Loopback
    "ishowu",
    "zoomaudiodevice",
    "aggregate device",
    "multi-output",
    "virtual",
)


def _is_virtual_device(name: str) -> bool:
    """True if the device name matches a known virtual/loopback device."""
    lower = name.lower()
    return any(sub in lower for sub in _VIRTUAL_DEVICE_SUBSTRINGS)


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
        # Append FIRST — this callback may be the one that raises CallbackStop.
        # Checking _should_stop before the append would drop the last in-flight
        # buffer. We also no longer gate on _recording: snapshotting happens
        # after finished_callback in stop(), so extra callbacks during shutdown
        # are harmless.
        with self._lock:
            self._chunks.append(indata.copy())
        if self._should_stop:
            raise sd.CallbackStop

    def _on_stream_finished(self) -> None:
        """Called by PortAudio when the stream becomes inactive."""
        self._stream_finished.set()

    def _pick_input_device(self) -> int | None:
        """Pick input device: prefer macOS default, but skip known pseudo-devices.

        System default sometimes points to a loopback/virtual device
        (BlackHole, Soundflower, Loopback, ZoomAudioDevice, iShowU) or
        Apple's "Find My" placeholder — these show up as legitimate input
        devices but either produce silence or aren't real mics. For a
        dictation tool, the real built-in mic is almost always what the
        user wants, so we skip these and fall back to the first physical
        microphone we find.
        """
        devices = sd.query_devices()
        default_idx = sd.default.device[0]

        if default_idx is not None and default_idx < len(devices):
            default_dev = devices[default_idx]
            if (default_dev["max_input_channels"] > 0
                    and not _is_virtual_device(default_dev["name"])):
                log.info("Audio input: [%d] %s (system default)",
                         default_idx, default_dev["name"])
                return default_idx
            if default_dev["max_input_channels"] > 0:
                log.warning(
                    "System default input [%d] %s is a virtual/loopback "
                    "device — falling back to built-in mic",
                    default_idx, default_dev["name"])

        # Default is bad — find best alternative
        builtin = None
        any_mic = None
        for i, d in enumerate(devices):
            if d["max_input_channels"] < 1:
                continue
            if _is_virtual_device(d["name"]):
                continue
            name = d["name"].lower()
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
        """Stop recording and return captured audio (with 150 ms silence tail).

        MUST be called from a worker thread, not the NSApplication main thread
        or the sounddevice audio thread — this method blocks for up to 3
        seconds waiting for the stream to drain.
        """
        if self._stream is None or not self._stream.active:
            # Stream not running — just snapshot whatever we have and finalize
            with self._lock:
                self._recording = False
                chunks = self._chunks.copy()
                self._chunks.clear()
            return self._finalize_audio(chunks)

        # Signal callback to raise CallbackStop on its next invocation, then
        # wait for finished_callback. Only after finished_callback fires can
        # we be sure no more callbacks will append to self._chunks.
        self._stream_finished.clear()
        self._should_stop = True

        if not self._stream_finished.wait(timeout=3.0):
            log.warning("Stream drain timed out (3s) — some audio may be lost")
            # Clear flag so a reused stream doesn't keep aborting
            self._should_stop = False

        with self._lock:
            self._recording = False
            chunks = self._chunks.copy()
            self._chunks.clear()

        # Close stream in background. Stream is stopped → Pa_CloseStream
        # skips AudioDeviceStop, so the Mixxx deadlock is avoided. We still
        # run it off the hotkey-release path for symmetry and to handle the
        # rare edge case where close() stalls.
        threading.Thread(target=self._close_stream_safely, daemon=True,
                         name="recorder-close").start()

        return self._finalize_audio(chunks)

    def _close_stream_safely(self) -> None:
        """Close the (already-stopped) stream. Idempotent."""
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.close()
            log.info("Stream closed")
        except Exception:
            log.exception("Stream close failed")

    def _finalize_audio(self, chunks: list[np.ndarray]) -> np.ndarray:
        """Concatenate chunks, record diagnostics on the captured audio, then
        append 150 ms of silence for Whisper's decoder."""
        if not chunks:
            log.warning("No audio recorded")
            self.last_duration = 0.0
            self.last_rms = 0.0
            self.last_peak = 0.0
            return np.array([], dtype=np.float32)

        audio = np.concatenate(chunks, axis=0).flatten()

        # Metrics on captured audio (pre-padding — padding is for Whisper only)
        duration = len(audio) / self._config.sample_rate
        rms = float(np.sqrt(np.mean(audio ** 2)))
        peak = float(np.max(np.abs(audio)))
        log.info("Recorded %.1fs (%d samples) | RMS=%.4f peak=%.4f %s",
                 duration, len(audio), rms, peak,
                 "⚠️ VERY QUIET" if peak < 0.01 else "OK")

        self.last_duration = duration
        self.last_rms = rms
        self.last_peak = peak

        # Pad 150 ms of silence so Whisper sees a natural segment boundary.
        tail_samples = int(self._config.sample_rate * 0.15)
        if tail_samples <= 0:
            return audio
        return np.concatenate(
            [audio, np.zeros(tail_samples, dtype=np.float32)])
