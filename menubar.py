"""macOS menu bar indicator using rumps."""

import threading
import time
from pathlib import Path

import AppKit
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
from transcriber import create_transcriber
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

        # Apply saved settings to config on startup
        saved = config.load_settings()
        config.apply_settings(saved)

        self._feedback = Feedback(config)
        self._recorder = Recorder(config)
        self._transcriber = create_transcriber(config)
        self._overlay = Overlay()
        self._history_window = HistoryWindow(config)
        self._history_window._recorder = self._recorder
        self._history_window._transcriber = self._transcriber
        self._transcription_lock = threading.Lock()
        self._is_recording = False
        self._is_processing = False
        self._watchdog_stop = threading.Event()
        self._hide_timer: threading.Timer | None = None
        self._ready = False

        db.init_db()
        db.init_notes_table()

        self._hotkey: HotkeyListener | None = None
        self._translate_mode = config.translate  # RU→EN translation

        self._status_item = rumps.MenuItem("⏳ Загрузка модели…")
        self._status_item.set_callback(None)
        self._lang_ru = rumps.MenuItem(
            "🇷🇺 Русский", callback=self._select_ru)
        self._lang_en = rumps.MenuItem(
            "🇺🇸 English", callback=self._select_en)
        self._update_lang_checks()
        self.menu = [
            self._status_item,
            None,
            self._lang_ru,
            self._lang_en,
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
        if not self._ready or self._is_recording:
            return
        if self._history_window._voice_recording:
            return
        if self._is_processing:
            self._overlay.show("Подождите…", state="process")
            # Reset hotkey toggle — we didn't actually start recording
            if self._hotkey:
                self._hotkey.reset_toggle()
            return
        self._is_recording = True
        self._cancel_auto_hide()  # prevent previous timer from hiding new overlay
        self._feedback.on_record_start()
        self.title = ICON_RECORDING
        rec_label = "Запись… → EN" if self._translate_mode else "Запись…"
        self._status_item.title = f"🔴 {rec_label}"
        self._overlay.show(rec_label, state="record")

        # Set recording_start_time early so watchdog has a valid baseline
        # (recorder.start() runs in bg thread and may be slow due to cleanup)
        self._recorder.recording_start_time = time.monotonic()

        def _start():
            try:
                self._recorder.start()
            except Exception:
                log.exception("Failed to start recording")
                self._is_recording = False
                if self._hotkey:
                    self._hotkey.reset_toggle()
                AppKit.NSBeep()
                self._set_state(ICON_IDLE, "❌ Ошибка записи", "Ошибка", "error")
                self._auto_hide_overlay(delay=5.0)

        threading.Thread(target=_start, daemon=True).start()

        # Safety watchdog — runs in background thread, independent of main thread.
        # Checks every 5 seconds and force-stops if max duration exceeded.
        self._watchdog_stop.clear()
        threading.Thread(target=self._safety_watchdog, daemon=True).start()

    def _safety_watchdog(self) -> None:
        """Background thread: force-stops recording after max_recording_seconds.

        Does NOT use AppHelper.callAfter for the critical stop logic,
        so it works even if the main thread / NSApplication run loop is blocked.
        """
        max_sec = float(self._config.max_recording_seconds)
        while not self._watchdog_stop.wait(timeout=5.0):
            if not self._is_recording:
                return
            elapsed = time.monotonic() - self._recorder.recording_start_time
            if elapsed >= max_sec:
                log.warning("Safety watchdog: force-stopping recording "
                            "after %.0f seconds (limit %d)",
                            elapsed, self._config.max_recording_seconds)
                self._force_stop_recording()
                return

    def _force_stop_recording(self) -> None:
        """Force-stop recording from ANY thread (safety watchdog)."""
        if not self._is_recording:
            return
        self._is_recording = False
        self._watchdog_stop.set()
        if self._hotkey:
            self._hotkey.reset_toggle()

        audio = self._recorder.stop()

        if len(audio) > 0:
            self._is_processing = True  # set before spawning worker
            t = threading.Thread(target=self._process, args=(audio,), daemon=True)
            t.start()

        # UI updates — best effort via main thread
        def _update_ui():
            self.title = ICON_IDLE
            self._status_item.title = "⚠️ Запись остановлена (таймаут)"
            self._overlay.show("Таймаут!", state="error")
            self._auto_hide_overlay()

        try:
            AppHelper.callAfter(_update_ui)
        except Exception:
            pass

    def _on_cancel(self) -> None:
        """Called on main thread when ESC is pressed during recording."""
        if not self._is_recording:
            return
        self._is_recording = False
        self._watchdog_stop.set()
        self._feedback.on_record_stop()

        def _cancel():
            self._recorder.stop()  # stop recording, discard audio
            self._set_state(ICON_IDLE, "Готов к записи", "Отменено", "error")
            self._auto_hide_overlay(delay=2.0)

        threading.Thread(target=_cancel, daemon=True).start()

    def _on_deactivate(self) -> None:
        """Called on main thread when hotkey is released."""
        if not self._ready or not self._is_recording:
            # Sync hotkey toggle state in case it's out of sync
            if self._hotkey:
                self._hotkey.reset_toggle()
            return
        self._is_recording = False
        self._is_processing = True  # block new recordings immediately
        self._watchdog_stop.set()
        if self._hotkey:
            self._hotkey.reset_toggle()
        self._feedback.on_record_stop()

        self.title = ICON_TRANSCRIBING
        self._status_item.title = "⏳ Обработка…"
        self._overlay.show("Обработка…", state="process")

        # Stop recorder and process in background — don't block main thread.
        # Main thread must stay responsive for hotkey events.
        def _stop_and_process():
            audio = self._recorder.stop()
            if len(audio) == 0:
                log.warning("No audio captured")
                self._is_processing = False
                self._set_state(ICON_IDLE, "Готов к записи")
                return
            self._set_state(ICON_TRANSCRIBING, "⏳ Распознавание…",
                            "Распознавание…", "process")
            self._process(audio)

        threading.Thread(target=_stop_and_process, daemon=True).start()

    def _process(self, audio) -> None:
        """Runs in worker thread."""
        try:
            with self._transcription_lock:
                try:
                    text = self._transcriber.transcribe(audio)
                except Exception:
                    log.exception("Transcription failed")
                    self._feedback.on_error()
                    AppKit.NSBeep()
                    self._set_state(ICON_IDLE, "❌ Ошибка распознавания",
                                    "Ошибка", "error")
                    self._auto_hide_overlay(delay=5.0)
                    return

                if not text:
                    self._set_state(ICON_IDLE, "Готов к записи")
                    return

            # Paste OUTSIDE the lock — paste_text can take up to 5s
            try:
                paste_text(text)
            except Exception:
                log.exception("Paste failed (text saved to DB and clipboard)")

            db.save(
                text,
                self._recorder.last_duration,
                self._recorder.last_rms,
                self._recorder.last_peak,
            )
            self._history_window.notify_new_entry()
            self._set_state(ICON_IDLE, f"✅ {text[:40]}…",
                            "Вставлено!", "done")
            self._auto_hide_overlay()
        finally:
            self._is_processing = False

    def _cancel_auto_hide(self) -> None:
        """Cancel pending auto-hide timer to prevent it from hiding a new overlay."""
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None

    def _auto_hide_overlay(self, delay: float = 3.0) -> None:
        self._cancel_auto_hide()
        self._hide_timer = threading.Timer(
            delay, lambda: AppHelper.callAfter(self._overlay.hide))
        self._hide_timer.daemon = True
        self._hide_timer.start()

    def _load_model(self) -> None:
        """Runs in background thread."""
        log.info("Starting audio-log-project…")
        if self._config.transcription_mode == "api":
            self._transcriber.load_model()
            self._ready = True
            self._set_state(ICON_IDLE, "Готов к записи (API)")
            log.info("API mode — ready. Listening for hotkey…")
        else:
            self._config.ensure_model_dir()
            self._transcriber.load_model()
            self._ready = True
            self._set_state(ICON_IDLE, "Готов к записи")
            log.info("Model loaded. Listening for hotkey…")

    def _update_lang_checks(self) -> None:
        self._lang_ru.state = not self._translate_mode
        self._lang_en.state = self._translate_mode

    def _select_ru(self, _) -> None:
        if not self._translate_mode:
            return
        self._translate_mode = False
        self._config.translate = False
        self._update_lang_checks()
        log.info("Language mode switched to RU")

    def _select_en(self, _) -> None:
        if self._translate_mode:
            return
        self._translate_mode = True
        self._config.translate = True
        self._update_lang_checks()
        log.info("Language mode switched to EN (translate)")

    def _show_history(self, _) -> None:
        self._history_window.show()

    def _quit(self, _) -> None:
        if self._hotkey:
            self._hotkey.stop()
        rumps.quit_application()

    def run(self, **kwargs) -> None:
        nsapp = AppKit.NSApplication.sharedApplication()
        # NSApplicationActivationPolicyRegular — shows in Dock
        nsapp.setActivationPolicy_(0)

        # Set application icon (overrides default Python icon)
        icon_path = Path(__file__).parent / "assets" / "AppIcon.icns"
        if icon_path.exists():
            icon_image = AppKit.NSImage.alloc().initWithContentsOfFile_(
                str(icon_path)
            )
            nsapp.setApplicationIconImage_(icon_image)

        self._overlay.build()
        self._history_window.build()

        # Inject Dock-click handler into rumps delegate class (rumps.rumps.NSApp)
        from rumps.rumps import NSApp as RumpsDelegate
        history_window = self._history_window

        def applicationShouldHandleReopen_hasVisibleWindows_(
            _self, _reopen, _has_visible
        ):
            history_window.show()
            return True
        RumpsDelegate.applicationShouldHandleReopen_hasVisibleWindows_ = (
            applicationShouldHandleReopen_hasVisibleWindows_
        )

        self._hotkey = HotkeyListener(
            config=self._config,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
            on_cancel=self._on_cancel,
        )
        self._hotkey.start()

        t = threading.Thread(target=self._load_model, daemon=True)
        t.start()

        # Show history window as the main window on launch
        self._history_window.show()

        super().run(**kwargs)
