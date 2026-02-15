"""Sound feedback using macOS system sounds."""

import subprocess
import threading

from config import Config
from utils import get_logger

log = get_logger(__name__)


class Feedback:
    def __init__(self, config: Config) -> None:
        self._config = config

    def _play(self, path: str) -> None:
        def _worker() -> None:
            try:
                subprocess.run(
                    ["afplay", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log.warning("Failed to play sound %s: %s", path, e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def on_record_start(self) -> None:
        self._play(self._config.sound_start)

    def on_record_stop(self) -> None:
        self._play(self._config.sound_stop)

    def on_error(self) -> None:
        self._play(self._config.sound_error)
