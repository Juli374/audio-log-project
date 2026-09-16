"""Text output: clipboard + auto-paste (Cmd+V) via Quartz CGEvent.

Runs inside AudioLog.app's own process, so it uses AudioLog's TCC
Accessibility grant directly. No helper binary, no subprocess — a single
Accessibility approval (for AudioLog.app) is all the user needs.

Pasting costs the clipboard, so the rule is: AudioLog only ever overwrites
what you could have typed. Anything the clipboard cannot hand over as text
— a screenshot, a file — is put back once the paste has landed. The
snapshot helpers here are shared with the translation overlay.
"""

import subprocess
import threading
import time

from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
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

# How long the target app gets to read the pasteboard before we put the
# previous contents back. Apps answer ⌘V on their main thread, usually in
# milliseconds; this is deliberately generous, and a restore that would
# overwrite something the user copied meanwhile is skipped anyway.
_RESTORE_DELAY = 0.6

# Every flavour that means "this is text". A clipboard offering any of them
# is one the transcription may replace, exactly as it always has.
_TEXT_TYPES = frozenset({
    "public.utf8-plain-text",
    "public.utf16-external-plain-text",
    "public.plain-text",
    "public.text",
    "public.rtf",
    "public.html",
    "NSStringPboardType",
})

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


def snapshot_pasteboard(pb) -> list | None:
    """Everything on `pb`, as [{type: NSData}]. None when there is nothing.

    Read eagerly: a promised ("lazy") flavour hands back nil once its owner
    has gone, so the bytes have to be taken now or not at all.
    """
    items = []
    for item in (pb.pasteboardItems() or []):
        payload = {}
        for kind in (item.types() or []):
            data = item.dataForType_(kind)
            if data is not None:
                payload[str(kind)] = data
        if payload:
            items.append(payload)
    return items or None


def restore_pasteboard(pb, items) -> None:
    """Put a snapshot_pasteboard() result back, replacing what is there."""
    pb.clearContents()
    restored = []
    for payload in items:
        item = NSPasteboardItem.alloc().init()
        for kind, data in payload.items():
            item.setData_forType_(data, kind)
        restored.append(item)
    if restored:
        pb.writeObjects_(restored)


def offers_text(items) -> bool:
    """True when the snapshot can be pasted as text somewhere."""
    return any(kind in _TEXT_TYPES
               for payload in items for kind in payload)


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


def _restore_after_paste(items, stamped: int) -> None:
    """Hand the clipboard back once the target app has read the text."""
    time.sleep(_RESTORE_DELAY)
    pb = NSPasteboard.generalPasteboard()
    if pb.changeCount() != stamped:
        return   # the user copied something else meanwhile — it wins
    try:
        restore_pasteboard(pb, items)
    except Exception:
        log.exception("could not restore the clipboard")
        return
    log.info("Clipboard restored after paste")


def paste_text(text: str) -> None:
    """Copy text to clipboard and auto-paste at cursor."""
    text = text.strip()
    if not text:
        log.warning("Empty transcription, skipping")
        return

    pb = NSPasteboard.generalPasteboard()
    # A screenshot, a file — anything the clipboard cannot offer as text is
    # borrowed, not taken: dictating must not throw away the picture you
    # just captured. Text is replaced as it always was.
    keep = snapshot_pasteboard(pb)
    if keep is not None and offers_text(keep):
        keep = None

    # 1. Always copy to clipboard as a safety net (user can Cmd+V manually
    #    if the auto-paste is blocked by some app's event tap).
    _copy_to_clipboard(text)
    stamped = pb.changeCount()

    # 2. Small delay lets the frontmost app finish any in-flight keystroke
    #    handling (e.g. the hotkey release) before we inject ⌘V.
    time.sleep(0.05)

    trusted = is_trusted()
    if not trusted:
        # CGEventPost fails silently without Accessibility — the keystroke is
        # dropped and the user just sees nothing happen. Ask for it instead.
        # The text stays put: with no paste it is the only copy there is.
        log.warning("Auto-paste blocked: Accessibility not granted. "
                    "Text is in the clipboard (⌘V).")
        request_accessibility(prompt=True)
        return

    try:
        _post_cmd_v()
        log.info("Pasted %d chars: %.60s…", len(text), text)
    except Exception:
        log.warning("Auto-paste failed, text is in clipboard", exc_info=True)
        return

    if keep is not None:
        threading.Thread(target=_restore_after_paste, args=(keep, stamped),
                         daemon=True, name="clipboard-restore").start()
