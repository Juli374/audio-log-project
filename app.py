"""Application orchestrator (headless mode, no menu bar)."""

import signal
import threading

from AppKit import NSApplication

from config import Config
from feedback import Feedback
from hotkey import HotkeyListener
from output import paste_text
from recorder import Recorder
from transcriber import create_transcriber
from utils import get_logger, setup_logging

log = get_logger(__name__)


class App:
    def __init__(self, config: Config | None = None) -> None:
        self._config = config or Config()
        setup_logging(self._config)

        self._feedback = Feedback(self._config)
        self._recorder = Recorder(self._config)
        self._transcriber = create_transcriber(self._config)
        self._transcription_lock = threading.Lock()
        self._is_recording = False
        self._is_processing = False

        self._hotkey = HotkeyListener(
            config=self._config,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )

    def _on_activate(self) -> None:
        if self._is_recording or self._is_processing:
            return
        self._is_recording = True
        self._feedback.on_record_start()

        def _start():
            try:
                self._recorder.start()
            except Exception:
                log.exception("Failed to start recording")
                self._is_recording = False

        threading.Thread(target=_start, daemon=True).start()

    def _on_deactivate(self) -> None:
        if not self._is_recording:
            return
        self._is_recording = False
        audio = self._recorder.stop()
        self._feedback.on_record_stop()

        if len(audio) == 0:
            log.warning("No audio captured")
            return

        t = threading.Thread(target=self._process, args=(audio,), daemon=True)
        t.start()

    def _process(self, audio) -> None:
        self._is_processing = True
        try:
            with self._transcription_lock:
                try:
                    text = self._transcriber.transcribe(audio)
                except Exception:
                    log.exception("Transcription failed")
                    self._feedback.on_error()
                    return

            # Paste OUTSIDE the lock
            if text:
                try:
                    paste_text(text)
                except Exception:
                    log.exception("Paste failed")
        finally:
            self._is_processing = False

    def run(self) -> None:
        log.info("Starting audio-log-project (headless)…")

        self._config.ensure_model_dir()
        self._transcriber.load_model()
        log.info("Model loaded. Listening for hotkey…")

        self._hotkey.start()

        # NSEvent monitor needs an NSApplication run loop
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(2)  # Prohibited — no dock icon

        signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
        signal.signal(signal.SIGTERM, lambda *_: app.terminate_(None))

        app.run()
        self._hotkey.stop()
        log.info("Shut down.")
