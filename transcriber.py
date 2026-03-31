"""Whisper transcription — local (pywhispercpp) or OpenAI API."""

import ctypes
import ctypes.util
import io
import json
import re
import struct
import threading
import time
import urllib.error
import urllib.request

import numpy as np

from config import Config
from utils import get_logger

log = get_logger(__name__)


def _mlockall() -> bool:
    """Lock all current process memory into RAM (prevent macOS swap).

    Returns True if successful, False otherwise (non-fatal).
    macOS may not implement mlockall (errno 78 = ENOSYS).
    """
    MCL_CURRENT = 1  # Lock all currently mapped pages

    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        log.warning("mlockall: libc not found — skipping")
        return False

    libc = ctypes.CDLL(libc_name, use_errno=True)
    result = libc.mlockall(MCL_CURRENT)
    if result != 0:
        errno = ctypes.get_errno()
        log.warning("mlockall failed (errno=%d) — will use heartbeat to keep model hot", errno)
        return False

    log.info("mlockall(MCL_CURRENT) succeeded — model memory pinned in RAM")
    return True

# Known Whisper hallucinations (short/silent audio produces these)
_HALLUCINATION_PATTERNS = [
    r"редактор\s+субтитров",
    r"субтитры\s+(сделал|делал|выполнил)",
    r"корректор\s+\w\.\s*\w+",
    r"продолжение\s+следует",
    r"спасибо\s+за\s+просмотр",
    r"подписывайтесь\s+на\s+канал",
    r"www\.",
    r"http",
]
_HALLUCINATION_RE = re.compile(
    "|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE
)

_TRANSCRIPTION_TIMEOUT = 30  # seconds


class BaseTranscriber:
    def __init__(self, config: Config) -> None:
        self._config = config

    def load_model(self) -> None:
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray) -> str:
        raise NotImplementedError


_HEARTBEAT_INTERVAL_SEC = 30 * 60  # 30 minutes


class LocalTranscriber(BaseTranscriber):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._model = None
        self._heartbeat_stop = threading.Event()
        self._busy = threading.Lock()  # held during real transcriptions

    def load_model(self) -> None:
        from pywhispercpp.model import Model
        log.info("Loading whisper model '%s' from %s …",
                 self._config.model_name, self._config.model_path)
        self._model = Model(
            self._config.model_path,
            n_threads=self._config.n_threads,
        )
        log.info("Model loaded successfully")

        # Warmup: transcribe 0.1s of silence to fault all model pages into RAM
        self._warmup()

        # Try to pin memory in RAM (works on Linux, may fail on macOS)
        locked = _mlockall()

        # If mlockall failed, start background heartbeat to keep model pages hot.
        # Every 30 min, a silent transcription touches all model memory pages,
        # preventing macOS from swapping them to disk.
        if not locked:
            self._start_heartbeat()

    def _warmup(self) -> None:
        """Run a tiny silent transcription to fault all model pages into RAM."""
        log.info("Warming up model (faulting all memory pages)…")
        try:
            silence = np.zeros(int(self._config.sample_rate * 0.1),
                               dtype=np.float32)
            self._model.transcribe(silence, language=self._config.language)
            log.info("Warmup complete")
        except Exception:
            log.warning("Warmup transcription failed (non-fatal)", exc_info=True)

    def _start_heartbeat(self) -> None:
        """Background thread: periodically run a silent transcription to keep
        model memory pages hot and prevent macOS from swapping them."""
        log.info("Starting model heartbeat (every %d min)",
                 _HEARTBEAT_INTERVAL_SEC // 60)

        def _heartbeat_loop():
            while not self._heartbeat_stop.wait(timeout=_HEARTBEAT_INTERVAL_SEC):
                # Only run if no real transcription is in progress
                if self._busy.acquire(blocking=False):
                    try:
                        log.debug("Heartbeat: touching model memory pages…")
                        silence = np.zeros(
                            int(self._config.sample_rate * 0.1),
                            dtype=np.float32,
                        )
                        self._model.transcribe(
                            silence, language=self._config.language,
                        )
                        log.debug("Heartbeat complete")
                    except Exception:
                        log.warning("Heartbeat failed (non-fatal)",
                                    exc_info=True)
                    finally:
                        self._busy.release()
                else:
                    log.debug("Heartbeat skipped — transcription in progress")

        t = threading.Thread(target=_heartbeat_loop, daemon=True,
                             name="whisper-heartbeat")
        t.start()

    def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        if len(audio) == 0:
            return ""

        duration = len(audio) / self._config.sample_rate
        if duration < self._config.min_duration:
            log.warning("Audio too short (%.2fs < %.2fs), skipping",
                        duration, self._config.min_duration)
            return ""

        # Acquire _busy to block heartbeat during real transcription
        self._busy.acquire()
        try:
            return self._do_transcribe(audio, duration)
        finally:
            self._busy.release()

    def _do_transcribe(self, audio: np.ndarray, duration: float) -> str:
        # Run transcription with timeout to prevent infinite hangs
        result = [None]
        error = [None]

        def _run():
            try:
                segments = self._model.transcribe(
                    audio,
                    language=self._config.language,
                    translate=self._config.translate,
                )
                result[0] = " ".join(
                    seg.text.strip() for seg in segments if seg.text.strip()
                )
            except Exception as e:
                error[0] = e

        t0 = time.monotonic()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=_TRANSCRIPTION_TIMEOUT)

        elapsed = time.monotonic() - t0

        if t.is_alive():
            log.warning("Transcription timed out after %ds — skipping",
                        _TRANSCRIPTION_TIMEOUT)
            return ""

        if error[0] is not None:
            raise error[0]

        text = result[0] or ""

        # Log inference speed for diagnostics
        ratio = elapsed / duration if duration > 0 else 0
        log.info("Inference: %.2fs for %.1fs audio (ratio=%.2f) %s",
                 elapsed, duration, ratio,
                 "SLOW" if ratio > 1.0 else "OK")

        if _HALLUCINATION_RE.search(text):
            log.warning("Filtered hallucination: %.80s", text)
            return ""

        log.info("Transcribed: %.80s…", text)
        return text


