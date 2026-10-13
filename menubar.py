"""macOS menu bar indicator using rumps."""

import os
import threading
import time

import AppKit
import rumps
from PyObjCTools import AppHelper

import db
import languages
import menu_icons
from config import Config
from feedback import Feedback
from history_ui import HistoryWindow
from hotkey import HotkeyListener
from output import copy_text, paste_text
from overlay import Overlay
from recorder import Recorder
from screenshot import ScreenshotEditor
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

# The status line at the top of the menu: an SF Symbol per state.
_STATUS_SYMBOLS = {
    "starting": "hourglass",
    "ready": "mic",
    "recording": "record.circle",
    "processing": "hourglass",
    "done": "checkmark.circle",
    "error": "exclamationmark.triangle",
}

# Switching the language this soon after a dictation reads as "that one came
# out in the wrong language": the dictation is handed over again in the new
# language, on the clipboard and on screen. Later than this, a switch is just
# a switch — it only affects what comes next.
_RELANG_WINDOW = 90.0


def _icon_file(state: str) -> str:
    from utils import resource_path
    return str(resource_path() / "assets" / "menubar" / f"{state}.png")


def _set_image(item: rumps.MenuItem, image) -> None:
    """Put an NSImage on a rumps menu item (rumps itself only takes files)."""
    try:
        item._menuitem.setImage_(image)
    except Exception:
        log.exception("could not set a menu item image")


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
        self._screenshot = ScreenshotEditor(
            config, on_copied=self._on_screenshot_copied)
        self._history_window = HistoryWindow(config)
        self._history_window._recorder = self._recorder
        self._history_window._transcriber = self._transcriber
        self._transcription_lock = threading.Lock()
        self._is_recording = False
        self._is_processing = False
        self._watchdog_stop = threading.Event()
        self._hide_timer: threading.Timer | None = None
        self._ready = False
        self._icon_state = ICON_IDLE
        # (monotonic time, entry dict) of the last dictation — what a
        # language switch right after it hands over again.
        self._last_dictation: tuple[float, dict] | None = None

        db.init_db()

        self._hotkey: HotkeyListener | None = None

        self._status_item = rumps.MenuItem("Запуск…")
        self._status_item.set_callback(None)
        self._set_status("starting", "Запуск…")
        self._lang_ru = rumps.MenuItem("Русский", callback=self._select_ru)
        self._lang_en = rumps.MenuItem("English — перевод",
                                       callback=self._select_en)
        self._lang_uk = rumps.MenuItem("Українська — переклад",
                                       callback=self._select_uk)
        for code, item in (("ru", self._lang_ru), ("en", self._lang_en),
                           ("uk", self._lang_uk)):
            _set_image(item, menu_icons.language_badge(languages.BADGE[code]))
        self._update_lang_checks()
        self._translate_item = rumps.MenuItem(
            "Перевести выделенное", callback=self._translate_menu)
        _set_image(self._translate_item, menu_icons.symbol("translate"))
        self._screenshot_item = rumps.MenuItem(
            "Скриншот с пометками", callback=self._screenshot_menu)
        _set_image(self._screenshot_item, menu_icons.symbol("camera.viewfinder"))
        history_item = rumps.MenuItem("История…", callback=self._show_history)
        _set_image(history_item, menu_icons.symbol("clock.arrow.circlepath"))
        self._refresh_menu_shortcuts()
        self._updater: Updater | None = None
        self.menu = [
            self._status_item,
            None,
            self._lang_ru,
            self._lang_en,
            self._lang_uk,
            None,
            self._translate_item,
            self._screenshot_item,
            None,
            history_item,
            None,
            rumps.MenuItem("Выйти из AudioLog", callback=self._quit),
        ]

    def set_icon(self, state: str, title: str = "") -> None:
        """Swap the menu bar image, plus optional text beside it.

        While dictation is translated the glyph carries the target
        language's tag, so the mode is visible without opening the menu.
        The image is a template: macOS tints it for the menu bar.
        """
        self._icon_state = state
        try:
            self._icon = _icon_file(state)
            self._icon_nsimage = menu_icons.status_icon(
                _icon_file(state), self._menu_bar_badge())
            nsapp = getattr(self, "_nsapp", None)
            if nsapp is not None:
                nsapp.setStatusBarIcon()
        except Exception:
            log.exception("could not set menu bar icon %r", state)
        self.title = title

    def _menu_bar_badge(self) -> str | None:
        """The language tag for the menu bar — only while translating."""
        out = languages.output_language(self._config)
        if out == (self._config.language or "ru"):
            return None
        return languages.BADGE.get(out, out.upper())

    def _set_status(self, kind: str, text: str) -> None:
        """The status line at the top of the menu. Main thread."""
        self._status_item.title = text
        _set_image(self._status_item,
                   menu_icons.symbol(_STATUS_SYMBOLS.get(kind, "mic")))

    def _set_state(self, icon: str, status: str,
                   overlay_text: str | None = None,
                   overlay_state: str = "record",
                   kind: str | None = None) -> None:
        """Thread-safe UI update — dispatches to main thread."""
        if kind is None:
            kind = {ICON_RECORDING: "recording",
                    ICON_TRANSCRIBING: "processing"}.get(icon, "ready")

        def _update():
            self.set_icon(icon)
            self._set_status(kind, status)
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
        badge = self._menu_bar_badge()
        rec_label = f"Запись… → {badge}" if badge else "Запись…"
        self._set_status("recording", rec_label)
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
                self._set_state(ICON_IDLE, "Ошибка записи", "Ошибка", "error",
                                kind="error")
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
            self._set_status("error", "Запись остановлена по таймауту")
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
        self._set_status("processing", "Обработка…")
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
            self._set_state(ICON_TRANSCRIBING, "Распознавание…",
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
                    self._set_state(ICON_IDLE, "Ошибка распознавания",
                                    "Ошибка", "error", kind="error")
                    self._auto_hide_overlay(delay=5.0)
                    return

                if not text:
                    self._set_state(ICON_IDLE, "Готов к записи")
                    return
                # Which language that came out in, and what was said when
                # it is a translation — read while the lock still holds.
                entry = {
                    "id": None,
                    "text": text,
                    "lang": getattr(self._transcriber, "last_lang", None),
                    "source_text": getattr(self._transcriber,
                                           "last_source_text", None),
                    "source_lang": getattr(self._transcriber,
                                           "last_source_lang", None),
                }

            # Paste OUTSIDE the lock — paste_text can take up to 5s
            try:
                paste_text(text)
            except Exception:
                log.exception("Paste failed (text saved to DB and clipboard)")
            self._last_dictation = (time.monotonic(), entry)

            # Save to DB in background — don't block the pipeline
            dur = self._recorder.last_duration
            rms = self._recorder.last_rms
            peak = self._recorder.last_peak

            def _save():
                entry["id"] = db.save(
                    text, dur, rms, peak, lang=entry["lang"],
                    source_text=entry["source_text"],
                    source_lang=entry["source_lang"])
                self._history_window.notify_new_entry()
            threading.Thread(target=_save, daemon=True).start()
            short = text if len(text) <= 40 else text[:40] + "…"
            self._set_state(ICON_IDLE, f"Вставлено: {short}",
                            "Вставлено!", "done", kind="done")
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
        self.set_icon(self._icon_state)   # the menu bar tag follows the mode

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
        self._history_window.notify_settings_changed()
        self._relang_last()

    def _relang_last(self) -> None:
        """Hand the last dictation over again in the language just picked.

        For "I forgot it was set to English": dictate, see English appear,
        pick Русский in the menu — and the same dictation in Russian is on
        the clipboard, shown on screen, ready for ⌘V. Instant when it was
        spoken in Russian (the original is kept), one Claude call otherwise.
        """
        last = self._last_dictation
        if last is None or self._is_recording or self._is_processing:
            return
        at, entry = last
        if time.monotonic() - at > _RELANG_WINDOW:
            return
        want = languages.output_language(self._config)
        if languages.entry_language(entry) == want:
            return
        threading.Thread(target=self._relang_worker, args=(entry, want),
                         daemon=True, name="relang").start()

    def _relang_worker(self, entry: dict, want: str) -> None:
        popup = self._translator._popup
        where = languages.IN_NAME.get(want, want)
        ready = languages.ready_text(entry, want)
        if not ready:
            AppHelper.callAfter(popup.show, entry["text"],
                                f"Перевожу последнюю запись {where}…")
        try:
            text = ready or languages.convert(entry, want, self._config)
        except Exception as e:
            log.exception("Could not re-deliver the last dictation in %s", want)
            AppHelper.callAfter(popup.show, f"Перевести не получилось: {e}",
                                "Последняя запись")
            return

        def _deliver():
            copy_text(text)
            popup.show(text, f"Последняя запись {where} — уже в буфере, "
                             f"вставь ⌘V  ·  Esc — закрыть")
        AppHelper.callAfter(_deliver)
        log.info("Last dictation handed over again in %s (%d chars, %s)",
                 want, len(text), "ready" if ready else "translated")

    def _refresh_menu_shortcuts(self) -> None:
        """Show the translate and screenshot shortcuts beside their items."""
        for item, key, mods, enabled in (
            (self._translate_item, self._config.translate_hotkey_key,
             self._config.translate_hotkey_mods,
             getattr(self._config, "translate_enabled", True)),
            (self._screenshot_item, self._config.screenshot_hotkey_key,
             self._config.screenshot_hotkey_mods,
             getattr(self._config, "screenshot_enabled", True)),
        ):
            char = menu_icons.key_equivalent(key) if enabled else ""
            try:
                item._menuitem.setKeyEquivalent_(char)
                item._menuitem.setKeyEquivalentModifierMask_(mods if char else 0)
            except Exception:
                log.exception("could not show a menu shortcut")

    def _on_settings_saved(self) -> None:
        """Settings changed in the window: bring the menu up to date."""
        self._update_lang_checks()
        self._refresh_menu_shortcuts()

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

    def _fire_screenshot(self) -> None:
        """Screenshot shortcut and menu item both land here. Main thread."""
        if not getattr(self._config, "screenshot_enabled", True):
            return
        from screenshot import request_screen_capture, screen_capture_granted
        if not screen_capture_granted():
            # A capture without this permission is a picture of the wallpaper
            # with every window missing — say why instead of showing that.
            log.warning("Screenshot: Screen Recording permission not granted")
            request_screen_capture()
            self._translator._popup.show(
                "Для скриншотов нужно разрешение «Запись экрана» — без него "
                "на снимке будут одни обои.\n\nСистемные настройки → "
                "Конфиденциальность и безопасность → Запись экрана → включить "
                "AudioLog. Если снимок после этого всё равно пустой — закрой "
                "AudioLog через меню (Выход) и открой заново из Applications."
            )
            return
        try:
            self._screenshot.capture()
        except Exception:
            log.exception("screenshot capture failed")

    def _screenshot_menu(self, _) -> None:
        self._fire_screenshot()

    def _on_screenshot_copied(self) -> None:
        """The editor put the marked-up shot on the clipboard."""
        self._cancel_auto_hide()
        self._overlay.show("Скриншот в буфере", state="done")
        self._auto_hide_overlay(delay=2.0)

    def _on_sigterm(self, *_args) -> None:
        """launchd stops us with SIGTERM (install, uninstall, logout).

        Without a handler the process just dies, and the system screenshot
        key we hold stays switched off until the next login. Quit the
        normal way instead, which gives it back.
        """
        log.info("SIGTERM — shutting down")
        self._quit(None)

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
                self._translator._popup.show("Запись не началась.\n\n" + text)
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
            on_screenshot=self._fire_screenshot,
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
        self._history_window._settings_saved_cb = self._on_settings_saved
        self._updater.start()

        threading.Thread(target=self._setup_status_item, daemon=True).start()
        threading.Thread(target=self._check_accessibility, daemon=True).start()

        # Show history window as the main window on launch
        self._history_window.show()

        # Delivered through a Mach port, so the handler runs on the main
        # thread even while the Cocoa run loop owns it.
        try:
            import signal
            from PyObjCTools import MachSignals
            MachSignals.signal(signal.SIGTERM, self._on_sigterm)
        except Exception:
            log.exception("could not install SIGTERM handler")

        super().run(**kwargs)
