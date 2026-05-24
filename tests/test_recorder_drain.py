"""Tests for Recorder's tail-preservation contract.

Regression guard for the v1.2.0 bug where the callback checked `_should_stop`
BEFORE appending and `stop()` snapshotted chunks immediately instead of
waiting for `finished_callback`. Both together dropped ~20–100 ms of audio
at the end of every short dictation.
"""

import os
import sys
import threading
import time
import unittest

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class CallbackOrderTests(unittest.TestCase):
    """The callback that raises CallbackStop must append its indata first,
    or the last buffer before shutdown is lost."""

    def _make_recorder(self):
        from config import Config
        from recorder import Recorder
        return Recorder(Config())

    def test_callback_appends_before_raising_stop(self) -> None:
        rec = self._make_recorder()
        rec._recording = True
        rec._should_stop = True  # pre-set — next callback will raise

        indata = np.full((512, 1), 0.5, dtype=np.float32)
        with self.assertRaises(sd.CallbackStop):
            rec._callback(indata, 512, None, None)

        # The indata MUST have made it into _chunks even though we raised
        self.assertEqual(len(rec._chunks), 1)
        np.testing.assert_array_equal(rec._chunks[0], indata)

    def test_callback_uses_callback_stop_not_abort(self) -> None:
        """CallbackStop (paComplete) is the documented clean-shutdown signal.
        CallbackAbort (paAbort) discards more of the HAL buffer."""
        rec = self._make_recorder()
        rec._recording = True
        rec._should_stop = True

        indata = np.zeros((512, 1), dtype=np.float32)
        # Specifically CallbackStop, not CallbackAbort
        with self.assertRaises(sd.CallbackStop):
            rec._callback(indata, 512, None, None)

    def test_callback_appends_without_recording_flag(self) -> None:
        """Post-fix, the callback no longer gates on _recording — shutdown
        callbacks (between _should_stop and finished_callback) still append.
        Snapshot timing is enforced in stop(), not in the callback."""
        rec = self._make_recorder()
        rec._recording = False  # not "recording"
        rec._should_stop = False

        indata = np.ones((256, 1), dtype=np.float32)
        rec._callback(indata, 256, None, None)

        # Even with _recording=False, the chunk is captured.
        # stop() is responsible for the snapshot boundary.
        self.assertEqual(len(rec._chunks), 1)


class StopContractTests(unittest.TestCase):
    """stop() must wait for finished_callback before snapshotting, and must
    append 150 ms of silence to help Whisper's decoder."""

    def _make_recorder(self):
        from config import Config
        from recorder import Recorder
        return Recorder(Config())

    def test_stop_waits_for_finished_callback(self) -> None:
        """If finished_callback fires 100 ms late, stop() must block for it
        and include any audio appended during that wait."""
        rec = self._make_recorder()

        # Fake an "active" stream so stop() takes the drain path
        class FakeStream:
            active = True
            def close(self): pass
        rec._stream = FakeStream()
        rec._recording = True

        # Seed one chunk of initial audio
        rec._chunks.append(np.ones((1000, 1), dtype=np.float32) * 0.3)

        # After 50 ms: simulate a late callback + finished_callback firing
        def late_arrival():
            time.sleep(0.05)
            with rec._lock:
                rec._chunks.append(
                    np.ones((500, 1), dtype=np.float32) * 0.4)
            rec._stream_finished.set()

        threading.Thread(target=late_arrival, daemon=True).start()

        t0 = time.monotonic()
        audio = rec.stop()
        elapsed = time.monotonic() - t0

        # Should have waited for the late arrival (~50 ms)
        self.assertGreaterEqual(elapsed, 0.04)
        # And captured both chunks (1000 + 500 samples) plus padding
        sr = rec._config.sample_rate
        expected_pad = int(sr * 0.15)
        self.assertEqual(len(audio), 1500 + expected_pad)

    def test_stop_appends_150ms_silence(self) -> None:
        rec = self._make_recorder()
        rec._stream = None  # no-stream fast path

        # One second of constant-amplitude audio
        sr = rec._config.sample_rate
        rec._chunks.append(np.full((sr, 1), 0.5, dtype=np.float32))

        audio = rec.stop()

        expected_pad = int(sr * 0.15)
        self.assertEqual(len(audio), sr + expected_pad)
        # Last 150 ms must be exactly zero
        np.testing.assert_array_equal(
            audio[-expected_pad:],
            np.zeros(expected_pad, dtype=np.float32),
        )
        # The rest is the original audio
        np.testing.assert_array_equal(
            audio[:sr],
            np.full(sr, 0.5, dtype=np.float32),
        )

    def test_stop_empty_chunks_returns_empty(self) -> None:
        rec = self._make_recorder()
        rec._stream = None

        audio = rec.stop()
        self.assertEqual(len(audio), 0)

    def test_stop_timeout_still_returns_what_was_captured(self) -> None:
        """If finished_callback never fires (runaway stream), stop() returns
        after 3 s with the audio it has — we don't want to block forever."""
        rec = self._make_recorder()

        class FakeStream:
            active = True
            def close(self): pass
        rec._stream = FakeStream()
        rec._chunks.append(np.ones((200, 1), dtype=np.float32))

        # Monkey-patch the wait to return False immediately (simulate timeout
        # without actually waiting 3 s in the test).
        original_wait = rec._stream_finished.wait
        rec._stream_finished.wait = lambda timeout=None: False
        try:
            audio = rec.stop()
        finally:
            rec._stream_finished.wait = original_wait

        sr = rec._config.sample_rate
        expected_pad = int(sr * 0.15)
        self.assertEqual(len(audio), 200 + expected_pad)
        # _should_stop should have been reset so a reused stream doesn't
        # keep aborting
        self.assertFalse(rec._should_stop)

    def test_stop_records_metrics_on_captured_audio_not_padding(self) -> None:
        """Diagnostic metrics (RMS, peak, duration) should reflect the real
        recording, not the padded-with-silence version."""
        rec = self._make_recorder()
        rec._stream = None

        sr = rec._config.sample_rate
        # 1 s of 0.4-amplitude audio → RMS = 0.4, peak = 0.4, duration = 1.0
        rec._chunks.append(np.full((sr, 1), 0.4, dtype=np.float32))

        rec.stop()

        self.assertAlmostEqual(rec.last_duration, 1.0, places=3)
        self.assertAlmostEqual(rec.last_peak, 0.4, places=3)
        # RMS of constant 0.4 = 0.4
        self.assertAlmostEqual(rec.last_rms, 0.4, places=3)


if __name__ == "__main__":
    unittest.main()
