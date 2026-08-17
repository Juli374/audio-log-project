"""Hotkey listener using native macOS NSEvent global monitor."""

import threading
import time

from AppKit import NSEvent
from PyObjCTools import AppHelper
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetMain,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventSourceFlagsState,
    CGEventSourceKeyState,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCGEventKeyDown,
    kCGEventSourceStateHIDSystemState,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGKeyboardEventKeycode,
    kCGSessionEventTap,
)
from utils import get_logger

log = get_logger(__name__)

# NSEventMaskFlagsChanged
_FLAGS_CHANGED_MASK = 1 << 12

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
# Stored as a key + modifier mask, so the user can record any combination
# they like instead of picking from a fixed list.
#
# It is caught with a CGEventTap rather than an NSEvent monitor. A monitor
# only observes: the keystroke still reaches the focused app, so ⌥D would
# also type "∂" into the document and ⌃⌥T would still open Google Docs'
# accessibility dialog. A tap consumes the event, which is what a global
# shortcut has to do.
_KEYCODE_D = 2

DEFAULT_TRANSLATE_KEY = _KEYCODE_D
DEFAULT_TRANSLATE_MODS = _FLAG_OPTION          # ⌥D

# Tap re-enable reasons (Quartz doesn't export these as constants).
_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
_TAP_DISABLED_BY_USER = 0xFFFFFFFF

# keycode → printable name, for showing the shortcut back to the user.
_KEYCODE_NAMES: dict[int, str] = {
    0: "A", 11: "B", 8: "C", 2: "D", 14: "E", 3: "F", 5: "G", 4: "H",
    34: "I", 38: "J", 40: "K", 37: "L", 46: "M", 45: "N", 31: "O", 35: "P",
    12: "Q", 15: "R", 1: "S", 17: "T", 32: "U", 9: "V", 13: "W", 7: "X",
    16: "Y", 6: "Z",
    29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7",
    28: "8", 25: "9",
    49: "Space", 36: "Return", 48: "Tab", 47: ".", 43: ",", 44: "/",
    27: "-", 24: "=", 33: "[", 30: "]", 39: "'", 41: ";", 42: "\\", 50: "`",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
    98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
}


def shortcut_label(keycode: int, mods: int) -> str:
    """Render a shortcut the way macOS shows it, e.g. (2, option) → '⌥D'."""
    out = ""
    if mods & _FLAG_CONTROL:
        out += "⌃"
    if mods & _FLAG_OPTION:
        out += "⌥"
    if mods & _FLAG_SHIFT:
        out += "⇧"
    if mods & _FLAG_COMMAND:
        out += "⌘"
    return out + _KEYCODE_NAMES.get(keycode, f"#{keycode}")


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
        self._translate_key = getattr(
            config, "translate_hotkey_key", DEFAULT_TRANSLATE_KEY)
        self._translate_mods = getattr(
            config, "translate_hotkey_mods", DEFAULT_TRANSLATE_MODS)
        self._pressed = False
        self._recording = False  # for toggle mode
        self._last_toggle_time = 0.0  # monotonic timestamp of last toggle action
        self._global_monitor = None
        self._local_monitor = None
        self._tap = None
        self._tap_source = None
        self._tap_retry_timer: threading.Timer | None = None
        self._tap_retries = 0
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
        if self._on_translate_toggle is not None:
            self._install_translate_tap()

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

    # ── Translate shortcut (CGEventTap) ──

    def _install_translate_tap(self) -> None:
        """Install a session-wide tap that consumes the translate shortcut.

        Creating the tap needs Accessibility. When it is missing the call
        simply returns NULL, so we keep retrying in the background: people
        grant the permission after the app is already running, and without
        the retry the shortcut stays dead until a restart nobody knows to do.
        """
        mask = 1 << kCGEventKeyDown
        self._tap = CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap,
            kCGEventTapOptionDefault, mask, self._tap_callback, None)

        if not self._tap:
            log.error(
                "Translate shortcut unavailable: could not create event tap. "
                "Grant Accessibility permission to AudioLog.")
            self._schedule_tap_retry()
            return

        self._tap_source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), self._tap_source,
                           kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)
        self._tap_retries = 0
        log.info("Translate shortcut: %s",
                 shortcut_label(self._translate_key, self._translate_mods))

    def _schedule_tap_retry(self) -> None:
        """Try again shortly — the user may be granting the permission now."""
        if self._tap_retry_timer is not None:
            return
        self._tap_retries += 1
        # Back off from every 5s to every couple of minutes; a permission
        # granted at any point still gets picked up, without spinning.
        delay = min(5.0 * self._tap_retries, 120.0)

        def _retry():
            self._tap_retry_timer = None
            if self._tap is not None:
                return
            AppHelper.callAfter(self._install_translate_tap)

        self._tap_retry_timer = threading.Timer(delay, _retry)
        self._tap_retry_timer.daemon = True
        self._tap_retry_timer.start()

    @property
    def translate_ready(self) -> bool:
        """True when the translate shortcut is actually armed."""
        return self._tap is not None

    def _tap_callback(self, proxy, event_type, event, refcon):
        """Runs for every key-down in the session. Must stay fast.

        Returning None swallows the event, which is how the shortcut is
        kept from reaching the focused app.
        """
        # macOS disables a tap that takes too long; re-arm and move on.
        if event_type in (_TAP_DISABLED_BY_TIMEOUT, _TAP_DISABLED_BY_USER):
            log.warning("Event tap disabled by system — re-enabling")
            if self._tap is not None:
                CGEventTapEnable(self._tap, True)
            return event

        try:
            keycode = CGEventGetIntegerValueField(
                event, kCGKeyboardEventKeycode)
            flags = CGEventGetFlags(event) & _MODIFIER_MASK
            # Exact modifier match: ⌥D must not fire on ⇧⌥D, which is a
            # different shortcut somewhere else.
            if keycode == self._translate_key and flags == self._translate_mods:
                self._fire_translate()
                return None
        except Exception:
            log.exception("Error in event tap")

        return event

    def _fire_translate(self) -> None:
        log.info("Translate shortcut fired: %s",
                 shortcut_label(self._translate_key, self._translate_mods))
        try:
            AppHelper.callAfter(self._on_translate_toggle)
        except Exception:
            log.exception("Error in on_translate_toggle")

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

    def set_translate_shortcut(self, keycode: int, mods: int) -> None:
        """Live-update the translate shortcut. The tap stays installed."""
        if keycode == self._translate_key and mods == self._translate_mods:
            return
        log.info("Translate shortcut: %s → %s",
                 shortcut_label(self._translate_key, self._translate_mods),
                 shortcut_label(keycode, mods))
        self._translate_key = keycode
        self._translate_mods = mods

    def stop(self):
        self._cancel_polling()
        self._stop_esc_polling()
        if self._tap_retry_timer is not None:
            self._tap_retry_timer.cancel()
            self._tap_retry_timer = None
        for attr in ("_global_monitor", "_local_monitor"):
            monitor = getattr(self, attr, None)
            if monitor is not None:
                NSEvent.removeMonitor_(monitor)
                setattr(self, attr, None)
        if self._tap is not None:
            CGEventTapEnable(self._tap, False)
            self._tap = None
            self._tap_source = None
        log.info("Hotkey monitors removed")
