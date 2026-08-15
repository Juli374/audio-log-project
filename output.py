"""Text output: clipboard + auto-paste (Cmd+V) via Quartz CGEvent.

Runs inside AudioLog.app's own process, so it uses AudioLog's TCC
Accessibility grant directly. No helper binary, no subprocess — a single
Accessibility approval (for AudioLog.app) is all the user needs.
"""

import subprocess
import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    kAXTrustedCheckOptionPrompt,
)
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

from utils import get_logger

log = get_logger(__name__)

# macOS virtual keycode for "V"
_KEYCODE_V = 9

_SETTINGS_URL = ("x-apple.systempreferences:com.apple.preference.security"
                 "?Privacy_Accessibility")


def is_trusted() -> bool:
    """True if macOS lets us synthesize keystrokes (Accessibility granted)."""
    return bool(AXIsProcessTrusted())


def request_accessibility(prompt: bool = True) -> bool:
    """Ask macOS for Accessibility, showing the system prompt.

    The prompt adds AudioLog to System Settings → Privacy & Security →
    Accessibility and offers a button to open it. Without this call the app
    is simply absent from that list and the user has nothing to switch on.

    TCC binds the grant to the code signature, so with our Developer ID this
    is a one-time step that survives every future update.
    """
    options = {kAXTrustedCheckOptionPrompt: bool(prompt)}
    trusted = bool(AXIsProcessTrustedWithOptions(options))
    log.info("Accessibility check: trusted=%s (prompt=%s)", trusted, prompt)
    return trusted


def open_accessibility_settings() -> None:
    """Open the Accessibility pane so the user can flip the switch."""
    try:
        subprocess.Popen(["/usr/bin/open", _SETTINGS_URL])
    except Exception:
        log.exception("could not open Accessibility settings")


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard via NSPasteboard."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(text, NSPasteboardTypeString))


def _post_cmd_v() -> None:
    """Synthesize ⌘V at the HID event tap. Requires Accessibility."""
    # Using a None source (default) — macOS attributes events to our process,
    # which is what TCC needs for Accessibility grant checks.
    down = CGEventCreateKeyboardEvent(None, _KEYCODE_V, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.02)
    up = CGEventCreateKeyboardEvent(None, _KEYCODE_V, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, up)


def paste_text(text: str) -> None:
    """Copy text to clipboard and auto-paste at cursor."""
    text = text.strip()
    if not text:
        log.warning("Empty transcription, skipping")
        return

    # 1. Always copy to clipboard as a safety net (user can Cmd+V manually
    #    if the auto-paste is blocked by some app's event tap).
    _copy_to_clipboard(text)

    # 2. Small delay lets the frontmost app finish any in-flight keystroke
    #    handling (e.g. the hotkey release) before we inject ⌘V.
    time.sleep(0.05)

    trusted = is_trusted()
    if not trusted:
        # CGEventPost fails silently without Accessibility — the keystroke is
        # dropped and the user just sees nothing happen. Ask for it instead.
        log.warning("Auto-paste blocked: Accessibility not granted. "
                    "Text is in the clipboard (⌘V).")
        request_accessibility(prompt=True)
        return

    try:
        _post_cmd_v()
        log.info("Pasted %d chars: %.60s…", len(text), text)
    except Exception:
        log.warning("Auto-paste failed, text is in clipboard", exc_info=True)
