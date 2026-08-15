#!/usr/bin/env bash
# Install AudioLog into /Applications and register it as a LaunchAgent.
#
# The app must live outside the git checkout: auto-update replaces the whole
# bundle, and it needs a stable path that does not depend on this repo.
#
#   bash install.sh          → install dist/AudioLog.app to /Applications
#   APP_DEST=~/Applications bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.audiolog.dictation"
PLIST_SRC="$SCRIPT_DIR/${PLIST_NAME}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/Library/Logs/audio-log"
APP_DEST="${APP_DEST:-/Applications}"
APP_PATH="$APP_DEST/AudioLog.app"
BUILT_APP="$SCRIPT_DIR/dist/AudioLog.app"

echo "=== Installing AudioLog ==="

# ── stop whatever is running now ────────────────────────────────────────────
launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null && \
    echo "Stopped running service." || true
pkill -f "AudioLog.app/Contents/MacOS/" 2>/dev/null && \
    echo "Stopped running app." || true
sleep 1

# ── figure out what to install ──────────────────────────────────────────────
if [ -d "$BUILT_APP" ]; then
    echo "Installing $BUILT_APP → $APP_PATH"
    rm -rf "$APP_PATH"
    ditto "$BUILT_APP" "$APP_PATH"
    xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

    EXEC_PATH="$APP_PATH/Contents/MacOS/AudioLog"
    [ -f "$EXEC_PATH" ] || EXEC_PATH="$APP_PATH/Contents/MacOS/audiolog"
    PROGRAM_ARGS="<string>${EXEC_PATH}</string>"
    WORK_DIR="$HOME"

    VERSION="$(defaults read "$APP_PATH/Contents/Info.plist" \
        CFBundleShortVersionString 2>/dev/null || echo "?")"
    echo "Installed version: $VERSION"
    codesign -dv --verbose=2 "$APP_PATH" 2>&1 \
        | grep -E "^(Authority|TeamIdentifier)" | head -2 || true
else
    VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    if [ ! -f "$VENV_PYTHON" ]; then
        echo "ERROR: no dist/AudioLog.app and no .venv."
        echo "       Run 'bash build.sh' (or 'bash setup.sh') first."
        exit 1
    fi
    echo "No build found — running from source via venv (no auto-update)."
    PROGRAM_ARGS="<string>${VENV_PYTHON}</string>
        <string>${SCRIPT_DIR}/run.py</string>"
    EXEC_PATH="$VENV_PYTHON"
    WORK_DIR="$SCRIPT_DIR"
fi

mkdir -p "$LOG_DIR"

# ── LaunchAgent ─────────────────────────────────────────────────────────────
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
    <string>${WORK_DIR}</string>

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

cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo ""
echo "=== Installed ==="
echo "App:  $EXEC_PATH"
echo "Logs: $LOG_DIR/"
echo "Auto-start on login: yes. Menu bar: look for the 🎙 icon."
echo ""
echo "IMPORTANT — macOS ties permissions to the signature and path, so after"
echo "this first install you must approve the app once:"
echo "  System Settings → Privacy & Security → Accessibility  → allow AudioLog"
echo "  System Settings → Privacy & Security → Microphone     → allow AudioLog"
echo "Signed updates keep those approvals — you only do this once."
echo ""
echo "To uninstall:  bash uninstall.sh"
