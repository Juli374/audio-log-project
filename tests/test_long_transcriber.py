"""Tests for long_transcriber: silence-based chunking, stitching, retries."""

import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class LoadWavTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        from long_transcriber import load_wav

        sr = 16000
        # Create simple sine wave
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        signal = 0.5 * np.sin(2 * np.pi * 440 * t)

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                pcm = (np.clip(signal, -1, 1) * 32767).astype(np.int16)
                wf.writeframes(pcm.tobytes())

            audio, loaded_sr = load_wav(tmp.name)
            self.assertEqual(loaded_sr, sr)
            self.assertEqual(len(audio), sr)
            # Amplitude should be close to 0.5
            self.assertAlmostEqual(float(np.max(np.abs(audio))), 0.5, places=2)
        finally:
            os.unlink(tmp.name)

    def test_rejects_stereo(self) -> None:
        from long_transcriber import load_wav

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(2)  # stereo — not supported
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00\x00\x00" * 100)
            with self.assertRaises(ValueError):
                load_wav(tmp.name)
        finally:
            os.unlink(tmp.name)


class RMSEnvelopeTests(unittest.TestCase):
    def test_envelope_length(self) -> None:
        from long_transcriber import compute_rms_envelope

        sr = 16000
        # 1 second at 20ms windows = 50 windows
        audio = np.random.randn(sr).astype(np.float32) * 0.1
        env = compute_rms_envelope(audio, sr, window_ms=20)
        self.assertEqual(len(env), 50)
        self.assertTrue(np.all(env >= 0))

    def test_silence_gives_zero(self) -> None:
        from long_transcriber import compute_rms_envelope

        audio = np.zeros(16000, dtype=np.float32)
        env = compute_rms_envelope(audio, 16000)
        self.assertEqual(float(np.max(env)), 0.0)


class FindSilenceCutsTests(unittest.TestCase):
    def test_short_audio_single_chunk(self) -> None:
        from long_transcriber import find_silence_cuts

        # 30 seconds of random audio, chunk window [5min, 15min] — fits entirely
        sr = 16000
        audio = np.random.randn(30 * sr).astype(np.float32) * 0.1
        cuts = find_silence_cuts(audio, sr)
        self.assertEqual(cuts, [(0, 30 * sr)])

    def test_cuts_at_silence(self) -> None:
        from long_transcriber import find_silence_cuts

        # Build 20 min of noise with a 1-sec silence at ~10 min mark
        sr = 16000
        noise = np.random.randn(20 * 60 * sr).astype(np.float32) * 0.2
        # Insert 1 sec silence at 10:00
        silence_start = 10 * 60 * sr
        noise[silence_start:silence_start + sr] = 0.0

        cuts = find_silence_cuts(
            noise, sr,
            target_sec=600, min_sec=300, max_sec=900,
            silence_rms=0.01, silence_min_ms=300,
        )
        self.assertEqual(len(cuts), 2)
        # First chunk should end in the silence window (10:00 ± 0.5 sec)
        first_end = cuts[0][1]
        self.assertGreater(first_end, silence_start - sr)
        self.assertLess(first_end, silence_start + 2 * sr)
        # Chunks must cover the whole audio
        self.assertEqual(cuts[0][0], 0)
        self.assertEqual(cuts[-1][1], len(noise))
        # Chunks must be contiguous
        for i in range(len(cuts) - 1):
            self.assertEqual(cuts[i][1], cuts[i + 1][0])

    def test_fallback_when_no_silence(self) -> None:
        from long_transcriber import find_silence_cuts

        # 20 min of continuous noise, no silence
        sr = 16000
        noise = np.random.randn(20 * 60 * sr).astype(np.float32) * 0.2
        cuts = find_silence_cuts(
            noise, sr,
            target_sec=600, min_sec=300, max_sec=900,
            silence_rms=0.01, silence_min_ms=300,
        )
        # Should still produce valid contiguous coverage
        self.assertGreaterEqual(len(cuts), 1)
        self.assertEqual(cuts[0][0], 0)
        self.assertEqual(cuts[-1][1], len(noise))
        for i in range(len(cuts) - 1):
            self.assertEqual(cuts[i][1], cuts[i + 1][0])

    def test_chunks_within_size_bounds(self) -> None:
        from long_transcriber import find_silence_cuts

        sr = 16000
        audio = np.random.randn(45 * 60 * sr).astype(np.float32) * 0.2
        cuts = find_silence_cuts(
            audio, sr,
            target_sec=600, min_sec=300, max_sec=900,
            silence_rms=0.01, silence_min_ms=300,
        )
        for i, (s, e) in enumerate(cuts):
            length_sec = (e - s) / sr
            # Last chunk can be shorter (remainder); all others must fit bounds
            if i < len(cuts) - 1:
                self.assertGreaterEqual(length_sec, 300 - 1)
                self.assertLessEqual(length_sec, 900 + 1)
            else:
                self.assertLessEqual(length_sec, 900 + 1)


