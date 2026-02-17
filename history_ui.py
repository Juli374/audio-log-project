"""History window using NSWindow + WKWebView."""

import json
from pathlib import Path

import objc
import AppKit
import Foundation
import WebKit
from PyObjCTools import AppHelper

import db
from config import Config
from utils import get_logger

log = get_logger(__name__)


class MessageHandler(AppKit.NSObject):
    """WKScriptMessageHandler — receives messages from JS."""

    def initWithCallback_(self, callback):
        self = objc.super(MessageHandler, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        # Convert NSDictionary/NSString to Python dict
        if hasattr(body, 'allKeys'):
            data = {str(k): body[k] for k in body.allKeys()}
            # Convert nested NSDictionary
            for k, v in data.items():
                if hasattr(v, 'allKeys'):
                    data[k] = {str(kk): v[kk] for kk in v.allKeys()}
        else:
            data = {"action": str(body)}
        self._callback(data)


class HistoryWindow:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._window = None
        self._webview = None
        self._built = False

    def build(self) -> None:
        """Create the window and webview. Must be called on main thread."""
        if self._built:
            return

        # Window
        rect = AppKit.NSMakeRect(0, 0, 600, 700)
        style = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        self._window.setTitle_("AudioLog — История")
        self._window.setMinSize_(AppKit.NSMakeSize(400, 400))
        self._window.center()
        self._window.setReleasedWhenClosed_(False)

        # WKWebView config
        wk_config = WebKit.WKWebViewConfiguration.alloc().init()
        handler = MessageHandler.alloc().initWithCallback_(self._on_message)
        # prevent deallocation
        self._handler = handler
        wk_config.userContentController().addScriptMessageHandler_name_(handler, "bridge")

        # Allow file:// access
        wk_config.preferences().setValue_forKey_(True, "allowFileAccessFromFileURLs")

        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            rect, wk_config
        )
        self._webview.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        self._window.contentView().addSubview_(self._webview)

        # Load HTML
        html_path = Path(__file__).parent / "ui" / "index.html"
        url = Foundation.NSURL.fileURLWithPath_(str(html_path))
        base_url = Foundation.NSURL.fileURLWithPath_(str(html_path.parent))
        self._webview.loadFileURL_allowingReadAccessToURL_(url, base_url)

        self._built = True
        log.info("History window built")

    def show(self) -> None:
        """Show the history window (main thread)."""
        def _do():
            if not self._built:
                return
            self._window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            # Refresh data
            self._eval_js("app.onNewEntry()")
        AppHelper.callAfter(_do)

    def hide(self) -> None:
        """Hide the history window."""
        def _do():
            if self._window:
                self._window.orderOut_(None)
        AppHelper.callAfter(_do)

    def notify_new_entry(self) -> None:
        """Notify the webview about a new transcription."""
        def _do():
            if self._webview and self._window and self._window.isVisible():
                self._eval_js("app.onNewEntry()")
        AppHelper.callAfter(_do)

    def _eval_js(self, js: str) -> None:
        """Run JS in the webview."""
        if self._webview:
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    def _send_response(self, data: dict) -> None:
        """Send data back to JS."""
        json_str = json.dumps(data, ensure_ascii=False)
        self._eval_js(f"app.onData({json_str})")

    def _on_message(self, data: dict) -> None:
        """Handle message from JS. Called on main thread."""
        action = data.get("action", "")

        if action == "get_history":
            limit = int(data.get("limit", 50))
            offset = int(data.get("offset", 0))
            rows = db.get_recent(limit, offset)
            self._send_response({
                "action": "history",
                "data": rows,
                "append": offset > 0,
            })

        elif action == "search":
            query = str(data.get("query", ""))
            limit = int(data.get("limit", 50))
            offset = int(data.get("offset", 0))
            rows = db.search(query, limit, offset)
            self._send_response({
                "action": "history",
                "data": rows,
                "append": offset > 0,
            })

        elif action == "get_stats":
            stats = db.get_stats()
            self._send_response({"action": "stats", "data": stats})

        elif action == "delete":
            row_id = int(data.get("id", 0))
            db.delete(row_id)
            self._send_response({"action": "deleted", "id": row_id})

        elif action == "copy_to_clipboard":
            text = str(data.get("text", ""))
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
            self._send_response({"action": "copied"})

        elif action == "get_settings":
            settings = self._config.load_settings()
            self._send_response({"action": "settings", "data": settings})

        elif action == "save_settings":
            settings = {
                "model": str(data.get("model", "small")),
                "language": str(data.get("language", "ru")),
                "n_threads": int(data.get("n_threads", 4)),
                "max_recording_seconds": int(data.get("max_recording_seconds", 300)),
                "hotkey_mode": str(data.get("hotkey_mode", "hold")),
            }
            self._config.save_settings(settings)
            self._send_response({"action": "settings_saved"})
