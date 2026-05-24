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

# 3. (PasteHelper is no longer needed — AudioLog does Cmd+V via Quartz directly.
#    The standalone PasteHelper.app in the repo is kept as a historical artifact
#    but isn't bundled anymore. See output.py.)

# 4. Clean previous build
rm -rf build dist

# 5. Build .app bundle
echo "Running py2app…"
python setup.py py2app 2>&1 | tail -5

# 6. Sign with a stable local identity so the CDHash changes across rebuilds
#    but the cert identity stays the same — TCC attributes Accessibility
#    by cert identity, so the user's approval persists across rebuilds.
#    Identity name comes from $CODESIGN_IDENTITY or defaults to "AudioLog Dev Local".
IDENTITY="${CODESIGN_IDENTITY:-AudioLog Dev Local}"
if security find-identity -v -p codesigning 2>&1 | grep -q "\"$IDENTITY\""; then
    echo "Signing with \"$IDENTITY\"…"
    codesign --force --deep --sign "$IDENTITY" \
        --options runtime \
        "dist/AudioLog.app" 2>&1 | tail -3
    codesign -dv --verbose=2 "dist/AudioLog.app" 2>&1 \
        | grep -E "^(CDHash|Authority|TeamIdent)" | head -3
else
    echo "⚠  Identity \"$IDENTITY\" not found in keychain — staying adhoc."
    echo "   Accessibility approval will NOT persist across rebuilds."
    echo "   Run tools/create-dev-cert.sh to set up a stable signing identity."
fi

# 7. Verify
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

# 8. Optional: create DMG
if [ "${1:-}" = "dmg" ]; then
    DMG_NAME="AudioLog-1.2.1.dmg"
    echo "Creating ${DMG_NAME}…"
    rm -f "dist/$DMG_NAME"
    hdiutil create -volname "AudioLog" \
        -srcfolder "dist/AudioLog.app" \
        -ov -format UDZO \
        "dist/$DMG_NAME"
    echo "DMG: dist/$DMG_NAME ($(du -sh "dist/$DMG_NAME" | cut -f1))"
fi
