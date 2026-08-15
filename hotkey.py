"""Hotkey listener using native macOS NSEvent global monitor."""

import threading
import time

from AppKit import NSEvent
from PyObjCTools import AppHelper
from Quartz import (
    CGEventSourceFlagsState,
    CGEventSourceKeyState,
    kCGEventSourceStateHIDSystemState,
)
from utils import get_logger

log = get_logger(__name__)

# NSEventMaskFlagsChanged
_FLAGS_CHANGED_MASK = 1 << 12

# macOS virtual keycode for Escape
_ESC_KEYCODE = 53

# NSEvent device-independent modifier flags
_FLAG_SHIFT = 1 << 17    # NSEventModifierFlagShift
_FLAG_CONTROL = 1 << 18  # NSEventModifierFlagControl
_FLAG_OPTION = 1 << 19   # NSEventModifierFlagOption (Alt)
_FLAG_COMMAND = 1 << 20  # NSEventModifierFlagCommand (⌘)

# Backward-compat alias used in older external code / tests.
_OPTION_FLAG = _FLAG_OPTION

# macOS virtual keycodes for the supported modifier keys.
# Mapping each keycode to the modifier flag it carries — left/right of
# the same key share a flag (the keycode tells us which side, the flag
# tells us press vs release state).
_KEYCODE_COMMAND_LEFT = 55
_KEYCODE_COMMAND_RIGHT = 54
_KEYCODE_OPTION_LEFT = 58
_KEYCODE_OPTION_RIGHT = 61
_KEYCODE_CONTROL_LEFT = 59
_KEYCODE_CONTROL_RIGHT = 62
_KEYCODE_SHIFT_LEFT = 56
_KEYCODE_SHIFT_RIGHT = 60

_RIGHT_OPTION_KEYCODE = _KEYCODE_OPTION_RIGHT  # default short-dictation key

MODIFIER_KEY_FLAGS: dict[int, int] = {
    _KEYCODE_COMMAND_LEFT:  _FLAG_COMMAND,
    _KEYCODE_COMMAND_RIGHT: _FLAG_COMMAND,
    _KEYCODE_OPTION_LEFT:   _FLAG_OPTION,
    _KEYCODE_OPTION_RIGHT:  _FLAG_OPTION,
    _KEYCODE_CONTROL_LEFT:  _FLAG_CONTROL,
    _KEYCODE_CONTROL_RIGHT: _FLAG_CONTROL,
    _KEYCODE_SHIFT_LEFT:    _FLAG_SHIFT,
    _KEYCODE_SHIFT_RIGHT:   _FLAG_SHIFT,
}

# Display labels for the menubar UI. Order here is the order shown to
# the user — keep the most-likely picks first.
KEY_DISPLAY_NAMES: dict[int, str] = {
    _KEYCODE_OPTION_RIGHT:  "Right ⌥ (Option)",
    _KEYCODE_OPTION_LEFT:   "Left ⌥ (Option)",
    _KEYCODE_COMMAND_RIGHT: "Right ⌘ (Command)",
    _KEYCODE_COMMAND_LEFT:  "Left ⌘ (Command)",
    _KEYCODE_CONTROL_RIGHT: "Right ⌃ (Control)",
    _KEYCODE_CONTROL_LEFT:  "Left ⌃ (Control)",
    _KEYCODE_SHIFT_RIGHT:   "Right ⇧ (Shift)",
    _KEYCODE_SHIFT_LEFT:    "Left ⇧ (Shift)",
}


def flag_for_keycode(keycode: int) -> int:
    """Modifier flag carried by a given keycode.

    Falls back to OPTION for unknown keycodes — preserves legacy
    behaviour if a stale settings file holds something we don't know
    about.
    """
    return MODIFIER_KEY_FLAGS.get(keycode, _FLAG_OPTION)

# Polling interval for detecting missed key-up events
_POLL_INTERVAL = 0.5

# Polling interval for ESC key detection during recording
_ESC_POLL_INTERVAL = 0.1

# Minimum interval between toggle actions (prevents double-trigger from
# held key repeats or bouncing contacts)
_TOGGLE_DEBOUNCE = 0.3

