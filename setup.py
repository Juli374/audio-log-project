"""py2app build configuration for AudioLog."""

from pathlib import Path

from setuptools import setup

# VERSION is the single source of truth — Info.plist and the runtime
# version check (version.py) both read from it.
VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

APP = ['run.py']

OPTIONS = {
    'argv_emulation': False,
    'packages': [
        'rumps',
        'sounddevice',
        '_sounddevice_data',
        'numpy',
        'objc',
        'AppKit',
        'Foundation',
        'Quartz',
        'WebKit',
    ],
    'includes': [
        'config',
        'menubar',
        'app',
        'recorder',
        'transcriber',
        'hotkey',
        'output',
        'overlay',
        'history_ui',
        'db',
        'feedback',
        'utils',
        'version',
        'updater',
    ],
    'plist': {
        'CFBundleIdentifier': 'com.audiolog.app',
        'CFBundleName': 'AudioLog',
        'CFBundleDisplayName': 'AudioLog',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'LSUIElement': False,  # Show in Dock (app sets policy at runtime)
        'LSMinimumSystemVersion': '13.0',
        'NSMicrophoneUsageDescription': 'AudioLog needs microphone access for voice dictation.',
        'NSAppleEventsUsageDescription': 'AudioLog needs accessibility for text insertion.',
        # A bundle launched by launchd inherits no locale, so Python falls
        # back to ASCII for any open() without an explicit encoding — which
        # is how a single em dash in the update helper silently disabled
        # auto-update. Force UTF-8 so the whole class of bug cannot recur.
        'LSEnvironment': {'PYTHONUTF8': '1'},
    },
    'iconfile': 'assets/AppIcon.icns',
    'resources': [
        'assets',
        'ui',
    ],
}

setup(
    name='AudioLog',
    version=VERSION,
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
