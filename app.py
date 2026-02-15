"""Application orchestrator (headless mode, no menu bar)."""

import signal
import threading

from AppKit import NSApplication

from config import Config
from feedback import Feedback
from hotkey import HotkeyListener
from output import paste_text
from recorder import Recorder
from transcriber import Transcriber
from utils import get_logger, setup_logging

log = get_logger(__name__)


class App:
    def __init__(self, config: Config | None = None) -> None:
        self._config = config or Config()
        setup_logging(self._config)

        self._feedback = Feedback(self._config)
        self._recorder = Recorder(self._config)
        self._transcriber = Transcriber(self._config)
        self._transcription_lock = threading.Lock()

        self._hotkey = HotkeyListener(
            config=self._config,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )

    def _on_activate(self) -> None:
        self._feedback.on_record_start()
        self._recorder.start()

    def _on_deactivate(self) -> None:
        audio = self._recorder.stop()
        self._feedback.on_record_stop()

        if len(audio) == 0:
            log.warning("No audio captured")
            return

        t = threading.Thread(target=self._process, args=(audio,), daemon=True)
        t.start()

    def _process(self, audio) -> None:
        with self._transcription_lock:
            try:
                text = self._transcriber.transcribe(audio)
                if text:
                    paste_text(text)
            except Exception:
                log.exception("Transcription failed")
                self._feedback.on_error()

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
