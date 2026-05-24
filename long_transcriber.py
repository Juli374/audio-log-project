"""Long-audio transcription: chunk on silence → transcribe parallel → stitch.

Wraps any `BaseTranscriber` (local whisper, OpenAI, Groq) to handle audio
files longer than a few minutes. Most transcription APIs cap upload size
(Groq: 25 MB, OpenAI: 25 MB) and benefit from parallel requests. This
module:

1. Loads a WAV file as float32.
2. Finds cut points inside silence windows near every ~10 min boundary,
   so chunks never split mid-word. Falls back to hard cuts if no silence.
3. Dispatches chunks to the base transcriber in parallel (ThreadPoolExecutor).
4. Retries transient failures per chunk (3 attempts, exponential backoff).
5. Stitches successful chunks with a configurable joiner; failed chunks
   appear in the output as `[ошибка чанка HH:MM-HH:MM]` markers.
"""

from __future__ import annotations

import time
import urllib.error
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from config import Config
from transcriber import BaseTranscriber
from utils import get_logger

log = get_logger(__name__)

# Retry policy for transient chunk failures (network, 5xx)
_MAX_CHUNK_ATTEMPTS = 3
_RETRY_BACKOFF = [1.0, 3.0, 9.0]  # seconds before attempts 1, 2, 3

# RMS window for silence detection
_RMS_WINDOW_MS = 20


@dataclass
class ChunkResult:
    index: int
    start_sec: float
    end_sec: float
    text: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ── Audio loading ──