class APITranscriber(BaseTranscriber):
    def load_model(self) -> None:
        # No-op — no model to load, 0 MB RAM
        log.info("API mode — no local model to load")

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) == 0:
            return ""

        duration = len(audio) / self._config.sample_rate
        if duration < self._config.min_duration:
            log.warning("Audio too short (%.2fs < %.2fs), skipping",
                        duration, self._config.min_duration)
            return ""

        api_key = self._config.openai_api_key
        if not api_key:
            raise RuntimeError("OpenAI API key not configured")

        # Convert numpy float32 array → WAV bytes in memory
        wav_bytes = _numpy_to_wav(audio, self._config.sample_rate)

        # Build multipart form data
        boundary = "----AudioLogBoundary"
        body = _build_multipart(boundary, {
            "model": self._config.openai_model,
            "language": self._config.language,
            "file": ("audio.wav", wav_bytes, "audio/wav"),
        })

        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=_TRANSCRIPTION_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                text = data.get("text", "").strip()
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            log.error("OpenAI API error %d: %s", e.code, body_text)
            raise RuntimeError(f"OpenAI API error {e.code}") from e

        if _HALLUCINATION_RE.search(text):
            log.warning("Filtered hallucination: %.80s", text)
            return ""

        log.info("Transcribed (API): %.80s…", text)
        return text


def create_transcriber(config: Config) -> BaseTranscriber:
    """Factory: create appropriate transcriber based on config."""
    if config.transcription_mode == "api":
        return APITranscriber(config)
    return LocalTranscriber(config)


def test_api_connection(api_key: str, model: str) -> dict:
    """Test OpenAI API connection with a tiny silent audio clip.
    Returns {"success": True} or {"success": False, "error": "..."}."""
    # 0.1s of silence
    silence = np.zeros(1600, dtype=np.float32)
    wav_bytes = _numpy_to_wav(silence, 16000)

    boundary = "----AudioLogBoundary"
    body = _build_multipart(boundary, {
        "model": model,
        "language": "en",
        "file": ("test.wav", wav_bytes, "audio/wav"),
    })

    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return {"success": True}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            err_data = json.loads(body_text)
            msg = err_data.get("error", {}).get("message", body_text)
        except Exception:
            msg = body_text
        return {"success": False, "error": f"HTTP {e.code}: {msg}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── helpers ──

def _numpy_to_wav(audio: np.ndarray, sample_rate: int) -> bytes:
    """Convert float32 numpy array to WAV bytes (16-bit PCM)."""
    # Clip and convert to int16
    audio_clipped = np.clip(audio, -1.0, 1.0)
    pcm = (audio_clipped * 32767).astype(np.int16)
    pcm_bytes = pcm.tobytes()

    buf = io.BytesIO()
    # WAV header
    num_samples = len(pcm)
    data_size = num_samples * 2  # 16-bit = 2 bytes per sample
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))        # chunk size
    buf.write(struct.pack("<H", 1))         # PCM format
    buf.write(struct.pack("<H", 1))         # mono
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * 2))  # byte rate
    buf.write(struct.pack("<H", 2))         # block align
    buf.write(struct.pack("<H", 16))        # bits per sample
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm_bytes)

    return buf.getvalue()


def _build_multipart(boundary: str, fields: dict) -> bytes:
    """Build multipart/form-data body.
    Values can be strings or tuples (filename, bytes, content_type)."""
    parts = []
    for key, value in fields.items():
        if isinstance(value, tuple):
            filename, data, content_type = value
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            )
            parts.append(data)
            parts.append(b"\r\n")
        else:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            )
    parts.append(f"--{boundary}--\r\n")

    result = b""
    for part in parts:
        if isinstance(part, str):
            result += part.encode("utf-8")
        else:
            result += part
    return result
