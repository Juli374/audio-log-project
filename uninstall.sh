#!/usr/bin/env bash
set -euo pipefail

PLIST_NAME="com.audiolog.dictation"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo "=== Uninstalling audio-log LaunchAgent ==="

launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null && \
    echo "Service stopped." || echo "Service was not running."

if [ -f "$PLIST_DST" ]; then
    rm "$PLIST_DST"
    echo "Plist removed."
fi

echo "Done. Dictation will no longer auto-start."
echo "To re-install: bash install.sh"
