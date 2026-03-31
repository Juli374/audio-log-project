#!/usr/bin/env python3
"""Entry point for audio-log-project."""

import fcntl
import sys
from pathlib import Path

from config import Config

_lock_file = None


def _acquire_lock() -> bool:
    """Ensure only one instance is running. Returns True if lock acquired."""
    global _lock_file
    lock_path = Path.home() / "Library" / "Application Support" / "audio-log" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _check_dependencies() -> None:
    """Verify critical imports before heavy initialization."""
    try:
        import rumps  # noqa: F401
        import sounddevice  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        sys.exit(0)  # exit 0 so launchd does NOT restart


def main() -> None:
    _check_dependencies()

    if not _acquire_lock():
        print("AudioLog is already running", file=sys.stderr)
        sys.exit(0)

    config = Config()

    if "--no-menubar" in sys.argv:
        from app import App
        App(config).run()
    else:
        from menubar import MenuBarApp
        MenuBarApp(config).run()


if __name__ == "__main__":
    main()
