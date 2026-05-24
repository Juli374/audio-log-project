"""Tests for the configurable-keycode hotkey logic.

Covers the new keycode→modifier-flag mapping and the live setters used
by the menubar picker. Pure logic — no NSEvent monitor lifecycle.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FlagForKeycodeTests(unittest.TestCase):
    def test_known_modifier_keys_have_correct_flags(self):
        from hotkey import (
            _FLAG_COMMAND,
            _FLAG_CONTROL,
            _FLAG_OPTION,
            _FLAG_SHIFT,
            flag_for_keycode,
        )
        # Right Option / Left Option → OPTION
        self.assertEqual(flag_for_keycode(61), _FLAG_OPTION)
        self.assertEqual(flag_for_keycode(58), _FLAG_OPTION)
        # Right Command / Left Command → COMMAND
        self.assertEqual(flag_for_keycode(54), _FLAG_COMMAND)
        self.assertEqual(flag_for_keycode(55), _FLAG_COMMAND)
        # Right Control / Left Control → CONTROL
        self.assertEqual(flag_for_keycode(62), _FLAG_CONTROL)
        self.assertEqual(flag_for_keycode(59), _FLAG_CONTROL)
        # Right Shift / Left Shift → SHIFT
        self.assertEqual(flag_for_keycode(60), _FLAG_SHIFT)
        self.assertEqual(flag_for_keycode(56), _FLAG_SHIFT)

    def test_unknown_keycode_falls_back_to_option(self):
        from hotkey import _FLAG_OPTION, flag_for_keycode
        # Unknown keycode (e.g. letter "A" = 0) → OPTION fallback
        self.assertEqual(flag_for_keycode(0), _FLAG_OPTION)
        self.assertEqual(flag_for_keycode(999), _FLAG_OPTION)


class HandleEventDispatchTests(unittest.TestCase):
    """Verify _handle() picks the correct flag based on the configured
    keycode — not always the OPTION flag."""

    def _make_event(self, keycode, flags):
        ev = MagicMock()
        ev.keyCode.return_value = keycode
        ev.modifierFlags.return_value = flags
        return ev

    def _make_hotkey(self, short_keycode):
        from config import Config
        from hotkey import HotkeyListener

        config = Config()
        config.hotkey_keycode = short_keycode
        config.hotkey_mode = "hold"
        on_activate = MagicMock()
        on_deactivate = MagicMock()
        hk = HotkeyListener(
            config=config,
            on_activate=on_activate,
            on_deactivate=on_deactivate,
        )
        return hk, on_activate, on_deactivate

    def test_right_command_press_activates_when_configured(self):
        from hotkey import _FLAG_COMMAND
        hk, on_act, on_deact = self._make_hotkey(short_keycode=54)
        ev = self._make_event(keycode=54, flags=_FLAG_COMMAND)
        hk._handle(ev)
        on_act.assert_called_once()
        on_deact.assert_not_called()

    def test_right_command_release_deactivates(self):
        from hotkey import _FLAG_COMMAND
        hk, on_act, on_deact = self._make_hotkey(short_keycode=54)
        # press
        hk._handle(self._make_event(keycode=54, flags=_FLAG_COMMAND))
        # release: same keycode but flag cleared
        hk._handle(self._make_event(keycode=54, flags=0))
        on_act.assert_called_once()
        on_deact.assert_called_once()

    def test_option_event_ignored_when_command_configured(self):
        """If user picked Right Command, a Right Option event must not
        accidentally fire — the keycode mismatch guards against it."""
        from hotkey import _FLAG_OPTION
        hk, on_act, _ = self._make_hotkey(short_keycode=54)
        ev = self._make_event(keycode=61, flags=_FLAG_OPTION)
        hk._handle(ev)
        on_act.assert_not_called()


class LiveSetterTests(unittest.TestCase):
    def _make_hotkey(self):
        from config import Config
        from hotkey import HotkeyListener

        config = Config()
        return HotkeyListener(
            config=config,
            on_activate=lambda: None,
            on_deactivate=lambda: None,
            on_long_toggle=lambda: None,
        )

    def test_set_keycode_updates_short_hotkey(self):
        hk = self._make_hotkey()
        self.assertEqual(hk._keycode, 61)  # default Right Option
        hk.set_keycode(54)  # Right Command
        self.assertEqual(hk._keycode, 54)

    def test_set_keycode_resets_press_state(self):
        hk = self._make_hotkey()
        hk._pressed = True
        hk._recording = True
        hk.set_keycode(54)
        self.assertFalse(hk._pressed)
        self.assertFalse(hk._recording)

    def test_set_keycode_noop_when_unchanged(self):
        hk = self._make_hotkey()
        hk._pressed = True  # Should NOT be reset for a no-op change
        hk.set_keycode(hk._keycode)
        self.assertTrue(hk._pressed)

    def test_set_long_keycode_updates_session_hotkey(self):
        hk = self._make_hotkey()
        hk.set_long_keycode(55)  # Left Command
        self.assertEqual(hk._long_keycode, 55)

    def test_after_set_keycode_new_event_dispatches(self):
        from hotkey import _FLAG_COMMAND
        from config import Config
        from hotkey import HotkeyListener

        config = Config()
        on_act = MagicMock()
        hk = HotkeyListener(
            config=config,
            on_activate=on_act,
            on_deactivate=lambda: None,
        )
        hk.set_keycode(54)  # Switch to Right Command live

        ev = MagicMock()
        ev.keyCode.return_value = 54
        ev.modifierFlags.return_value = _FLAG_COMMAND
        hk._handle(ev)
        on_act.assert_called_once()


if __name__ == "__main__":
    unittest.main()
