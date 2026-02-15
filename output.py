"""Text output: auto-paste via PasteHelper.app launched through macOS 'open'."""

import os
import subprocess

from AppKit import NSPasteboard, NSPasteboardTypeString

from utils import get_logger

log = get_logger(__name__)

_PASTE_HELPER_APP = os.path.join(
    os.path.dirname(__file__),
    "PasteHelper.app",
)


def _copy_to_clipboard(text):
    """Copy text to system clipboard via NSPasteboard."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return pb.setString_forType_(text, NSPasteboardTypeString)


def paste_text(text):
    """Copy text to clipboard and auto-paste at cursor."""
    text = text.strip()
    if not text:
        log.warning("Empty transcription, skipping")
        return

    # 1. Always copy to clipboard as safety net
    _copy_to_clipboard(text)

    # 2. Kill any lingering PasteHelper from previous run
    subprocess.run(["killall", "PasteHelper"], capture_output=True)

    # 3. Launch PasteHelper via 'open' (own TCC identity) with text as argument
    #    -n = new instance, -W = wait for exit, -g = don't steal focus
    try:
        result = subprocess.run(
            ["open", "-n", "-W", "-g", "-a", _PASTE_HELPER_APP, "--args", text],
            timeout=5, capture_output=True,
        )
        stderr_out = result.stderr.decode().strip()
        if stderr_out:
            for line in stderr_out.splitlines():
                log.info("  %s", line)
        if result.returncode == 0:
            log.info("Pasted %d chars: %.60s…", len(text), text)
        else:
            log.warning("PasteHelper failed (rc=%d): %s — text is in clipboard",
                        result.returncode, stderr_out)
    except subprocess.TimeoutExpired:
        subprocess.run(["killall", "PasteHelper"], capture_output=True)
        log.warning("PasteHelper timed out — text is in clipboard")
    except Exception:
        log.warning("Auto-paste failed, text is in clipboard", exc_info=True)
