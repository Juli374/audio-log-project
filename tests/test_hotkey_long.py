"""Tests for the Left Option long-session hotkey handling.

The handler is driven by NSEvent instances — we mock those with a minimal
stub. The actual NSEvent monitor lifecycle is not exercised (requires a
running NSApplication), but `_handle_long()` is pure logic and can be
tested directly.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class DoubleTapTests(unittest.TestCase):
    def _make_hotkey(self, require_double_tap=True, long_keycode=58):
        from config import Config
        from hotkey import HotkeyListener

        config = Config()
        config.session_hotkey_keycode = long_keycode
        config.session_hotkey_require_double_tap = require_double_tap
        on_long_toggle = MagicMock()
        hk = HotkeyListener(
            config=config,
            on_activate=lambda: None,
            on_deactivate=lambda: None,
            on_cancel=lambda: None,
            on_long_toggle=on_long_toggle,
        )
        return hk, on_long_toggle

    def test_single_tap_does_not_fire_in_double_tap_mode(self):
        hk, cb = self._make_hotkey(require_double_tap=True)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            # First tap should not fire
            mock_helper.callAfter.assert_not_called()

    def test_double_tap_fires(self):
        hk, cb = self._make_hotkey(require_double_tap=True)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            # Simulate second tap within window
            hk._handle_long(is_pressed=True)
            mock_helper.callAfter.assert_called_once_with(cb)

    def test_slow_double_tap_does_not_fire(self):
        hk, cb = self._make_hotkey(require_double_tap=True)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            # Simulate gap > 0.5s by manipulating _last_long_tap
            hk._last_long_tap = time.monotonic() - 1.0
            hk._handle_long(is_pressed=True)
            # Should restart the wait, not fire
            mock_helper.callAfter.assert_not_called()

    def test_release_does_not_fire(self):
        hk, cb = self._make_hotkey(require_double_tap=True)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            hk._handle_long(is_pressed=False)  # release
            hk._handle_long(is_pressed=False)  # second release
            mock_helper.callAfter.assert_not_called()

    def test_single_press_mode_fires_immediately(self):
        hk, cb = self._make_hotkey(require_double_tap=False)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            mock_helper.callAfter.assert_called_once_with(cb)

    def test_single_press_mode_debounced(self):
        hk, cb = self._make_hotkey(require_double_tap=False)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            # Second press 50ms later — ignored by debounce
            hk._handle_long(is_pressed=True)
            self.assertEqual(mock_helper.callAfter.call_count, 1)

    def test_triple_tap_fires_only_once(self):
        """After double-tap fires, third quick tap should not immediately
        trigger another fire — internal state is reset."""
        hk, cb = self._make_hotkey(require_double_tap=True)
        with patch("hotkey.AppHelper") as mock_helper:
            hk._handle_long(is_pressed=True)
            hk._handle_long(is_pressed=True)  # double → fires
            hk._handle_long(is_pressed=True)  # this becomes "first" of next cycle
            self.assertEqual(mock_helper.callAfter.call_count, 1)

    def test_long_toggle_not_fired_without_callback(self):
        from config import Config
        from hotkey import HotkeyListener

        config = Config()
        hk = HotkeyListener(
            config=config,
            on_activate=lambda: None,
            on_deactivate=lambda: None,
            on_long_toggle=None,  # disabled
        )
        # Build a fake event for long keycode
        event = MagicMock()
        event.keyCode.return_value = config.session_hotkey_keycode
        event.modifierFlags.return_value = 1 << 19  # Option flag
        # Should not crash
        hk._handle(event)


if __name__ == "__main__":
    unittest.main()
