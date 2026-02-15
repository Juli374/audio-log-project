#!/usr/bin/env python3
"""Entry point for audio-log-project."""

import sys

from config import Config


def main() -> None:
    config = Config()

    if "--no-menubar" in sys.argv:
        from app import App
        App(config).run()
    else:
        from menubar import MenuBarApp
        MenuBarApp(config).run()


if __name__ == "__main__":
    main()
