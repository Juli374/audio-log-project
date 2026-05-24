"""Text output: clipboard + auto-paste (Cmd+V) via Quartz CGEvent.

Runs inside AudioLog.app's own process, so it uses AudioLog's TCC
Accessibility grant directly. No helper binary, no subprocess — a single
Accessibility approval (for AudioLog.app) is all the user needs.
"""

import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from ApplicationServices import AXIsProcessTrusted
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

    trusted = bool(AXIsProcessTrusted())
    try:
        _post_cmd_v()
        log.info("Pasted %d chars (AX trusted=%s): %.60s…",
                 len(text), trusted, text)
    except Exception:
        log.warning("Auto-paste failed (AX trusted=%s), text is in clipboard",
                    trusted, exc_info=True)
