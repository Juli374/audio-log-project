"""Long-session audio recorder — streams to disk, not RAM.

Unlike `Recorder` which accumulates chunks in memory (max 5 min, ~300 MB for
hours of audio), `SessionRecorder` writes each incoming audio chunk to a WAV
file immediately via a dedicated writer thread. Constant RAM usage regardless
of recording duration.

Design:
- PortAudio callback enqueues numpy chunks into a Queue (non-blocking,
  never stalls the audio thread with disk I/O).
- A dedicated writer thread dequeues chunks, computes running metrics
  (peak, sum-of-squares for RMS), converts float32 → int16 PCM, and
  appends to an open `wave.Wave_write` file. Python's `wave` module
  automatically maintains the RIFF/data chunk sizes.
- Shutdown uses the same Mixxx serialization pattern as `Recorder`:
  callback raises CallbackAbort, finished_callback confirms the stream
  is stopped, then close() is safe.

Exclusive mic use with `Recorder` is enforced at the SessionController
layer — the recorder itself just opens whatever device is available.
"""

from __future__ import annotations

import queue
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

import db
from config import Config
from utils import get_logger

log = get_logger(__name__)

# Writer thread waits at most this long for new chunks before checking stop flag
_WRITER_POLL_TIMEOUT = 0.5

# How long to wait for finished_callback after raising CallbackAbort
_STREAM_STOP_TIMEOUT = 3.0

# How long to wait for the writer thread to drain its queue at shutdown
_WRITER_JOIN_TIMEOUT = 10.0


@dataclass
class SessionRecording:
    """Metadata returned by SessionRecorder.start()/stop()."""
    audio_path: Path
    duration_sec: float
    started_at: datetime
    rms: float
    peak: float
    session_id: int


def write_chunk_to_wav(
    wav_file: wave.Wave_write,
    chunk: np.ndarray,
) -> tuple[int, float, float]:
    """Write one float32 chunk to a WAV file as int16 PCM.

    Returns (samples_written, chunk_peak, chunk_sum_of_squares). The caller
    accumulates these across chunks to compute overall RMS/peak.

    Extracted as a pure function so unit tests can verify WAV output
    without spinning up a real audio stream.
    """
    flat = chunk.flatten().astype(np.float32, copy=False)
    if len(flat) == 0:
        return 0, 0.0, 0.0
    peak = float(np.max(np.abs(flat)))
    sum_sq = float(np.sum(flat * flat))
    clipped = np.clip(flat, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16).tobytes()
    wav_file.writeframes(pcm)
    return len(flat), peak, sum_sq


