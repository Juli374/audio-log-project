"""A dead input device must never block recording.

Twice now this went wrong in opposite directions: a name-based filter
rejected working AirPods, then removing the filter let an unopenable
headset block a laptop that had a perfectly good built-in mic.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FAKE_DEVICES = [
    {"name": "AirPods Pro (Yulii) - Find My", "max_input_channels": 1},
    {"name": "Микрофон MacBook Air", "max_input_channels": 1},
    {"name": "BlackHole 2ch", "max_input_channels": 2},
]


class InputDeviceFallbackTests(unittest.TestCase):
    def _recorder(self):
        from config import Config
        from recorder import Recorder
        return Recorder(Config())

    def test_dead_default_falls_through_to_builtin(self):
        rec = self._recorder()
        with patch("recorder.sd.query_devices", return_value=FAKE_DEVICES), \
             patch("recorder.sd.default") as default, \
             patch.object(rec, "_can_open", side_effect=lambda i: i != 0):
            default.device = [0, 1]
            self.assertEqual(rec._pick_input_device(), 1)

    def test_working_default_is_used_even_when_named_find_my(self):
        rec = self._recorder()
        with patch("recorder.sd.query_devices", return_value=FAKE_DEVICES), \
             patch("recorder.sd.default") as default, \
             patch.object(rec, "_can_open", return_value=True):
            default.device = [0, 1]
            self.assertEqual(rec._pick_input_device(), 0)

    def test_virtual_device_is_last_resort_not_a_veto(self):
        rec = self._recorder()
        only_virtual = [{"name": "BlackHole 2ch", "max_input_channels": 2}]
        with patch("recorder.sd.query_devices", return_value=only_virtual), \
             patch("recorder.sd.default") as default, \
             patch.object(rec, "_can_open", return_value=True):
            default.device = [None, None]
            self.assertEqual(rec._pick_input_device(), 0)

    def test_returns_none_when_nothing_opens(self):
        rec = self._recorder()
        with patch("recorder.sd.query_devices", return_value=FAKE_DEVICES), \
             patch("recorder.sd.default") as default, \
             patch.object(rec, "_can_open", return_value=False):
            default.device = [0, 1]
            self.assertIsNone(rec._pick_input_device())


if __name__ == "__main__":
    unittest.main()
