#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.audiolog.dictation"
PLIST_SRC="$SCRIPT_DIR/${PLIST_NAME}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/Library/Logs/audio-log"

echo "=== Installing audio-log as LaunchAgent ==="

# Determine executable: prefer AudioLog.app, fallback to venv python
APP_EXEC="$SCRIPT_DIR/AudioLog.app/Contents/MacOS/audiolog"
if [ -f "$APP_EXEC" ]; then
    PROGRAM_ARGS="<string>${APP_EXEC}</string>"
    echo "Using AudioLog.app bundle"
else
    VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    if [ ! -f "$VENV_PYTHON" ]; then
        echo "ERROR: Neither AudioLog.app nor .venv found. Run setup.sh first."
        exit 1
    fi
    PROGRAM_ARGS="<string>${VENV_PYTHON}</string>
        <string>${SCRIPT_DIR}/run.py</string>"
    APP_EXEC="$VENV_PYTHON"
    echo "Using venv python (AudioLog.app not found)"
fi

# Check venv exists
if [ ! -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "ERROR: venv not found. Run setup.sh first."
    exit 1
fi

# Create log directory
mkdir -p "$LOG_DIR"

# Generate plist
cat > "$PLIST_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        ${PROGRAM_ARGS}
    </array>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>

    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
EOF

# Unload if already loaded
launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null || true

# Copy and load
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo ""
echo "=== Installed ==="
echo "Dictation is now running and will auto-start on login."
echo "Menu bar: look for 🎙 icon"
echo "Logs: $LOG_DIR/"
echo ""
echo "To uninstall:  bash uninstall.sh"
echo ""
echo "IMPORTANT (one-time setup):"
echo "  System Settings → Privacy & Security → Accessibility"
echo "    → add: $APP_EXEC"
echo "  System Settings → Privacy & Security → Microphone"
echo "    → allow for the same app"