# Window for detecting a double-tap of a bare modifier key. Two presses
# within this window = fire. Guards against false triggers when the user
# types ⌘C / Option+arrow etc. (any single press during typing won't
# match the double-tap pattern).
_DOUBLE_TAP_WINDOW = 0.5


class HotkeyListener:
    """Monitors Right Option key press/release via NSEvent monitors.

    Uses BOTH global and local monitors to catch events regardless of
    which app is active:
    - Global monitor: catches events when OTHER apps are key/active
    - Local monitor: catches events when THIS app is key/active

    The handler is called on the main thread's run loop, so it requires
    NSApplication to be running (rumps provides this).

    Includes active polling via CGEventSourceFlagsState to detect missed
    key-up events (can happen after sleep, idle, or focus loss).
    """

    def __init__(self, config, on_activate, on_deactivate, on_cancel=None,
                 on_translate_toggle=None):
        self._config = config
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._on_cancel = on_cancel
        self._on_translate_toggle = on_translate_toggle
        self._keycode = getattr(config, "hotkey_keycode", _RIGHT_OPTION_KEYCODE)
        self._translate_keycode = getattr(config, "translate_hotkey_keycode", None)
        self._pressed = False
        self._recording = False  # for toggle mode
        self._last_toggle_time = 0.0  # monotonic timestamp of last toggle action
        self._last_translate_tap = 0.0  # double-tap detection on translate key
        self._global_monitor = None
        self._local_monitor = None
        self._poll_timer: threading.Timer | None = None
        self._esc_stop = threading.Event()

    def start(self):
        """Install global + local event monitors. Must be called on main thread."""
        # Global: events when OTHER apps are active
        self._global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            _FLAGS_CHANGED_MASK,
            self._handle,
        )
        # Local: events when THIS app is active (history window, Dock click)
        self._local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            _FLAGS_CHANGED_MASK,
            self._handle_local,
        )
        if self._global_monitor and self._local_monitor:
            log.info("Hotkey monitors installed (global + local, keycode %d)",
                     self._keycode)
        elif self._global_monitor:
            log.warning("Only global hotkey monitor installed (local failed)")
        else:
            log.error(
                "Failed to install hotkey monitor. "
                "Grant Accessibility permission: System Settings → "
                "Privacy & Security → Accessibility → add AudioLog.app"
            )

    def _handle_local(self, event):
        """Local monitor handler — must return the event to pass it through."""
        self._handle(event)
        return event

    def _start_esc_polling(self):
        """Start background thread that polls ESC key state during recording."""
        if not self._on_cancel:
            return
        self._esc_stop.clear()
        t = threading.Thread(target=self._poll_esc, daemon=True)
        t.start()
        log.info("ESC polling started")

    def _stop_esc_polling(self):
        """Signal the ESC polling thread to stop."""
        self._esc_stop.set()

    def _poll_esc(self):
        """Background thread: polls ESC key state every 100ms."""
        while not self._esc_stop.wait(timeout=_ESC_POLL_INTERVAL):
            if not self._pressed and not self._recording:
                return
            esc_down = CGEventSourceKeyState(
                kCGEventSourceStateHIDSystemState, _ESC_KEYCODE)
            if esc_down:
                log.info("ESC detected via polling — cancelling recording")
                self._pressed = False
                self._recording = False
                self._cancel_polling()
                self._esc_stop.set()
                try:
                    AppHelper.callAfter(self._on_cancel)
                except Exception:
                    log.exception("Error in on_cancel")
                return

    def _handle(self, event):
        kc = event.keyCode()
        flags = event.modifierFlags()

        if kc == self._keycode:
            is_pressed = bool(flags & flag_for_keycode(self._keycode))
            if getattr(self._config, "hotkey_mode", "hold") == "toggle":
                self._handle_toggle(is_pressed)
            else:
                self._handle_hold(is_pressed)
        elif (self._translate_keycode is not None
              and kc == self._translate_keycode
              and self._on_translate_toggle is not None):
            is_pressed = bool(flags & flag_for_keycode(self._translate_keycode))
            self._handle_translate(is_pressed)

    def _handle_translate(self, is_pressed):
        """Translate key: always double-tap.

        The translate key is a plain modifier the user also types with
        (⌘C, ⌘V and friends), so a single press must never fire — only two
        presses inside the double-tap window count.
        """
        if not is_pressed:
            return

        now = time.monotonic()
        if now - self._last_translate_tap < _DOUBLE_TAP_WINDOW:
            log.info("Translate hotkey: double-tap detected")
            self._last_translate_tap = 0.0
            try:
                AppHelper.callAfter(self._on_translate_toggle)
            except Exception:
                log.exception("Error in on_translate_toggle")
        else:
            self._last_translate_tap = now

    def _handle_hold(self, is_pressed):
        """Hold mode: hold key to record, release to stop."""
        if is_pressed and not self._pressed:
            self._pressed = True
            self._start_polling()
            self._start_esc_polling()
            try:
                self._on_activate()
            except Exception:
                log.exception("Error in on_activate")

        elif not is_pressed and self._pressed:
            self._pressed = False
            self._cancel_polling()
            self._stop_esc_polling()
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in on_deactivate")

    def _handle_toggle(self, is_pressed):
        """Toggle mode: press once to start, press again to stop.

        Uses time-based debounce instead of _pressed flag to avoid getting
        stuck when macOS drops a key-up event.
        """
        if not is_pressed:
            return

        now = time.monotonic()
        if now - self._last_toggle_time < _TOGGLE_DEBOUNCE:
            return
        self._last_toggle_time = now

        if not self._recording:
            self._recording = True
            log.info("Toggle: recording ON")
            self._start_esc_polling()
            try:
                self._on_activate()
            except Exception:
                self._recording = False
                log.exception("Error in on_activate (toggle)")
        else:
            self._recording = False
            log.info("Toggle: recording OFF")
            self._stop_esc_polling()
            try:
                self._on_deactivate()
            except Exception:
                log.exception("Error in on_deactivate (toggle)")

    def reset_toggle(self):
        """Reset toggle state. Called externally when recording is
        force-stopped (e.g. by safety timer)."""
        self._recording = False
        self._pressed = False
        self._stop_esc_polling()

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
        """Check real modifier flags via Quartz. If the chosen modifier
        is no longer held but _pressed is True, we missed a key-up
        event."""
        if not self._pressed:
            return

        flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)
        held = bool(flags & flag_for_keycode(self._keycode))

        if not held:
            log.warning("Polling detected missed key-up — forcing deactivate")
            self._pressed = False
            self._recording = False
            self._stop_esc_polling()
            try:
                AppHelper.callAfter(self._on_deactivate)
            except Exception:
                log.exception("Error in forced on_deactivate via polling")
            return

        # Still pressed — schedule next poll
        self._poll_timer = threading.Timer(_POLL_INTERVAL, self._poll_key_state)
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def set_keycode(self, keycode: int) -> None:
        """Live-update the short-dictation hotkey. Resets in-flight
        press state so the next event is treated as a fresh press."""
        if keycode == self._keycode:
            return
        log.info("Short hotkey: keycode %d → %d", self._keycode, keycode)
        self._keycode = keycode
        self._pressed = False
        self._recording = False
        self._last_toggle_time = 0.0
        self._cancel_polling()
        self._stop_esc_polling()

    def set_translate_keycode(self, keycode: int) -> None:
        """Live-update the translate hotkey."""
        if keycode == self._translate_keycode:
            return
        log.info("Translate hotkey: keycode %s → %d",
                 self._translate_keycode, keycode)
        self._translate_keycode = keycode
        self._last_translate_tap = 0.0

    def stop(self):
        self._cancel_polling()
        self._stop_esc_polling()
        if self._global_monitor:
            NSEvent.removeMonitor_(self._global_monitor)
            self._global_monitor = None
        if self._local_monitor:
            NSEvent.removeMonitor_(self._local_monitor)
            self._local_monitor = None
        log.info("Hotkey monitors removed")
