"""Hotkey listener using native macOS NSEvent global monitor."""

import time

from AppKit import NSEvent
from utils import get_logger

log = get_logger(__name__)

# NSEventMaskFlagsChanged
_FLAGS_CHANGED_MASK = 1 << 12

# NSEventModifierFlagOption
_OPTION_FLAG = 1 << 19

# macOS virtual keycode for Right Option
_RIGHT_OPTION_KEYCODE = 61


class HotkeyListener:
    """Monitors Right Option key press/release via NSEvent global monitor.

    The handler is called on the main thread's run loop, so it requires
    NSApplication to be running (rumps provides this).
    """

    def __init__(self, config, on_activate, on_deactivate):
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._keycode = getattr(config, "hotkey_keycode", _RIGHT_OPTION_KEYCODE)
        self._pressed = False
        self._pressed_at: float = 0.0
        self._monitor = None

    def start(self):
        """Install global event monitor. Must be called on the main thread."""
        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            _FLAGS_CHANGED_MASK,
            self._handle,
        )
        if self._monitor:
            log.info("Hotkey monitor installed (keycode %d)", self._keycode)
        else:
            log.error(
                "Failed to install hotkey monitor. "
                "Grant Accessibility permission: System Settings → "
                "Privacy & Security → Accessibility → add AudioLog.app"
            )

    def _handle(self, event):
        if event.keyCode() != self._keycode:
            return

        option_pressed = bool(event.modifierFlags() & _OPTION_FLAG)

        # Guard: if key-up was missed (sleep, focus loss), auto-reset after 120s
        if self._pressed and (time.monotonic() - self._pressed_at) > 120:
            log.warning("Hotkey pressed state exceeded 120s — forcing reset")
            self._pressed = False
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in forced on_deactivate")

        if option_pressed and not self._pressed:
            self._pressed = True
            self._pressed_at = time.monotonic()
            try:
                self._on_activate()
            except Exception:
                log.exception("Error in on_activate")

        elif not option_pressed and self._pressed:
            self._pressed = False
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in on_deactivate")

    def stop(self):
        if self._monitor:
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
            log.info("Hotkey monitor removed")
