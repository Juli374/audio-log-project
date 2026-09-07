"""Hand a system screenshot shortcut over to AudioLog while it is ours.

⌘⇧4 belongs to macOS: the window server matches it before any app — or
any event tap — sees the keystroke, so merely listening for it would open
our editor *and* the system crosshair at once. The checkbox in System
Settings → Keyboard → Shortcuts → Screenshots is a per-shortcut flag that
lives in the window server, and SkyLight exposes that flag: flipping it is
exactly what the checkbox does, minus the persistence. The state belongs
to the login session — it comes back by itself after a logout or reboot —
and we put it back the moment the shortcut is released or the app quits.

AltTab takes over ⌘Tab the same way.
"""

import ctypes

from utils import get_logger

log = get_logger(__name__)

_SKYLIGHT = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"

# NSEvent device-independent modifier flags — the same values hotkey.py
# stores in settings.
_SHIFT = 1 << 17
_CONTROL = 1 << 18
_COMMAND = 1 << 20

# (virtual keycode, modifier mask) → id in com.apple.symbolichotkeys.
SYSTEM_SHORTCUTS: dict[tuple[int, int], int] = {
    (20, _SHIFT | _COMMAND): 28,             # ⌘⇧3   screen → file
    (20, _SHIFT | _COMMAND | _CONTROL): 29,  # ⌃⌘⇧3  screen → clipboard
    (21, _SHIFT | _COMMAND): 30,             # ⌘⇧4   area → file
    (21, _SHIFT | _COMMAND | _CONTROL): 31,  # ⌃⌘⇧4  area → clipboard
    (23, _SHIFT | _COMMAND): 184,            # ⌘⇧5   screenshot panel
}


def system_shortcut_id(keycode: int, mods: int) -> int | None:
    """The system screenshot shortcut this combination collides with, if any."""
    return SYSTEM_SHORTCUTS.get((int(keycode), int(mods)))


class SystemShortcuts:
    """Owns at most one claimed system shortcut and gives it back on release.

    A shortcut the user had already switched off in System Settings is
    left alone: nothing to take, nothing to restore.
    """

    def __init__(self) -> None:
        self._lib = None
        self._claimed: int | None = None

    def _load(self):
        if self._lib is None:
            lib = ctypes.cdll.LoadLibrary(_SKYLIGHT)
            lib.CGSIsSymbolicHotKeyEnabled.argtypes = [ctypes.c_int]
            lib.CGSIsSymbolicHotKeyEnabled.restype = ctypes.c_bool
            lib.CGSSetSymbolicHotKeyEnabled.argtypes = [ctypes.c_int, ctypes.c_bool]
            lib.CGSSetSymbolicHotKeyEnabled.restype = ctypes.c_int
            self._lib = lib
        return self._lib

    @property
    def claimed(self) -> int | None:
        return self._claimed

    def claim(self, keycode: int, mods: int) -> None:
        """Take `keycode+mods` away from macOS if it is a screenshot shortcut.

        Whatever was claimed before is released first, so changing the
        shortcut in settings never leaves a stray system key switched off.
        """
        wanted = system_shortcut_id(keycode, mods)
        if wanted == self._claimed:
            return
        self.release()
        if wanted is None:
            return
        try:
            lib = self._load()
            if not lib.CGSIsSymbolicHotKeyEnabled(wanted):
                log.info("System shortcut %d is already off in System "
                         "Settings — nothing to take over", wanted)
                return
            err = lib.CGSSetSymbolicHotKeyEnabled(wanted, False)
        except Exception:
            log.exception("could not switch off system shortcut %d", wanted)
            return
        if err != 0:
            log.warning("system shortcut %d: switching off returned %d",
                        wanted, err)
            return
        self._claimed = wanted
        log.info("System screenshot shortcut %d handed over to AudioLog",
                 wanted)

    def release(self) -> None:
        """Give the claimed shortcut back to macOS (no-op if none)."""
        if self._claimed is None:
            return
        hotkey, self._claimed = self._claimed, None
        try:
            err = self._load().CGSSetSymbolicHotKeyEnabled(hotkey, True)
        except Exception:
            log.exception("could not restore system shortcut %d", hotkey)
            return
        if err != 0:
            log.warning("system shortcut %d: restore returned %d", hotkey, err)
        else:
            log.info("System screenshot shortcut %d given back to macOS",
                     hotkey)
