"""Tests for SessionRecorder.

Covers:
- write_chunk_to_wav (pure function): round-trip WAV correctness, metrics.
- SessionRecorder end-to-end with a fake InputStream that feeds prerecorded
  audio via the callback — verifies file, DB integration, and metrics
  without requiring real microphone access.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch, MagicMock

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class WriteChunkToWavTests(unittest.TestCase):
    def test_roundtrip_preserves_samples(self) -> None:
        from session_recorder import write_chunk_to_wav

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            # 1 second of 440 Hz sine at amplitude 0.5
            sr = 16000
            t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
            signal = 0.5 * np.sin(2 * np.pi * 440 * t)

            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                samples, peak, sum_sq = write_chunk_to_wav(wf, signal)

            self.assertEqual(samples, sr)
            self.assertAlmostEqual(peak, 0.5, places=2)
            # RMS of 0.5-amplitude sine = 0.5/sqrt(2) ≈ 0.3536
            rms = float(np.sqrt(sum_sq / samples))
            self.assertAlmostEqual(rms, 0.3536, places=2)

            with wave.open(tmp.name, "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), sr)
                self.assertEqual(wf.getnframes(), sr)
        finally:
            os.unlink(tmp.name)

    def test_empty_chunk(self) -> None:
        from session_recorder import write_chunk_to_wav

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                samples, peak, sum_sq = write_chunk_to_wav(
                    wf, np.array([], dtype=np.float32))
            self.assertEqual(samples, 0)
            self.assertEqual(peak, 0.0)
            self.assertEqual(sum_sq, 0.0)
        finally:
            os.unlink(tmp.name)

    def test_clipping(self) -> None:
        from session_recorder import write_chunk_to_wav

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            # Samples above 1.0 should be clipped
            loud = np.array([2.0, -2.0, 0.5, -0.5], dtype=np.float32)
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                _, peak, _ = write_chunk_to_wav(wf, loud)
            # Peak reports the raw pre-clip value
            self.assertEqual(peak, 2.0)

            with wave.open(tmp.name, "rb") as wf:
                pcm = np.frombuffer(wf.readframes(4), dtype=np.int16)
            # Clipped to ±32767
            self.assertEqual(pcm[0], 32767)
            self.assertEqual(pcm[1], -32767)
        finally:
            os.unlink(tmp.name)


class FakeInputStream:
    """Stand-in for sounddevice.InputStream that pumps prerecorded audio
    through the callback from a background thread. Mimics the Mixxx
    serialization pattern (callback raises CallbackAbort → finished_callback)."""

    def __init__(self, audio: np.ndarray, sample_rate: int, callback,
                 finished_callback, **kwargs):
        self._audio = audio
        self._sample_rate = sample_rate
        self._callback = callback
        self._finished_callback = finished_callback
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.active = False

    def start(self) -> None:
        import sounddevice as sd
        self.active = True
        self._stop.clear()

        def pump():
            block_size = 1024
            i = 0
            try:
                while not self._stop.is_set() and i < len(self._audio):
                    chunk = self._audio[i:i + block_size]
                    if len(chunk) == 0:
                        break
                    # Shape as (frames, channels)
                    indata = chunk.reshape(-1, 1)
                    try:
                        self._callback(indata, len(chunk), None, None)
                    except sd.CallbackAbort:
                        break
                    i += block_size
                    # Simulate real-time pacing at 100x speed
                    time.sleep(block_size / self._sample_rate / 100)
            finally:
                self.active = False
                if self._finished_callback:
                    self._finished_callback()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class SessionRecorderIntegrationTests(unittest.TestCase):
    """End-to-end SessionRecorder with a fake InputStream + fake device query."""

    def setUp(self) -> None:
        # Isolate DB to temp file
        self._db_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_tmp.close()
        self._db_patcher = patch("db._db_path", self._db_tmp.name)
        self._db_patcher.start()

        import db
        db.init_sessions_table()

        # Isolate session audio directory
        self._session_dir = tempfile.mkdtemp(prefix="session-test-")

        # Build config that points to isolated session dir
        from config import Config
        self._config = Config()
        # Override session_audio_dir via monkey-patching the property path
        self._config_patcher = patch(
            "config.Config.session_audio_dir",
            new_callable=lambda: property(lambda s: self._session_dir))
        self._config_patcher.start()

        # Test audio: 2 sec of 0.3-amplitude sine @ 440Hz, 16kHz mono
        sr = self._config.sample_rate
        t = np.linspace(0, 2.0, int(sr * 2), endpoint=False, dtype=np.float32)
        self._test_audio = 0.3 * np.sin(2 * np.pi * 440 * t)

    def tearDown(self) -> None:
        self._config_patcher.stop()
        self._db_patcher.stop()
        os.unlink(self._db_tmp.name)
        import shutil
        shutil.rmtree(self._session_dir, ignore_errors=True)

    def _make_stream_factory(self):
        """Returns a callable that sounddevice.InputStream will be patched to."""
        test_audio = self._test_audio
        sr = self._config.sample_rate

        def factory(**kwargs):
            return FakeInputStream(
                audio=test_audio,
                sample_rate=sr,
                callback=kwargs["callback"],
                finished_callback=kwargs["finished_callback"],
            )
        return factory

    def test_start_creates_db_row_and_opens_wav(self) -> None:
        from session_recorder import SessionRecorder
        import db

        with patch("sounddevice.InputStream",
                   side_effect=self._make_stream_factory()), \
             patch("sounddevice.query_devices",
                   return_value=[{"name": "Built-in", "max_input_channels": 1}]), \
             patch("sounddevice.default", MagicMock(device=[0, 0])):

            rec = SessionRecorder(self._config)
            meta = rec.start()

            self.assertTrue(rec.is_recording)
            self.assertIsNotNone(meta.session_id)
            self.assertTrue(meta.audio_path.exists())

            row = db.session_get(meta.session_id)
            self.assertEqual(row["status"], "recording")
            self.assertEqual(row["audio_path"], str(meta.audio_path))

            rec.cancel()

    def test_full_recording_roundtrip(self) -> None:
        from session_recorder import SessionRecorder
        import db

        with patch("sounddevice.InputStream",
                   side_effect=self._make_stream_factory()), \
             patch("sounddevice.query_devices",
                   return_value=[{"name": "Built-in", "max_input_channels": 1}]), \
             patch("sounddevice.default", MagicMock(device=[0, 0])):

            rec = SessionRecorder(self._config)
            meta = rec.start()

            # Let the fake stream feed audio (2 sec at 100x → ~20ms real time)
            time.sleep(0.5)

            final = rec.stop()

            # Session marked transcribing in DB
            row = db.session_get(final.session_id)
            self.assertEqual(row["status"], "transcribing")
            self.assertGreater(row["duration_sec"], 0)

            # WAV file exists and contains audio
            self.assertTrue(final.audio_path.exists())
            with wave.open(str(final.audio_path), "rb") as wf:
                self.assertEqual(wf.getframerate(), 16000)
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertGreater(wf.getnframes(), 0)

            # Metrics populated
            self.assertGreater(final.peak, 0)
            self.assertGreater(final.rms, 0)

    def test_cancel_deletes_wav_and_marks_db(self) -> None:
        from session_recorder import SessionRecorder
        import db

        with patch("sounddevice.InputStream",
                   side_effect=self._make_stream_factory()), \
             patch("sounddevice.query_devices",
                   return_value=[{"name": "Built-in", "max_input_channels": 1}]), \
             patch("sounddevice.default", MagicMock(device=[0, 0])):

            rec = SessionRecorder(self._config)
            meta = rec.start()
            time.sleep(0.3)

            rec.cancel()

            self.assertFalse(meta.audio_path.exists())
            row = db.session_get(meta.session_id)
            self.assertEqual(row["status"], "cancelled")

    def test_double_start_raises(self) -> None:
        from session_recorder import SessionRecorder

        with patch("sounddevice.InputStream",
                   side_effect=self._make_stream_factory()), \
             patch("sounddevice.query_devices",
                   return_value=[{"name": "Built-in", "max_input_channels": 1}]), \
             patch("sounddevice.default", MagicMock(device=[0, 0])):

            rec = SessionRecorder(self._config)
            rec.start()
            try:
                with self.assertRaises(RuntimeError):
                    rec.start()
            finally:
                rec.cancel()


if __name__ == "__main__":
    unittest.main()
