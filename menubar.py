"""macOS menu bar indicator using rumps."""

import threading

import rumps
from PyObjCTools import AppHelper

import db
from config import Config
from feedback import Feedback
from history_ui import HistoryWindow
from hotkey import HotkeyListener
from output import paste_text
from overlay import Overlay
from recorder import Recorder
from transcriber import Transcriber
from utils import get_logger, setup_logging

log = get_logger(__name__)

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴"
ICON_TRANSCRIBING = "⏳"


class MenuBarApp(rumps.App):
    def __init__(self, config: Config) -> None:
        super().__init__(ICON_IDLE, quit_button=None)
        self._config = config
        setup_logging(config)

        self._feedback = Feedback(config)
        self._recorder = Recorder(config)
        self._transcriber = Transcriber(config)
        self._overlay = Overlay()
        self._history_window = HistoryWindow(config)
        self._transcription_lock = threading.Lock()
        self._ready = False

        db.init_db()

        self._hotkey: HotkeyListener | None = None

        self._status_item = rumps.MenuItem("⏳ Загрузка модели…")
        self._status_item.set_callback(None)
        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("📋 История", callback=self._show_history),
            rumps.MenuItem("Хоткей: Right Option (зажать)"),
            None,
            rumps.MenuItem("Выход", callback=self._quit),
        ]

    def _set_state(self, icon: str, status: str,
                   overlay_text: str | None = None,
                   overlay_state: str = "record") -> None:
        """Thread-safe UI update — dispatches to main thread."""
        def _update():
            self.title = icon
            self._status_item.title = status
            if overlay_text:
                self._overlay.show(overlay_text, state=overlay_state)
            else:
                self._overlay.hide()
        AppHelper.callAfter(_update)

    def _on_activate(self) -> None:
        """Called on main thread when hotkey is pressed."""
        if not self._ready:
            return
        self._feedback.on_record_start()
        self._recorder.start()
        self.title = ICON_RECORDING
        self._status_item.title = "🔴 Запись…"
        self._overlay.show("Запись…", state="record")

    def _on_deactivate(self) -> None:
        """Called on main thread when hotkey is released."""
        if not self._ready:
            return
        audio = self._recorder.stop()
        self._feedback.on_record_stop()

        if len(audio) == 0:
            log.warning("No audio captured")
            self.title = ICON_IDLE
            self._status_item.title = "Готов к записи"
            self._overlay.hide()
            return

        self.title = ICON_TRANSCRIBING
        self._status_item.title = "⏳ Распознавание…"
        self._overlay.show("Распознавание…", state="process")

        t = threading.Thread(target=self._process, args=(audio,), daemon=True)
        t.start()

    def _process(self, audio) -> None:
        """Runs in worker thread."""
        with self._transcription_lock:
            try:
                text = self._transcriber.transcribe(audio)
                if text:
                    paste_text(text)
                    db.save(
                        text,
                        self._recorder.last_duration,
                        self._recorder.last_rms,
                        self._recorder.last_peak,
                    )
                    self._history_window.notify_new_entry()
                    self._set_state(ICON_IDLE, f"✅ {text[:40]}…",
                                    "Вставлено!", "done")
                    # Auto-hide overlay after 3 seconds
                    def _hide_later():
                        import time
                        time.sleep(3.0)
                        AppHelper.callAfter(self._overlay.hide)
                    threading.Thread(target=_hide_later, daemon=True).start()
                else:
                    self._set_state(ICON_IDLE, "Готов к записи")
            except Exception:
                log.exception("Transcription failed")
                self._feedback.on_error()
                self._set_state(ICON_IDLE, "❌ Ошибка", "Ошибка", "error")
                def _hide_error():
                    import time
                    time.sleep(3.0)
                    AppHelper.callAfter(self._overlay.hide)
                threading.Thread(target=_hide_error, daemon=True).start()

    def _load_model(self) -> None:
        """Runs in background thread."""
        log.info("Starting audio-log-project…")
        self._config.ensure_model_dir()
        self._transcriber.load_model()
        self._ready = True
        self._set_state(ICON_IDLE, "Готов к записи")
        log.info("Model loaded. Listening for hotkey…")

    def _show_history(self, _) -> None:
        self._history_window.show()

    def _quit(self, _) -> None:
        if self._hotkey:
            self._hotkey.stop()
        rumps.quit_application()

    def run(self, **kwargs) -> None:
        self._overlay.build()
        self._history_window.build()

        self._hotkey = HotkeyListener(
            config=self._config,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )
        self._hotkey.start()

        t = threading.Thread(target=self._load_model, daemon=True)
        t.start()

        super().run(**kwargs)