class StitchTextsTests(unittest.TestCase):
    def test_orders_by_index(self) -> None:
        from long_transcriber import ChunkResult, stitch_texts

        chunks = [
            ChunkResult(2, 120, 180, "world"),
            ChunkResult(0, 0, 60, "hello"),
            ChunkResult(1, 60, 120, "cruel"),
        ]
        out = stitch_texts(chunks, joiner=" ")
        self.assertEqual(out, "hello cruel world")

    def test_failed_chunk_produces_marker(self) -> None:
        from long_transcriber import ChunkResult, stitch_texts

        chunks = [
            ChunkResult(0, 0, 60, "hello"),
            ChunkResult(1, 60, 120, "", error="timeout"),
            ChunkResult(2, 120, 180, "world"),
        ]
        out = stitch_texts(chunks, joiner="\n\n")
        self.assertIn("hello", out)
        self.assertIn("world", out)
        self.assertIn("[ошибка чанка", out)
        self.assertIn("timeout", out)

    def test_empty_text_skipped(self) -> None:
        from long_transcriber import ChunkResult, stitch_texts

        chunks = [
            ChunkResult(0, 0, 60, "hello"),
            ChunkResult(1, 60, 120, "   "),  # whitespace only
            ChunkResult(2, 120, 180, "world"),
        ]
        out = stitch_texts(chunks, joiner=" | ")
        self.assertEqual(out, "hello | world")


class LongTranscriberIntegrationTests(unittest.TestCase):
    """End-to-end with a FakeBaseTranscriber."""

    def _make_wav(self, audio: np.ndarray, sr: int) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            wf.writeframes(pcm.tobytes())
        return tmp.name

    def test_transcribe_file_parallel(self) -> None:
        from config import Config
        from long_transcriber import LongTranscriber
        from transcriber import BaseTranscriber

        sr = 16000
        # 30 min of noise with silence at 10:00 and 20:00 → 3 chunks expected.
        # (25 min would leave a 15 min tail that fits in max_chunk, so the
        #  algorithm correctly makes 2 chunks — we need >30 min to force 3.)
        audio = np.random.randn(30 * 60 * sr).astype(np.float32) * 0.2
        audio[10 * 60 * sr: 10 * 60 * sr + sr] = 0.0
        audio[20 * 60 * sr: 20 * 60 * sr + sr] = 0.0
        wav_path = self._make_wav(audio, sr)

        try:
            config = Config()
            config.session_chunk_minutes = 10
            config.session_chunk_min_minutes = 5
            config.session_chunk_max_minutes = 15
            config.session_parallel_workers = 3
            config.session_silence_rms = 0.01
            config.session_silence_min_ms = 300
            config.session_chunk_joiner = " | "

            class FakeBase(BaseTranscriber):
                def __init__(self, cfg):
                    super().__init__(cfg)
                    self.calls = 0
                    self.lock = threading.Lock()

                def transcribe(self, audio):
                    with self.lock:
                        self.calls += 1
                        n = self.calls
                    return f"chunk{n}"

            base = FakeBase(config)
            lt = LongTranscriber(base, config)

            progress: list[tuple[int, int]] = []
            result = lt.transcribe_file(wav_path, progress_cb=lambda d, t: progress.append((d, t)))

            self.assertEqual(base.calls, 3)
            # Text should contain three chunk markers joined by " | "
            parts = result.split(" | ")
            self.assertEqual(len(parts), 3)
            # Progress callback called for each completion, ending at (3, 3)
            self.assertEqual(progress[-1], (3, 3))
        finally:
            os.unlink(wav_path)

    def test_transcribe_file_retries_transient(self) -> None:
        from config import Config
        from long_transcriber import LongTranscriber
        from transcriber import BaseTranscriber
        import urllib.error

        sr = 16000
        audio = np.random.randn(8 * 60 * sr).astype(np.float32) * 0.2  # 8 min, single chunk
        wav_path = self._make_wav(audio, sr)

        try:
            config = Config()
            config.session_chunk_minutes = 10
            config.session_parallel_workers = 1

            class FlakeyBase(BaseTranscriber):
                def __init__(self, cfg):
                    super().__init__(cfg)
                    self.calls = 0

                def transcribe(self, audio):
                    self.calls += 1
                    if self.calls < 3:
                        raise urllib.error.URLError("simulated network")
                    return "recovered"

            base = FlakeyBase(config)
            lt = LongTranscriber(base, config)

            # Patch backoff to be instant
            with patch("long_transcriber._RETRY_BACKOFF", [0.01, 0.01, 0.01]):
                result = lt.transcribe_file(wav_path)

            self.assertEqual(base.calls, 3)
            self.assertIn("recovered", result)
        finally:
            os.unlink(wav_path)

    def test_transcribe_file_marks_failed_chunks(self) -> None:
        from config import Config
        from long_transcriber import LongTranscriber
        from transcriber import BaseTranscriber
        import urllib.error

        sr = 16000
        audio = np.random.randn(8 * 60 * sr).astype(np.float32) * 0.2
        wav_path = self._make_wav(audio, sr)

        try:
            config = Config()

            class AlwaysFailBase(BaseTranscriber):
                def transcribe(self, audio):
                    raise urllib.error.URLError("persistent failure")

            base = AlwaysFailBase(config)
            lt = LongTranscriber(base, config)

            with patch("long_transcriber._RETRY_BACKOFF", [0.01, 0.01, 0.01]):
                result = lt.transcribe_file(wav_path)

            self.assertIn("[ошибка чанка", result)
            self.assertIn("persistent failure", result)
        finally:
            os.unlink(wav_path)


if __name__ == "__main__":
    unittest.main()
