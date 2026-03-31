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
        self._recorder = None
        self._transcriber = None
        self._voice_recording = False
        self._voice_target = None  # 'note' or 'task'

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

        elif action == "update_entry":
            row_id = int(data.get("id", 0))
            text = str(data.get("text", ""))
            db.update_entry(row_id, text)
            self._send_response({"action": "entry_updated", "id": row_id, "text": text})

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
                "transcription_mode": str(data.get("transcription_mode", "local")),
                "openai_api_key": str(data.get("openai_api_key", "")),
                "openai_model": str(data.get("openai_model", "gpt-4o-mini-transcribe")),
            }
            self._config.save_settings(settings)
            self._send_response({"action": "settings_saved"})

        elif action == "test_api":
            import threading
            api_key = str(data.get("api_key", ""))
            model = str(data.get("model", "gpt-4o-mini-transcribe"))

            def _test():
                from transcriber import test_api_connection
                result = test_api_connection(api_key, model)
                def _respond():
                    self._send_response({"action": "test_api_result", **result})
                AppHelper.callAfter(_respond)

            threading.Thread(target=_test, daemon=True).start()

        # --- Заметки ---
        elif action == "save_note":
            text = str(data.get("text", ""))
            if text:
                db.save_note(text)
            self._send_response({"action": "note_saved"})

        elif action == "get_notes":
            limit = int(data.get("limit", 50))
            offset = int(data.get("offset", 0))
            rows = db.get_notes(limit, offset)
            self._send_response({"action": "notes", "data": rows, "append": offset > 0})

        elif action == "search_notes":
            query = str(data.get("query", ""))
            limit = int(data.get("limit", 50))
            offset = int(data.get("offset", 0))
            rows = db.search_notes(query, limit, offset)
            self._send_response({"action": "notes", "data": rows, "append": offset > 0})

        elif action == "update_note":
            row_id = int(data.get("id", 0))
            text = str(data.get("text", ""))
            db.update_note(row_id, text)
            self._send_response({"action": "note_updated", "id": row_id, "text": text})

        elif action == "delete_note":
            row_id = int(data.get("id", 0))
            db.delete_note(row_id)
            self._send_response({"action": "note_deleted", "id": row_id})

        # --- Дневник ---
        elif action == "get_diary":
            date = str(data.get("date", ""))
            entry = db.get_diary(date)
            self._send_response({"action": "diary", "data": entry})

        elif action == "save_diary":
            date = str(data.get("date", ""))
            text = str(data.get("text", ""))
            db.save_diary(date, text)
            self._send_response({"action": "diary_saved"})

        elif action == "get_diary_streak":
            streak = db.get_diary_streak()
            self._send_response({"action": "diary_streak", "data": streak})

        # --- Задачи ---
        elif action == "save_task":
            text = str(data.get("text", ""))
            if text:
                db.save_task(text)
            self._send_response({"action": "task_saved"})

        elif action == "get_tasks":
            f = str(data.get("filter", "all"))
            rows = db.get_tasks(f)
            self._send_response({"action": "tasks", "data": rows})

        elif action == "toggle_task":
            row_id = int(data.get("id", 0))
            db.toggle_task(row_id)
            self._send_response({"action": "task_toggled"})

        elif action == "update_task":
            row_id = int(data.get("id", 0))
            text = str(data.get("text", ""))
            db.update_task(row_id, text)
            self._send_response({"action": "task_updated", "id": row_id, "text": text})

        elif action == "delete_task":
            row_id = int(data.get("id", 0))
            db.delete_task(row_id)
            self._send_response({"action": "task_deleted", "id": row_id})

        # --- Voice recording ---
        elif action == "start_voice":
            target = str(data.get("target", "note"))
            if self._voice_recording or not self._recorder or not self._transcriber:
                self._send_response({"action": "voice_error", "error": "busy"})
                return
            if self._recorder.is_recording:
                self._send_response({"action": "voice_error", "error": "busy"})
                return
            self._voice_recording = True
            self._voice_target = target
            self._send_response({"action": "voice_started"})

            import threading
            def _start():
                try:
                    self._recorder.start()
                except Exception:
                    log.exception("Voice recording failed to start")
                    self._voice_recording = False
                    def _err():
                        self._send_response({"action": "voice_error", "error": "mic"})
                    AppHelper.callAfter(_err)
            threading.Thread(target=_start, daemon=True).start()

        elif action == "stop_voice":
            if not self._voice_recording:
                return
            target = self._voice_target
            self._voice_recording = False
            self._voice_target = None

            import threading
            def _stop_and_transcribe():
                try:
                    audio = self._recorder.stop()
                    if len(audio) == 0:
                        def _empty():
                            self._send_response({"action": "voice_result", "target": target, "text": ""})
                        AppHelper.callAfter(_empty)
                        return
                    text = self._transcriber.transcribe(audio)
                    if text:
                        if target == "note":
                            db.save_note(text)
                        elif target == "task":
                            db.save_task(text)
                        # diary: JS handles save_diary with appended text
                    def _done():
                        self._send_response({"action": "voice_result", "target": target, "text": text or ""})
                    AppHelper.callAfter(_done)
                except Exception:
                    log.exception("Voice transcription failed")
                    def _err():
                        self._send_response({"action": "voice_error", "error": "transcribe"})
                    AppHelper.callAfter(_err)
            threading.Thread(target=_stop_and_transcribe, daemon=True).start()