def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Load a mono int16 WAV as float32 in [-1, 1]. Returns (audio, sample_rate).

    Raises ValueError on non-mono or non-16-bit files — SessionRecorder
    always writes mono int16 at config.sample_rate, so this is a sanity check.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        if channels != 1:
            raise ValueError(f"Expected mono WAV, got {channels} channels")
        if width != 2:
            raise ValueError(f"Expected 16-bit WAV, got {width * 8}-bit")
        raw = wf.readframes(n_frames)
    pcm = np.frombuffer(raw, dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    return audio, sr


# ── Silence-based chunking ──

def compute_rms_envelope(
    audio: np.ndarray, sample_rate: int, window_ms: int = _RMS_WINDOW_MS,
) -> np.ndarray:
    """RMS per `window_ms` window. Output length = ceil(len(audio) / window_samples)."""
    window = max(1, int(sample_rate * window_ms / 1000))
    n_windows = (len(audio) + window - 1) // window
    # Pad so it's divisible
    padded = np.zeros(n_windows * window, dtype=np.float32)
    padded[:len(audio)] = audio
    framed = padded.reshape(n_windows, window)
    return np.sqrt(np.mean(framed * framed, axis=1))


def find_silence_cuts(
    audio: np.ndarray,
    sample_rate: int,
    target_sec: float = 600.0,
    min_sec: float = 300.0,
    max_sec: float = 900.0,
    silence_rms: float = 0.005,
    silence_min_ms: int = 300,
) -> list[tuple[int, int]]:
    """Split audio into chunks with boundaries inside silence windows.

    Walks forward from start, every ~target_sec searches [-Δ, +Δ] around
    the target for the longest silent run ≥ silence_min_ms where RMS <
    silence_rms, cuts at its center. Clamps the search so each chunk
    ends up in [min_sec, max_sec].

    Returns a list of (start_sample, end_sample) pairs covering the whole
    audio. For audio shorter than `target_sec`, returns one chunk.
    """
    total_samples = len(audio)
    if total_samples == 0:
        return []
    target_samples = int(target_sec * sample_rate)
    min_samples = int(min_sec * sample_rate)
    max_samples = int(max_sec * sample_rate)

    # Short audio → single chunk
    if total_samples <= max_samples:
        return [(0, total_samples)]

    window = max(1, int(sample_rate * _RMS_WINDOW_MS / 1000))
    silence_min_windows = max(1, int(silence_min_ms / _RMS_WINDOW_MS))
    rms = compute_rms_envelope(audio, sample_rate, _RMS_WINDOW_MS)
    is_silent = rms < silence_rms

    cuts: list[tuple[int, int]] = []
    cursor = 0
    while cursor < total_samples:
        # If the remainder fits in one max-chunk, stop splitting
        if total_samples - cursor <= max_samples:
            cuts.append((cursor, total_samples))
            break

        # Search window in sample indices: [cursor+min, cursor+max]
        search_lo = cursor + min_samples
        search_hi = min(cursor + max_samples, total_samples)

        # Translate to RMS-window indices
        wlo = search_lo // window
        whi = min(search_hi // window, len(is_silent))

        cut_sample = _find_best_silence_cut(
            is_silent, wlo, whi, silence_min_windows, window,
            fallback=cursor + target_samples,
        )
        # Clamp into [search_lo, search_hi] in case fallback overshoots
        cut_sample = max(search_lo, min(cut_sample, search_hi))

        cuts.append((cursor, cut_sample))
        cursor = cut_sample

    return cuts


def _find_best_silence_cut(
    is_silent: np.ndarray,
    wlo: int,
    whi: int,
    min_run_windows: int,
    window_samples: int,
    fallback: int,
) -> int:
    """Within is_silent[wlo:whi], find the longest silent run ≥ min_run_windows
    and return its midpoint (in sample units). If no such run exists, return
    `fallback`."""
    if wlo >= whi:
        return fallback

    best_start = -1
    best_len = 0
    run_start = -1

    for i in range(wlo, whi):
        if is_silent[i]:
            if run_start == -1:
                run_start = i
            run_len = i - run_start + 1
            if run_len >= min_run_windows and run_len > best_len:
                best_len = run_len
                best_start = run_start
        else:
            run_start = -1

    if best_start < 0:
        return fallback

    midpoint_window = best_start + best_len // 2
    return midpoint_window * window_samples


# ── Stitching ──

def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def stitch_texts(chunks: list[ChunkResult], joiner: str = "\n\n") -> str:
    """Join chunk texts in index order. Failed chunks appear as
    `[ошибка чанка MM:SS-MM:SS]` markers so the user can see what's missing."""
    ordered = sorted(chunks, key=lambda c: c.index)
    parts: list[str] = []
    for c in ordered:
        if c.ok:
            text = c.text.strip()
            if text:
                parts.append(text)
        else:
            marker = (f"[ошибка чанка {_format_ts(c.start_sec)}-"
                      f"{_format_ts(c.end_sec)}: {c.error}]")
            parts.append(marker)
    return joiner.join(parts)


# ── Orchestration ──

ProgressCallback = Callable[[int, int], None]


class LongTranscriber:
    """Chunked + parallel transcription wrapper around any BaseTranscriber."""

    def __init__(self, base: BaseTranscriber, config: Config) -> None:
        self._base = base
        self._config = config

    def transcribe_file(
        self,
        audio_path: Path,
        progress_cb: ProgressCallback | None = None,
    ) -> str:
        """Load WAV, chunk, transcribe in parallel, stitch. Returns full text."""
        t0 = time.monotonic()
        audio, sr = load_wav(audio_path)
        duration = len(audio) / sr
        log.info("LongTranscriber: loaded %.1fs from %s", duration, audio_path)

        cuts = find_silence_cuts(
            audio, sr,
            target_sec=self._config.session_chunk_minutes * 60,
            min_sec=self._config.session_chunk_min_minutes * 60,
            max_sec=self._config.session_chunk_max_minutes * 60,
            silence_rms=self._config.session_silence_rms,
            silence_min_ms=self._config.session_silence_min_ms,
        )
        log.info("LongTranscriber: split into %d chunks", len(cuts))

        results: list[ChunkResult] = [None] * len(cuts)  # type: ignore[list-item]
        done_count = 0
        workers = max(1, self._config.session_parallel_workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._transcribe_chunk, audio[s:e], sr, i,
                    s / sr, e / sr,
                ): i
                for i, (s, e) in enumerate(cuts)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()
                done_count += 1
                if progress_cb is not None:
                    try:
                        progress_cb(done_count, len(cuts))
                    except Exception:
                        log.exception("progress_cb raised (ignored)")

        text = stitch_texts(results, joiner=self._config.session_chunk_joiner)
        elapsed = time.monotonic() - t0
        ratio = elapsed / duration if duration > 0 else 0
        ok = sum(1 for r in results if r.ok)
        log.info(
            "LongTranscriber: done in %.1fs (%.2fx realtime), %d/%d chunks ok",
            elapsed, ratio, ok, len(cuts))
        return text

    def _transcribe_chunk(
        self,
        audio: np.ndarray,
        sample_rate: int,
        index: int,
        start_sec: float,
        end_sec: float,
    ) -> ChunkResult:
        """Transcribe a single chunk with retry on transient failures."""
        _ = sample_rate  # base transcriber uses config.sample_rate
        last_err: Exception | None = None
        for attempt in range(_MAX_CHUNK_ATTEMPTS):
            if attempt > 0:
                delay = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                log.warning(
                    "Chunk %d: retry %d/%d after %.1fs (last error: %s)",
                    index, attempt, _MAX_CHUNK_ATTEMPTS - 1, delay, last_err)
                time.sleep(delay)
            try:
                # apply_cleanup=False: cleanup is only for short dictation,
                # never per-chunk in long sessions.
                text = self._base.transcribe(audio, apply_cleanup=False)
                return ChunkResult(
                    index=index, start_sec=start_sec, end_sec=end_sec,
                    text=text, error=None)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                continue
            except RuntimeError as e:
                # Transcriber raises RuntimeError for HTTP 5xx etc.
                msg = str(e).lower()
                if "5" in msg and ("error" in msg or "status" in msg):
                    last_err = e
                    continue
                # Non-retryable
                log.exception("Chunk %d: non-retryable error", index)
                return ChunkResult(
                    index=index, start_sec=start_sec, end_sec=end_sec,
                    text="", error=str(e))
            except Exception as e:
                log.exception("Chunk %d: unexpected error", index)
                return ChunkResult(
                    index=index, start_sec=start_sec, end_sec=end_sec,
                    text="", error=str(e))

        log.error("Chunk %d: failed after %d attempts",
                  index, _MAX_CHUNK_ATTEMPTS)
        return ChunkResult(
            index=index, start_sec=start_sec, end_sec=end_sec,
            text="", error=str(last_err) if last_err else "unknown error")
