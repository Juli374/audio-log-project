"""Screenshot with markup.

Press the shortcut (⌘⇧4 by default), drag out an area with the standard
macOS crosshair, and the shot opens in a small editor: arrow, rectangle,
line, check mark, a handful of colours, ⌘Z. Enter (or «Копировать») puts
the result on the clipboard, closes the window and hands focus back to the
app you were in — paste it anywhere.

The capture itself is the system's own `screencapture -i`, so the selection
UI is the one people already know, and Escape there simply cancels. The raw
shot lands on the clipboard the moment it is taken: the editor is optional,
and closing it without drawing changes nothing.

The editor is an NSWindow around a WKWebView (ui/screenshot.html), the same
arrangement as the history window. The image travels in as a data URL and
the annotated result comes back the same way; the DPI metadata screencapture
stamps on Retina shots is put back on the result so it pastes at the right
size in apps that honour it.
"""

import base64
import os
import struct
import subprocess
import tempfile
import threading

import objc
import AppKit
import Foundation
import WebKit
from PyObjCTools import AppHelper

from history_ui import MessageHandler
from utils import get_logger, resource_path

log = get_logger(__name__)

_SCREENCAPTURE = "/usr/sbin/screencapture"

# The crosshair may sit on screen for as long as the user likes; this is a
# backstop so a wedged helper cannot block the next capture forever.
_CAPTURE_TIMEOUT = 600

# Editor chrome around the image, in points: toolbar, footer, margins.
_CHROME_HEIGHT = 100
_CHROME_WIDTH = 32
_MIN_WIDTH = 640
_MIN_HEIGHT = 380
_SCREEN_SHARE = 0.9   # the window never exceeds this fraction of the screen

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# NSApplicationActivateIgnoringOtherApps
_ACTIVATE_IGNORING_OTHERS = 1 << 1


# ── PNG helpers ──


def png_size(png: bytes) -> tuple[int, int]:
    """Pixel width and height from the IHDR chunk; (0, 0) if not a PNG."""
    if len(png) < 24 or png[:8] != _PNG_SIGNATURE or png[12:16] != b"IHDR":
        return (0, 0)
    w, h = struct.unpack(">II", png[16:24])
    return (int(w), int(h))


def png_scale(png: bytes) -> float:
    """Backing scale the PNG was taken at (2.0 on Retina), from its pHYs chunk.

    screencapture stamps the display's DPI into the file. Without it a
    Retina shot pastes at twice its on-screen size in apps that honour DPI.
    """
    if png[:8] != _PNG_SIGNATURE:
        return 1.0
    pos = 8
    while pos + 12 <= len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        kind = png[pos + 4:pos + 8]
        if kind == b"pHYs" and length >= 9:
            per_metre, _y, unit = struct.unpack(">IIB", png[pos + 8:pos + 17])
            if unit == 1 and per_metre:
                return max(1.0, float(round(per_metre * 0.0254 / 72.0)))
            return 1.0
        if kind == b"IDAT":
            break
        pos += 12 + length
    return 1.0


def stamp_scale(png: bytes, scale: float) -> bytes:
    """Re-encode `png` so its DPI metadata matches `scale` (1.0 = 72 dpi).

    The canvas in the editor produces a bare PNG; this gives the annotated
    result the same physical size the original shot had.
    """
    if scale <= 1.0:
        return png
    data = Foundation.NSData.dataWithBytes_length_(png, len(png))
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(data)
    if rep is None:
        return png
    rep.setSize_(Foundation.NSMakeSize(rep.pixelsWide() / scale,
                                       rep.pixelsHigh() / scale))
    out = rep.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, {})
    return bytes(out) if out is not None else png


def copy_png_to_clipboard(png: bytes) -> bool:
    """Put a PNG on the general pasteboard as PNG and TIFF. Main thread."""
    data = Foundation.NSData.dataWithBytes_length_(png, len(png))
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    ok = bool(pb.setData_forType_(data, AppKit.NSPasteboardTypePNG))
    # TIFF is the lowest common denominator — some apps only look for it.
    image = AppKit.NSImage.alloc().initWithData_(data)
    if image is not None:
        tiff = image.TIFFRepresentation()
        if tiff is not None:
            pb.setData_forType_(tiff, AppKit.NSPasteboardTypeTIFF)
    return ok


# ── capture ──


def screen_capture_granted() -> bool:
    """True when macOS lets us see other apps' windows in a capture.

    Without the Screen Recording permission `screencapture` still returns
    a file — of the wallpaper alone, every window blanked out.
    """
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True   # no API to ask: better to try than to nag


def request_screen_capture() -> bool:
    """Show the system prompt (once) and add AudioLog to the permission list."""
    try:
        from Quartz import CGRequestScreenCaptureAccess
        return bool(CGRequestScreenCaptureAccess())
    except Exception:
        return False


def capture_interactive() -> bytes | None:
    """Run the system crosshair; return the PNG, or None when cancelled.

    Escape in the crosshair leaves no file behind, which is how a cancel
    is told apart from a capture.
    """
    fd, path = tempfile.mkstemp(prefix="audiolog-shot-", suffix=".png")
    os.close(fd)
    os.unlink(path)
    try:
        subprocess.run([_SCREENCAPTURE, "-i", "-t", "png", path],
                       capture_output=True, timeout=_CAPTURE_TIMEOUT,
                       check=False)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ── window ──