class SessionRecorder:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._stream: sd.InputStream | None = None
        self._device_id: int | None = None

        self._wav_file: wave.Wave_write | None = None
        self._audio_path: Path | None = None

        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._writer_stop = threading.Event()

        self._recording = False
        self._should_stop = False
        self._stream_finished = threading.Event()

        self._start_time: float = 0.0
        self._start_datetime: datetime | None = None
        self._samples_written: int = 0
        self._rms_accum: float = 0.0
        self._peak: float = 0.0
        self._session_id: int | None = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def bytes_written(self) -> int:
        # int16 mono → 2 bytes per sample
        return self._samples_written * 2

    @property
    def audio_path(self) -> Path | None:
        return self._audio_path

    @property
    def session_id(self) -> int | None:
        return self._session_id

    # ── internal: audio stream ──

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
        # Copy because indata buffer is reused by PortAudio
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            log.error("Session queue full — dropping audio chunk")

    def _on_stream_finished(self) -> None:
        self._stream_finished.set()

    def _pick_input_device(self) -> int | None:
        """Same logic as Recorder._pick_input_device. Imported as a free
        function to keep the two recorders independent at the class level
        while sharing the virtual-device skip list."""
        from recorder import _is_virtual_device

        devices = sd.query_devices()
        default_idx = sd.default.device[0]

        if default_idx is not None and default_idx < len(devices):
            default_dev = devices[default_idx]
            if (default_dev["max_input_channels"] > 0
                    and not _is_virtual_device(default_dev["name"])):
                log.info("Session audio input: [%d] %s (system default)",
                         default_idx, default_dev["name"])
                return default_idx
            if default_dev["max_input_channels"] > 0:
                log.warning(
                    "System default input [%d] %s is virtual/loopback — "
                    "falling back for session", default_idx,
                    default_dev["name"])

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
            log.info("Session audio input: [%d] %s (auto-selected)",
                     chosen, devices[chosen]["name"])
        else:
            log.error("No usable input device for session")
        return chosen

    # ── internal: writer thread ──

    def _writer_loop(self) -> None:
        """Dequeue audio chunks and append to WAV file.

        Exits only after _writer_stop is set AND the queue is drained,
        to avoid losing buffered audio on shutdown.
        """
        while True:
            try:
                chunk = self._queue.get(timeout=_WRITER_POLL_TIMEOUT)
            except queue.Empty:
                if self._writer_stop.is_set():
                    return
                continue

            if self._wav_file is None:
                log.warning("Writer got chunk but WAV file is closed — dropping")
                continue

            try:
                samples, peak, sum_sq = write_chunk_to_wav(
                    self._wav_file, chunk)
            except Exception:
                log.exception("Failed to write chunk to WAV")
                continue

            self._samples_written += samples
            self._rms_accum += sum_sq
            if peak > self._peak:
                self._peak = peak

    # ── public API ──

    def start(self) -> SessionRecording:
        """Open WAV, start audio stream, start writer thread.

        Raises RuntimeError if already recording or no device available.
        """
        if self._recording:
            raise RuntimeError("SessionRecorder already recording")

        device_id = self._pick_input_device()
        if device_id is None:
            raise RuntimeError("No usable input device")

        # File path
        self._config.ensure_session_dir()
        self._start_datetime = datetime.now()
        filename = self._start_datetime.strftime("%Y%m%d-%H%M%S.wav")
        self._audio_path = Path(self._config.session_audio_dir) / filename

        # DB row — status='recording'
        self._session_id = db.session_create(str(self._audio_path))

        # Reset counters
        self._samples_written = 0
        self._rms_accum = 0.0
        self._peak = 0.0
        self._should_stop = False
        self._stream_finished.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        # Open WAV
        self._wav_file = wave.open(str(self._audio_path), "wb")
        self._wav_file.setnchannels(self._config.channels)
        self._wav_file.setsampwidth(2)  # int16
        self._wav_file.setframerate(self._config.sample_rate)

        # Start writer thread
        self._writer_stop.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="session-writer")
        self._writer_thread.start()

        # Open + start audio stream
        self._device_id = device_id
        try:
            self._stream = sd.InputStream(
                device=device_id,
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
                dtype=self._config.dtype,
                callback=self._callback,
                finished_callback=self._on_stream_finished,
            )
            self._stream.start()
        except Exception:
            # Rollback: close WAV, stop writer, mark DB error
            self._writer_stop.set()
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=1.0)
            try:
                self._wav_file.close()
            except Exception:
                pass
            self._wav_file = None
            db.session_set_status(
                self._session_id, "error", error="Failed to open audio stream")
            raise

        self._recording = True
        self._start_time = time.monotonic()
        log.info("Session started → %s (device %d, id=%d)",
                 self._audio_path, device_id, self._session_id)

        return SessionRecording(
            audio_path=self._audio_path,
            duration_sec=0.0,
            started_at=self._start_datetime,
            rms=0.0,
            peak=0.0,
            session_id=self._session_id,
        )

    def stop(self) -> SessionRecording:
        """Stop recording, close WAV, finalize DB (→ transcribing).

        Returns metadata with final duration/metrics.
        """
        recording = self._stop_capture()
        if recording.session_id is not None:
            db.session_finalize(recording.session_id, recording.duration_sec)
        return recording

    def cancel(self) -> SessionRecording | None:
        """Stop recording and discard the WAV file. Marks DB as cancelled.

        Returns None if nothing was recording.
        """
        if not self._recording:
            return None
        recording = self._stop_capture()
        if recording.audio_path and recording.audio_path.exists():
            try:
                recording.audio_path.unlink()
                log.info("Cancelled WAV deleted: %s", recording.audio_path)
            except Exception:
                log.exception("Failed to delete cancelled WAV")
        if recording.session_id is not None:
            db.session_set_status(recording.session_id, "cancelled")
        return recording

    # ── internal: shared shutdown ──

    def _stop_capture(self) -> SessionRecording:
        """Stop stream, drain queue, close WAV. Does NOT mutate DB —
        caller (stop/cancel) decides the final status."""
        if not self._recording:
            raise RuntimeError("SessionRecorder not recording")

        self._recording = False

        if self._stream is not None:
            if not self._stream.active:
                # Stream already stopped on its own — skip the abort dance
                log.info("Session stream already inactive")
                try:
                    self._stream.close()
                except Exception:
                    log.exception("Stream close failed")
            else:
                # Mixxx pattern: signal callback to abort, wait for finished_callback
                self._stream_finished.clear()
                self._should_stop = True
                if self._stream_finished.wait(timeout=_STREAM_STOP_TIMEOUT):
                    log.info("Session stream stopped via finished_callback")
                    try:
                        self._stream.close()
                    except Exception:
                        log.exception("Stream close failed")
                else:
                    log.warning(
                        "Session stream finished_callback timed out (%.1fs) — "
                        "forcing close", _STREAM_STOP_TIMEOUT)
                    try:
                        self._stream.close()
                    except Exception:
                        log.exception("Forced stream close failed")
            self._stream = None

        # Drain queue + stop writer
        self._writer_stop.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=_WRITER_JOIN_TIMEOUT)
            if self._writer_thread.is_alive():
                log.warning("Writer thread did not finish in %.1fs — "
                            "buffered audio may be lost", _WRITER_JOIN_TIMEOUT)
            self._writer_thread = None

        # Close WAV (fixes RIFF size header)
        if self._wav_file is not None:
            try:
                self._wav_file.close()
            except Exception:
                log.exception("WAV close failed")
            self._wav_file = None

        # Compute final metrics
        actual_duration = (
            self._samples_written / self._config.sample_rate
            if self._config.sample_rate > 0 else 0.0)
        rms = (
            float(np.sqrt(self._rms_accum / self._samples_written))
            if self._samples_written > 0 else 0.0)

        log.info(
            "Session stopped: %.1fs (%d samples) | RMS=%.4f peak=%.4f → %s",
            actual_duration, self._samples_written, rms, self._peak,
            self._audio_path)

        return SessionRecording(
            audio_path=self._audio_path,
            duration_sec=actual_duration,
            started_at=self._start_datetime,
            rms=rms,
            peak=self._peak,
            session_id=self._session_id,
        )
