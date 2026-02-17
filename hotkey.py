"""Hotkey listener using native macOS NSEvent global monitor."""

import threading
import time

from AppKit import NSEvent
from PyObjCTools import AppHelper
from Quartz import CGEventSourceFlagsState, kCGEventSourceStateHIDSystemState
from utils import get_logger

log = get_logger(__name__)

# NSEventMaskFlagsChanged
_FLAGS_CHANGED_MASK = 1 << 12

# NSEventModifierFlagOption
_OPTION_FLAG = 1 << 19

# macOS virtual keycode for Right Option
_RIGHT_OPTION_KEYCODE = 61

# Polling interval for detecting missed key-up events
_POLL_INTERVAL = 0.5

# Minimum interval between toggle actions (prevents double-trigger from
# held key repeats or bouncing contacts)
_TOGGLE_DEBOUNCE = 0.3


class HotkeyListener:
    """Monitors Right Option key press/release via NSEvent global monitor.

    The handler is called on the main thread's run loop, so it requires
    NSApplication to be running (rumps provides this).

    Includes active polling via CGEventSourceFlagsState to detect missed
    key-up events (can happen after sleep, idle, or focus loss).
    """

    def __init__(self, config, on_activate, on_deactivate):
        self._config = config
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._keycode = getattr(config, "hotkey_keycode", _RIGHT_OPTION_KEYCODE)
        self._pressed = False
        self._recording = False  # for toggle mode
        self._last_toggle_time = 0.0  # monotonic timestamp of last toggle action
        self._monitor = None
        self._poll_timer: threading.Timer | None = None

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

        if getattr(self._config, "hotkey_mode", "hold") == "toggle":
            self._handle_toggle(option_pressed)
        else:
            self._handle_hold(option_pressed)

    def _handle_hold(self, option_pressed):
        """Hold mode: hold key to record, release to stop."""
        if option_pressed and not self._pressed:
            self._pressed = True
            self._start_polling()
            try:
                self._on_activate()
            except Exception:
                log.exception("Error in on_activate")

        elif not option_pressed and self._pressed:
            self._pressed = False
            self._cancel_polling()
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in on_deactivate")

    def _handle_toggle(self, option_pressed):
        """Toggle mode: press once to start, press again to stop.

        Uses time-based debounce instead of _pressed flag to avoid getting
        stuck when macOS drops a key-up event.
        """
        if not option_pressed:
            return

        now = time.monotonic()
        if now - self._last_toggle_time < _TOGGLE_DEBOUNCE:
            return
        self._last_toggle_time = now

        if not self._recording:
            self._recording = True
            log.info("Toggle: recording ON")
            try:
                self._on_activate()
            except Exception:
                self._recording = False
                log.exception("Error in on_activate (toggle)")
        else:
            self._recording = False
            log.info("Toggle: recording OFF")
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in on_deactivate (toggle)")

    def reset_toggle(self):
        """Reset toggle state. Called externally when recording is
        force-stopped (e.g. by safety timer)."""
        self._recording = False
        self._pressed = False

    def _start_polling(self):
        """Start polling chain to detect missed key-up events."""
        self._cancel_polling()
        self._poll_timer = threading.Timer(_POLL_INTERVAL, self._poll_key_state)
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def _cancel_polling(self):
        """Cancel the active polling timer."""
        if self._poll_timer is not None:
            self._poll_timer.cancel()
            self._poll_timer = None

    def _poll_key_state(self):
        """Check real modifier flags via Quartz. If Option is not pressed
        but _pressed is True, we missed a key-up event."""
        if not self._pressed:
            return

        flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)
        option_held = bool(flags & _OPTION_FLAG)

        if not option_held:
            log.warning("Polling detected missed key-up — forcing deactivate")
            self._pressed = False
            self._recording = False
            try:
                AppHelper.callAfter(self._on_deactivate)
            except Exception:
                log.exception("Error in forced on_deactivate via polling")
            return

        # Still pressed — schedule next poll
        self._poll_timer = threading.Timer(_POLL_INTERVAL, self._poll_key_state)
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def stop(self):
        self._cancel_polling()
        if self._monitor:
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
            log.info("Hotkey monitor removed")