class _WindowDelegate(AppKit.NSObject):
    """Lets the editor notice the red close button as well as its own Esc."""

    def initWithCallback_(self, callback):
        self = objc.super(_WindowDelegate, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def windowWillClose_(self, notification):
        self._callback()


class ScreenshotEditor:
    """Shortcut → crosshair → editor window → clipboard."""

    def __init__(self, config, on_copied=None) -> None:
        self._config = config
        self._on_copied = on_copied
        self._window = None
        self._webview = None
        self._handler = None
        self._delegate = None
        self._built = False
        self._page_ready = False
        self._pending_js: str | None = None
        self._capturing = False
        self._scale = 1.0
        self._return_to = None   # the app that was in front before us

    # -- capture --

    def capture(self) -> None:
        """Shortcut and menu item both land here. Any thread."""
        if self._capturing:
            return
        self._capturing = True
        threading.Thread(target=self._capture_worker, daemon=True,
                         name="screenshot").start()

    def _capture_worker(self) -> None:
        try:
            png = capture_interactive()
        except Exception:
            log.exception("screencapture failed")
            png = None
        finally:
            self._capturing = False
        if png is None:
            log.info("Screenshot: cancelled")
            return
        width, height = png_size(png)
        scale = png_scale(png)
        log.info("Screenshot: %dx%d px @%.0fx, %d KB",
                 width, height, scale, len(png) // 1024)
        AppHelper.callAfter(self._present, png, width, height, scale)

    # -- window --

    def _build(self) -> None:
        if self._built:
            return
        rect = AppKit.NSMakeRect(0, 0, 900, 640)
        style = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        self._window.setTitle_("AudioLog — Скриншот")
        self._window.setMinSize_(AppKit.NSMakeSize(_MIN_WIDTH, _MIN_HEIGHT))
        self._window.setReleasedWhenClosed_(False)
        self._delegate = _WindowDelegate.alloc().initWithCallback_(self._on_closed)
        self._window.setDelegate_(self._delegate)

        wk_config = WebKit.WKWebViewConfiguration.alloc().init()
        self._handler = MessageHandler.alloc().initWithCallback_(self._on_message)
        wk_config.userContentController().addScriptMessageHandler_name_(
            self._handler, "bridge")
        wk_config.preferences().setValue_forKey_(True, "allowFileAccessFromFileURLs")

        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            rect, wk_config)
        self._webview.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self._window.contentView().addSubview_(self._webview)

        html_path = resource_path() / "ui" / "screenshot.html"
        url = Foundation.NSURL.fileURLWithPath_(str(html_path))
        base_url = Foundation.NSURL.fileURLWithPath_(str(html_path.parent))
        self._webview.loadFileURL_allowingReadAccessToURL_(url, base_url)

        self._built = True
        log.info("Screenshot editor built")

    def _present(self, png: bytes, width: int, height: int,
                 scale: float) -> None:
        """Main thread: copy the raw shot, size the window to it, show it."""
        try:
            copy_png_to_clipboard(png)   # screencapture already stamped DPI
        except Exception:
            log.exception("could not copy the raw screenshot")

        self._scale = scale
        self._build()
        self._fit_window(width / scale, height / scale)

        b64 = base64.b64encode(png).decode("ascii")
        js = f"editor.load('data:image/png;base64,{b64}', {scale})"
        if self._page_ready:
            self._eval_js(js)
        else:
            self._pending_js = js

        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() != os.getpid():
            self._return_to = front
        self._window.makeKeyAndOrderFront_(None)
        self._window.makeFirstResponder_(self._webview)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def _fit_window(self, image_w: float, image_h: float) -> None:
        """Size the window to the image (in points), capped to the screen."""
        screen = self._window.screen() or AppKit.NSScreen.mainScreen()
        vf = screen.visibleFrame()
        max_w = vf.size.width * _SCREEN_SHARE
        max_h = vf.size.height * _SCREEN_SHARE
        w = min(max(image_w + _CHROME_WIDTH, _MIN_WIDTH), max_w)
        h = min(max(image_h + _CHROME_HEIGHT, _MIN_HEIGHT), max_h)
        frame = self._window.frameRectForContentRect_(
            AppKit.NSMakeRect(0, 0, w, h))
        x = vf.origin.x + (vf.size.width - frame.size.width) / 2.0
        y = vf.origin.y + (vf.size.height - frame.size.height) / 2.0
        self._window.setFrame_display_(
            AppKit.NSMakeRect(x, y, frame.size.width, frame.size.height), True)

    def _eval_js(self, js: str) -> None:
        if self._webview is not None:
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    def close(self) -> None:
        """Hide the editor (main thread). The window is reused next time."""
        if self._window is not None and self._window.isVisible():
            self._window.close()   # → windowWillClose → _on_closed

    def _on_closed(self) -> None:
        """Send focus back to the app the user was in before the capture."""
        target, self._return_to = self._return_to, None
        if target is None:
            return
        try:
            target.activateWithOptions_(_ACTIVATE_IGNORING_OTHERS)
        except Exception:
            log.exception("could not return focus to %s", target)

    # -- messages from the page --

    def _on_message(self, data: dict) -> None:
        """Called on the main thread by the WKWebView bridge."""
        action = data.get("action", "")
        if action == "ready":
            self._page_ready = True
            if self._pending_js:
                js, self._pending_js = self._pending_js, None
                self._eval_js(js)
        elif action == "copy":
            self._copy_result(str(data.get("image", "")))
        elif action == "close":
            self.close()

    def _copy_result(self, data_url: str) -> None:
        head, _, b64 = data_url.partition(",")
        if not b64 or "image/png" not in head:
            log.error("Screenshot editor sent something that is not a PNG")
            return
        try:
            png = stamp_scale(base64.b64decode(b64), self._scale)
            ok = copy_png_to_clipboard(png)
        except Exception:
            log.exception("could not copy the annotated screenshot")
            ok = False
        self.close()
        if not ok:
            return
        log.info("Screenshot with markup copied (%d KB)", len(png) // 1024)
        if self._on_copied is not None:
            try:
                self._on_copied()
            except Exception:
                log.exception("on_copied callback failed")
