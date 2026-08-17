"""macOS menu bar indicator using rumps."""

import os
import threading
import time

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
from translate_popup import TranslateController
from updater import Updater
from utils import get_logger, setup_logging

log = get_logger(__name__)

# macOS remembers the menu bar icon position under this name, so the icon
# stays where the user ⌘-dragged it across restarts and auto-updates.
STATUS_ITEM_AUTOSAVE = "AudioLogStatusItem"

# Menu bar states. Each name maps to assets/menubar/<name>.png — a template
# image (black + alpha), so macOS tints it for the light or dark menu bar.
# Redraw them with tools/make-icons.py.
ICON_IDLE = "idle"
ICON_RECORDING = "recording"
ICON_TRANSCRIBING = "processing"


def _icon_file(state: str) -> str:
    from utils import resource_path
    return str(resource_path() / "assets" / "menubar" / f"{state}.png")


class MenuBarApp(rumps.App):
    def __init__(self, config: Config) -> None:
        super().__init__("AudioLog", title="", icon=_icon_file(ICON_IDLE),
                         template=True, quit_button=None)
        self._config = config
        setup_logging(config)

        # Apply saved settings to config on startup
        saved = config.load_settings()
        config.apply_settings(saved)

        self._feedback = Feedback(config)
        self._recorder = Recorder(config)
        self._transcriber = create_transcriber(config)
        self._overlay = Overlay()
        self._translator = TranslateController(config)
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

        self._hotkey: HotkeyListener | None = None

        self._status_item = rumps.MenuItem("⏳ Запуск…")
        self._status_item.set_callback(None)
        self._lang_ru = rumps.MenuItem(
            "🇷🇺 Русский", callback=self._select_ru)
        self._lang_en = rumps.MenuItem(
            "🇺🇸 English (перевод)", callback=self._select_en)
        self._lang_uk = rumps.MenuItem(
            "🇺🇦 Українська (переклад)", callback=self._select_uk)
        self._update_lang_checks()
        self._updater: Updater | None = None
        self.menu = [
            self._status_item,
            None,
            self._lang_ru,
            self._lang_en,
            self._lang_uk,
            None,
            rumps.MenuItem("🌐 Перевести выделенное",
                           callback=self._translate_menu),
            None,
            rumps.MenuItem("📋 История", callback=self._show_history),
            None,
            rumps.MenuItem("Выход", callback=self._quit),
        ]

    def set_icon(self, state: str, title: str = "") -> None:
        """Swap the menu bar image, plus optional text beside it.

        rumps keeps template mode across icon changes, so the glyph stays
        tinted by the system for the current menu bar appearance.
        """
        try:
            self.icon = _icon_file(state)
        except Exception:
            log.exception("could not set menu bar icon %r", state)
        self.title = title

    def _set_state(self, icon: str, status: str,
                   overlay_text: str | None = None,
                   overlay_state: str = "record") -> None:
        """Thread-safe UI update — dispatches to main thread."""
        def _update():
            self.set_icon(icon)
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
        self.set_icon(ICON_RECORDING)
        tgt = (self._config.target_language or "").lower()
        if tgt == "en":
            rec_label = "Запись… → EN"
        elif tgt == "uk":
            rec_label = "Запись… → UK"
        else:
            rec_label = "Запись…"
        self._status_item.title = f"🔴 {rec_label}"
        self._overlay.show(rec_label, state="record")

        # Set recording_start_time early so watchdog has a valid baseline
        # (recorder.start() runs in bg thread and may be slow due to cleanup)
        self._recorder.recording_start_time = time.monotonic()

        def _start():
            try:
                self._recorder.start()
                # Beep only once the device is actually delivering audio —
                # on a Bluetooth headset that is ~0.5s after the keypress,
                # and beeping earlier invites the user to talk into a mic
                # that isn't listening yet.
                AppHelper.callAfter(self._feedback.on_record_start)
            except Exception:
                log.exception("Failed to start recording")
                self._is_recording = False
                if self._hotkey:
                    self._hotkey.reset_toggle()
                AppKit.NSBeep()
                self._set_state(ICON_IDLE, "❌ Ошибка записи", "Ошибка", "error")
                self._auto_hide_overlay(delay=5.0)
                # "Recording failed" in the menu bar tells the user nothing
                # actionable. Say which cause it is, in a window they can read.
                self._explain_recording_failure()

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
            self.set_icon(ICON_IDLE)
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

        self.set_icon(ICON_TRANSCRIBING)
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

            # Save to DB in background — don't block the pipeline
            dur = self._recorder.last_duration
            rms = self._recorder.last_rms
            peak = self._recorder.last_peak
            threading.Thread(
                target=lambda: (db.save(text, dur, rms, peak),
                                self._history_window.notify_new_entry()),
                daemon=True,
            ).start()
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
        self._transcriber.load_model()
        self._ready = True
        label = "Groq" if self._config.transcription_mode == "groq" else "API"
        self._set_state(ICON_IDLE, f"Готов к записи ({label})")
        log.info("%s mode — ready. Listening for hotkey…", label)

    def _rebuild_transcriber(self) -> None:
        """Rebuild the transcriber after the mode changed in Settings.

        Called from the settings handler on the main thread; the actual
        work happens on a worker. Skipped mid-recording — swapping the
        engine underneath an in-flight transcription would lose it.
        """
        if self._is_recording or self._is_processing:
            log.warning("Mode changed while busy — new mode applies after "
                        "the current recording")
            return

        def _worker():
            try:
                new = create_transcriber(self._config)
                new.load_model()
            except Exception:
                log.exception("Could not build transcriber for mode %s",
                              self._config.transcription_mode)
                return

            self._transcriber = new
            self._history_window._transcriber = new
            log.info("Transcriber rebuilt: mode=%s",
                     self._config.transcription_mode)

        threading.Thread(target=_worker, daemon=True,
                         name="transcriber-rebuild").start()

    def _update_lang_checks(self) -> None:
        tgt = (self._config.target_language or "").lower()
        self._lang_ru.state = tgt == ""
        self._lang_en.state = tgt == "en"
        self._lang_uk.state = tgt == "uk"

    def _set_target(self, target: str) -> None:
        if (self._config.target_language or "") == target:
            return
        self._config.target_language = target
        self._update_lang_checks()
        # Persist to settings.json so choice survives restart
        settings = self._config.load_settings()
        settings["target_language"] = target
        try:
            self._config.save_settings(settings)
        except Exception:
            log.exception("Failed to persist target_language")
        log.info("Target language switched to %r", target or "none")

    def _select_ru(self, _) -> None:
        self._set_target("")

    def _select_en(self, _) -> None:
        self._set_target("en")

    def _select_uk(self, _) -> None:
        self._set_target("uk")

    def _fire_translate(self) -> None:
        """Translate hotkey and menu item both land here. Main thread only.

        Independent of the transcriber — translation works while the speech
        model is still loading.
        """
        if not getattr(self._config, "translate_enabled", True):
            return
        try:
            self._translator.toggle()
        except Exception:
            log.exception("translate popup failed")

    def _translate_menu(self, _) -> None:
        self._fire_translate()

    def _show_history(self, _) -> None:
        self._history_window.show()

    # ── auto-update ──

    def _busy_for_update(self) -> bool:
        """True while anything would be lost by restarting."""
        if self._is_recording or self._is_processing:
            return True
        if self._history_window._voice_recording:
            return True
        return False

    def _check_accessibility(self) -> None:
        """Ask macOS for Accessibility at startup if we don't have it yet.

        Without it CGEventPost is dropped silently and auto-paste does nothing.
        The system prompt is also what puts AudioLog into the Accessibility
        list in the first place — until it fires, there is no switch to flip.
        """
        time.sleep(6)
        from output import is_trusted, request_accessibility
        if is_trusted():
            log.info("Accessibility: granted — auto-paste is available")
            return
        log.warning("Accessibility: not granted — showing the system prompt")
        request_accessibility(prompt=True)
        self._warn_if_translate_dead()

    def _explain_recording_failure(self) -> None:
        """Show the actual reason recording could not start."""
        def _run():
            try:
                import diagnostics
                probe = diagnostics.probe_microphone(seconds=0.5)
                text = probe["message"]
            except Exception:
                log.exception("could not diagnose recording failure")
                text = "Запись не началась, и определить причину не удалось."

            def _show():
                self._translator._popup.show(
                    "Запись не началась.\n\n" + text + "\n\n"
                    "Если наушники подключены, но их нет в списке — macOS "
                    "держит их в режиме воспроизведения. Открой Системные "
                    "настройки → Звук → Вход и выбери их там; на Mac mini "
                    "другого микрофона нет."
                )
            AppHelper.callAfter(_show)

        threading.Thread(target=_run, daemon=True).start()

    def _warn_if_translate_dead(self) -> None:
        """Say out loud when the translate shortcut failed to arm.

        Otherwise the failure is completely silent: the shortcut simply does
        nothing, with no window, no sound and no hint about the missing
        permission — which is exactly how it looks on a fresh machine.
        """
        time.sleep(4)
        if self._hotkey is None or self._hotkey.translate_ready:
            return

        log.error("Translate shortcut is not armed — Accessibility missing")

        def _show():
            self._translator._popup.show(
                "Перевод по сочетанию клавиш не работает.\n\n"
                "macOS не дала приложению читать нажатия клавиш. Открой "
                "Системные настройки → Конфиденциальность и безопасность → "
                "Универсальный доступ и включи AudioLog — сочетание заработает "
                "само, перезапуск не нужен.\n\n"
                "Подробности: окно AudioLog → ⚙ → Диагностика."
            )
        AppHelper.callAfter(_show)

    def _setup_status_item(self) -> None:
        """Give the menu bar icon a remembered position, and check it is visible.

        Without an autosave name macOS re-inserts the icon at the leftmost slot
        on every launch — and this app relaunches itself on every update. On a
        notched Mac with a busy menu bar, "leftmost" lands *under the notch*,
        where the icon is invisible and unclickable even though macOS reports
        it as visible. With the name set, macOS remembers where the user
        dragged it (⌘-drag) and puts it back there after every restart.
        """
        time.sleep(4)

        def _do():
            try:
                item = getattr(self._nsapp, "nsstatusitem", None)
                if item is None:
                    log.warning("menubar icon: NSStatusItem was never created")
                    return

                button = item.button() if hasattr(item, "button") else None
                frame = None
                if button is not None and button.window() is not None:
                    frame = button.window().frame()
                if frame is None:
                    log.info("menubar icon: no frame yet")
                    return

                left, right = frame.origin.x, frame.origin.x + frame.size.width
                log.info("menubar icon: x=%.0f..%.0f title=%r",
                         left, right, self.title)

                screen = AppKit.NSScreen.mainScreen()
                if not hasattr(screen, "auxiliaryTopLeftArea"):
                    return
                notch_start = screen.auxiliaryTopLeftArea().size.width
                notch_end = screen.auxiliaryTopRightArea().origin.x
                if notch_end > notch_start and left < notch_end and right > notch_start:
                    log.warning(
                        "menubar icon is hidden behind the notch (icon %.0f..%.0f, "
                        "notch %.0f..%.0f) — menu bar is full. Free a slot or "
                        "⌘-drag icons to move it out.",
                        left, right, notch_start, notch_end)
            except Exception:
                log.exception("menubar icon setup failed")
        AppHelper.callAfter(_do)

    def _install_update(self, _=None) -> None:
        """Hand the bundle swap to the helper and quit so it can proceed.

        Called from the Settings tab («Установить и перезапустить») and by the
        updater itself once the app has been idle long enough.
        """
        if self._updater is None or not self._updater.staged_version:
            self._history_window.notify_update_failed(
                "Устанавливать нечего — обновление не загружено.")
            return

        self._overlay.show("Устанавливаю обновление…", state="process")
        if not self._updater.apply_now():
            # Tell the window, not just the overlay strip: the Settings tab
            # is showing "installing, the app will restart" and would sit
            # there forever otherwise.
            self._overlay.show("Не удалось установить обновление", state="error")
            self._auto_hide_overlay(delay=3.0)
            self._history_window.notify_update_failed(
                "Не удалось запустить установку. Подробности в логе: "
                "⚙ → Диагностика → Показать лог.")
            return

        if self._hotkey:
            self._hotkey.stop()

        # Belt and braces: if the Cocoa shutdown stalls, the helper waits on
        # our exit and the user just watches "installing…" forever. Force the
        # exit shortly after asking politely.
        def _force_exit():
            time.sleep(4)
            log.warning("Still alive after quit_application — forcing exit "
                        "so the update helper can proceed")
            os._exit(0)

        threading.Thread(target=_force_exit, daemon=True).start()
        rumps.quit_application()

    def _on_update_staged(self, new_version: str) -> None:
        """Called from the updater thread once a build is verified + staged."""
        log.info("update %s staged and verified", new_version)

    def _on_update_apply(self) -> None:
        """Called from the updater thread when it decides to restart silently."""
        def _do():
            if self._busy_for_update():
                return
            self._install_update(None)
        AppHelper.callAfter(_do)

    def _quit(self, _) -> None:
        if self._hotkey:
            self._hotkey.stop()
        if self._updater:
            self._updater.stop()
        rumps.quit_application()

    def run(self, **kwargs) -> None:
        nsapp = AppKit.NSApplication.sharedApplication()
        # NSApplicationActivationPolicyRegular — shows in Dock
        nsapp.setActivationPolicy_(0)

        # Set application icon (overrides default Python icon)
        from utils import resource_path
        icon_path = resource_path() / "assets" / "AppIcon.icns"
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

        # Name the status item the moment rumps creates it. macOS only honours
        # a remembered menu bar position if the autosave name is set while the
        # item is being placed — set it later and the icon goes wherever the
        # system wants, which on a full notched menu bar means behind the notch.
        _orig_did_finish = RumpsDelegate.applicationDidFinishLaunching_

        def applicationDidFinishLaunching_(_self, notification):
            _orig_did_finish(_self, notification)
            try:
                item = getattr(_self, "nsstatusitem", None)
                if item is not None and hasattr(item, "setAutosaveName_"):
                    item.setAutosaveName_(STATUS_ITEM_AUTOSAVE)
            except Exception:
                log.exception("could not set status item autosave name")
        RumpsDelegate.applicationDidFinishLaunching_ = (
            applicationDidFinishLaunching_
        )

        self._hotkey = HotkeyListener(
            config=self._config,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
            on_cancel=self._on_cancel,
            on_translate_toggle=self._fire_translate,
        )
        self._hotkey.start()
        # History window's settings tab needs to live-apply hotkey changes
        self._history_window._hotkey_listener = self._hotkey

        t = threading.Thread(target=self._load_model, daemon=True)
        t.start()

        # Auto-update: no-op when running from source (see updater.py).
        # The UI for it lives in the History window's Settings tab.
        self._updater = Updater(
            config=self._config,
            is_busy=self._busy_for_update,
            on_staged=self._on_update_staged,
            on_apply=self._on_update_apply,
        )
        self._history_window._updater = self._updater
        self._history_window._install_update_cb = self._install_update
        self._history_window._rebuild_transcriber_cb = self._rebuild_transcriber
        self._updater.start()

        threading.Thread(target=self._setup_status_item, daemon=True).start()
        threading.Thread(target=self._check_accessibility, daemon=True).start()

        # Show history window as the main window on launch
        self._history_window.show()

        super().run(**kwargs)
