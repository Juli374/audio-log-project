"""History window using NSWindow + WKWebView."""

import json

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
        self._hotkey_listener = None   # set by MenuBarApp after listener.start()
        self._updater = None           # set by MenuBarApp after updater.start()
        self._install_update_cb = None  # MenuBarApp quits the app, then swaps
        self._rebuild_transcriber_cb = None  # MenuBarApp owns the transcriber
        self._voice_recording = False
        self._voice_target = None  # 'note' or 'task'

    def _rebuild_transcriber(self) -> None:
        """Swap in a transcriber for the newly-chosen mode."""
        if self._rebuild_transcriber_cb is None:
            log.warning("Transcription mode changed but no rebuild hook set — "
                        "restart required for it to take effect")
            return
        try:
            self._rebuild_transcriber_cb()
            log.info("Transcriber rebuilt for mode %s",
                     self._config.transcription_mode)
        except Exception:
            log.exception("Failed to rebuild transcriber")

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
        from utils import resource_path
        html_path = resource_path() / "ui" / "index.html"
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
            from version import current as current_version
            settings = self._config.load_settings()
            settings["auto_update"] = bool(
                settings.get("auto_update", self._config.auto_update))
            self._send_response({
                "action": "settings",
                "data": settings,
                "version": current_version(),
                "staged_version": (self._updater.staged_version
                                   if self._updater else None),
            })

        elif action == "get_permissions":
            from output import is_trusted
            self._send_response({"action": "permissions",
                                 "accessibility": is_trusted()})

        elif action == "request_accessibility":
            from output import (is_trusted, open_accessibility_settings,
                                request_accessibility)
            granted = request_accessibility(prompt=True)
            if not granted:
                open_accessibility_settings()
            self._send_response({"action": "permissions",
                                 "accessibility": is_trusted()})

        elif action == "check_updates":
            import threading
            if self._updater is None:
                self._send_response({
                    "action": "update_status", "status": "error",
                    "detail": "Обновления доступны только в установленном приложении"})
                return

            def _check():
                status, detail = self._updater.check_now()

                def _respond():
                    self._send_response({"action": "update_status",
                                         "status": status, "detail": detail})
                AppHelper.callAfter(_respond)

            threading.Thread(target=_check, daemon=True).start()

        elif action == "install_update":
            if self._updater is None or not self._updater.staged_version:
                self._send_response({
                    "action": "update_status", "status": "error",
                    "detail": "Нечего устанавливать"})
                return
            if self._install_update_cb is not None:
                self._install_update_cb()
            else:
                self._updater.apply_now()

        elif action == "save_settings":
            from hotkey import (DEFAULT_TRANSLATE_COMBO, MODIFIER_KEY_FLAGS,
                                TRANSLATE_COMBOS)

            short_kc = int(data.get(
                "hotkey_keycode", self._config.hotkey_keycode))
            # Reject unknown / conflicting keycodes — fall back silently
            # to current values rather than corrupting state.
            if short_kc not in MODIFIER_KEY_FLAGS:
                short_kc = self._config.hotkey_keycode
            # The translate shortcut is a key combination, so it cannot
            # collide with the bare-modifier recording key.
            translate_combo = str(data.get(
                "translate_hotkey_combo",
                getattr(self._config, "translate_hotkey_combo",
                        DEFAULT_TRANSLATE_COMBO)))
            if translate_combo not in TRANSLATE_COMBOS:
                translate_combo = getattr(
                    self._config, "translate_hotkey_combo",
                    DEFAULT_TRANSLATE_COMBO)

            settings = {
                "language": str(data.get("language", "ru")),
                "max_recording_seconds": int(data.get("max_recording_seconds", 300)),
                "hotkey_mode": str(data.get("hotkey_mode", "hold")),
                "hotkey_keycode": short_kc,
                "transcription_mode": str(data.get("transcription_mode", "groq")),
                "openai_api_key": str(data.get("openai_api_key", "")),
                "openai_model": str(data.get("openai_model", "gpt-4o-mini-transcribe")),
                "groq_api_key": str(data.get("groq_api_key", "")),
                "groq_model": str(data.get("groq_model", "whisper-large-v3-turbo")),
                "anthropic_api_key": str(data.get("anthropic_api_key", "")),
                "cleanup_mode": str(data.get("cleanup_mode", "off")),
                "target_language": str(data.get("target_language", "")),
                "translate_hotkey_combo": translate_combo,
                "translate_target": str(data.get("translate_target", "ru")),
                "auto_update": bool(data.get("auto_update", True)),
            }
            previous_mode = self._config.transcription_mode
            self._config.save_settings(settings)
            # Live-apply hotkey changes — no restart needed.
            if self._hotkey_listener is not None:
                try:
                    self._hotkey_listener.set_keycode(short_kc)
                    self._hotkey_listener.set_translate_combo(translate_combo)
                except Exception:
                    log.exception("Failed to live-apply hotkey changes")
            # The transcriber is picked once, at startup, from the mode in
            # config. Switching Groq↔OpenAI in the UI used to change nothing
            # until a restart, so the app kept using the previous service and
            # its key — exactly the "wrong key" symptom.
            if self._config.transcription_mode != previous_mode:
                self._rebuild_transcriber()
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

        elif action == "test_groq":
            import threading
            api_key = str(data.get("api_key", ""))
            model = str(data.get("model", "whisper-large-v3-turbo"))

            def _test_groq():
                from transcriber import test_groq_connection
                result = test_groq_connection(api_key, model)
                def _respond():
                    self._send_response({"action": "test_groq_result", **result})
                AppHelper.callAfter(_respond)

            threading.Thread(target=_test_groq, daemon=True).start()
