"""Logging setup."""

import logging
import sys
from pathlib import Path

from config import Config

LOG_DIR = Path.home() / "Library" / "Logs" / "audio-log"
LOG_FILE = LOG_DIR / "app.log"


def setup_logging(config: Config) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(config.log_format)

    # Always log to file
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Also log to stderr (visible in LaunchAgent stderr.log or terminal)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
