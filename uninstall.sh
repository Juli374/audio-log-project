#!/usr/bin/env bash
# Stop AudioLog and remove it from the Mac.
#
#   bash uninstall.sh          → stop + unregister, keep /Applications/AudioLog.app
#   bash uninstall.sh --app    → also delete the installed app bundle
#
# Settings and history in ~/Library/Application Support/audio-log are always
# kept — delete that folder by hand if you want a clean slate.
set -euo pipefail

PLIST_NAME="com.audiolog.dictation"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
REMOVE_APP=0
[ "${1:-}" = "--app" ] && REMOVE_APP=1

echo "=== Uninstalling AudioLog ==="

launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null && \
    echo "Service stopped." || echo "Service was not running."

pkill -f "AudioLog.app/Contents/MacOS/" 2>/dev/null && \
    echo "App process stopped." || true

if [ -f "$PLIST_DST" ]; then
    rm "$PLIST_DST"
    echo "LaunchAgent removed."
fi

if [ "$REMOVE_APP" = "1" ]; then
    for candidate in "/Applications/AudioLog.app" "$HOME/Applications/AudioLog.app"; do
        if [ -d "$candidate" ]; then
            rm -rf "$candidate"
            echo "Deleted $candidate"
        fi
    done
    rm -rf "$HOME/Library/Application Support/audio-log/updates"
fi

echo "Done. Settings and history were kept."
echo "To re-install: bash install.sh"
