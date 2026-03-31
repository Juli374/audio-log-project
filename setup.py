"""py2app build configuration for AudioLog."""

from setuptools import setup

APP = ['run.py']

OPTIONS = {
    'argv_emulation': False,
    'packages': [
        'rumps',
        'sounddevice',
        'numpy',
        'pywhispercpp',
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
    ],
    'plist': {
        'CFBundleIdentifier': 'com.audiolog.app',
        'CFBundleName': 'AudioLog',
        'CFBundleDisplayName': 'AudioLog',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1.0',
        'LSUIElement': False,  # Show in Dock (app sets policy at runtime)
        'NSMicrophoneUsageDescription': 'AudioLog needs microphone access for voice dictation.',
        'NSAppleEventsUsageDescription': 'AudioLog needs accessibility for text insertion.',
    },
    'iconfile': 'assets/AppIcon.icns',
    'resources': [
        'assets',
        'ui',
        'PasteHelper.app',
    ],
}

setup(
    name='AudioLog',
    version='1.1.0',
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
