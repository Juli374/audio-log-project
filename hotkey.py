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

# NSEventMaskKeyDown — used for the translate shortcut, which is a real
# key combination rather than a bare modifier.
_KEY_DOWN_MASK = 1 << 10

# Only the device-independent modifier bits matter when matching a
# shortcut; caps lock, fn and the left/right distinction must be ignored
# or the combination silently stops matching.
_MODIFIER_MASK = 0xFFFF0000

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

# ── Translate shortcut ──
# A modifier-only trigger is not usable here: double-tapping Control or
# Command collides with other apps' global shortcuts (Claude opens on a
# double Control) and with ordinary typing. So the translate action uses a
# genuine combination. Only combinations that macOS and the common apps
# leave alone are offered — ⌘D is "bookmark" in browsers, ⌃⌘D is Look Up.
_KEYCODE_T = 17
_KEYCODE_D = 2

TRANSLATE_COMBOS: dict[str, tuple[int, int, str]] = {
    "ctrl+opt+t":   (_KEYCODE_T, _FLAG_CONTROL | _FLAG_OPTION,  "⌃⌥T"),
    "cmd+opt+t":    (_KEYCODE_T, _FLAG_COMMAND | _FLAG_OPTION,  "⌘⌥T"),
    "ctrl+shift+t": (_KEYCODE_T, _FLAG_CONTROL | _FLAG_SHIFT,   "⌃⇧T"),
    "cmd+ctrl+t":   (_KEYCODE_T, _FLAG_COMMAND | _FLAG_CONTROL, "⌘⌃T"),
    "ctrl+opt+d":   (_KEYCODE_D, _FLAG_CONTROL | _FLAG_OPTION,  "⌃⌥D"),
    "cmd+opt+d":    (_KEYCODE_D, _FLAG_COMMAND | _FLAG_OPTION,  "⌘⌥D"),
}

DEFAULT_TRANSLATE_COMBO = "ctrl+opt+t"


def translate_combo_label(name: str) -> str:
    """Human-readable form of a combo id, e.g. 'ctrl+opt+t' → '⌃⌥T'."""
    entry = TRANSLATE_COMBOS.get(name)
    return entry[2] if entry else name


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
        self._translate_combo = getattr(
            config, "translate_hotkey_combo", DEFAULT_TRANSLATE_COMBO)
        self._pressed = False
        self._recording = False  # for toggle mode
        self._last_toggle_time = 0.0  # monotonic timestamp of last toggle action
        self._global_monitor = None
        self._local_monitor = None
        self._key_global_monitor = None
        self._key_local_monitor = None
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
        # Separate pair for the translate combination: key-down events are a
        # different mask, and the local one must swallow the event so the
        # focused app does not also act on it.
        if self._on_translate_toggle is not None:
            self._key_global_monitor = (
                NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    _KEY_DOWN_MASK, self._handle_key_down))
            self._key_local_monitor = (
                NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    _KEY_DOWN_MASK, self._handle_key_down_local))
            log.info("Translate shortcut: %s",
                     translate_combo_label(self._translate_combo))

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

    def _matches_translate(self, event) -> bool:
        """True if this key-down is exactly the translate combination."""
        entry = TRANSLATE_COMBOS.get(self._translate_combo)
        if entry is None or self._on_translate_toggle is None:
            return False
        keycode, required, _ = entry
        if event.keyCode() != keycode:
            return False
        # Exact match on the modifier set: ⌃⌥T must not fire on ⌃⌥⇧T,
        # which may well be another app's shortcut.
        return (event.modifierFlags() & _MODIFIER_MASK) == required

    def _fire_translate(self) -> None:
        log.info("Translate shortcut fired: %s",
                 translate_combo_label(self._translate_combo))
        try:
            AppHelper.callAfter(self._on_translate_toggle)
        except Exception:
            log.exception("Error in on_translate_toggle")

    def _handle_key_down(self, event):
        """Global monitor: key-downs while another app is focused."""
        if self._matches_translate(event):
            self._fire_translate()

    def _handle_key_down_local(self, event):
        """Local monitor: swallow the event so our own UI doesn't see it."""
        if self._matches_translate(event):
            self._fire_translate()
            return None
        return event

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

    def set_translate_combo(self, combo: str) -> None:
        """Live-update the translate shortcut. Unknown ids are ignored."""
        if combo == self._translate_combo or combo not in TRANSLATE_COMBOS:
            return
        log.info("Translate shortcut: %s → %s",
                 translate_combo_label(self._translate_combo),
                 translate_combo_label(combo))
        self._translate_combo = combo

    def stop(self):
        self._cancel_polling()
        self._stop_esc_polling()
        for attr in ("_global_monitor", "_local_monitor",
                     "_key_global_monitor", "_key_local_monitor"):
            monitor = getattr(self, attr, None)
            if monitor is not None:
                NSEvent.removeMonitor_(monitor)
                setattr(self, attr, None)
        log.info("Hotkey monitors removed")
