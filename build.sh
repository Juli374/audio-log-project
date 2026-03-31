#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Building AudioLog.app ==="

# 1. Ensure venv
if [ ! -d ".venv" ]; then
    echo "ERROR: .venv not found. Run setup.sh first."
    exit 1
fi
source .venv/bin/activate

# 2. Install py2app
pip install py2app -q

# 3. Ensure PasteHelper.app is built
PASTE_APP="$SCRIPT_DIR/PasteHelper.app/Contents/MacOS/PasteHelper"
if [ ! -f "$PASTE_APP" ]; then
    echo "Building PasteHelper.app…"
    mkdir -p "$SCRIPT_DIR/PasteHelper.app/Contents/MacOS"
    cat > "$SCRIPT_DIR/PasteHelper.app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>PasteHelper</string>
    <key>CFBundleIdentifier</key>
    <string>com.audiolog.paste-helper</string>
    <key>CFBundleName</key>
    <string>PasteHelper</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST
    swiftc "$SCRIPT_DIR/PasteHelper.swift" -o "$PASTE_APP"
    echo "PasteHelper built"
fi

# 4. Clean previous build
rm -rf build dist

# 5. Build .app bundle
echo "Running py2app…"
python setup.py py2app 2>&1 | tail -5

# 6. Verify
APP_PATH="dist/AudioLog.app"
if [ -d "$APP_PATH" ]; then
    SIZE=$(du -sh "$APP_PATH" | cut -f1)
    echo ""
    echo "=== Build successful ==="
    echo "App: $APP_PATH ($SIZE)"
    echo ""
    echo "To test:  open dist/AudioLog.app"
    echo "To create DMG:  bash build.sh dmg"
else
    echo "ERROR: Build failed"
    exit 1
fi

# 7. Optional: create DMG
if [ "${1:-}" = "dmg" ]; then
    DMG_NAME="AudioLog-1.1.0.dmg"
    echo "Creating $DMG_NAME…"
    rm -f "dist/$DMG_NAME"
    hdiutil create -volname "AudioLog" \
        -srcfolder "dist/AudioLog.app" \
        -ov -format UDZO \
        "dist/$DMG_NAME"
    echo "DMG: dist/$DMG_NAME ($(du -sh "dist/$DMG_NAME" | cut -f1))"
fi
