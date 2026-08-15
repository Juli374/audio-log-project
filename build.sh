#!/usr/bin/env bash
# Build AudioLog.app, sign it, and (optionally) notarize it with Apple.
#
#   bash build.sh                 → build + sign with the best identity found
#   bash build.sh notarize        → also notarize + staple (needs a notary profile)
#   bash build.sh dmg             → also produce a DMG
#
# Identity selection, in order:
#   $CODESIGN_IDENTITY            → used as-is (name or SHA-1 hash)
#   Developer ID Application …    → real signature, works on other Macs
#   AudioLog Dev Local            → self-signed fallback, this Mac only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="$(tr -d '[:space:]' < VERSION)"
APP="dist/AudioLog.app"
ZIP="dist/AudioLog-${VERSION}.zip"
ENTITLEMENTS="$SCRIPT_DIR/entitlements.plist"
# Keychain profile holding the notarytool credentials. Defaults to the
# profile already stored on this Mac for the same SOFTSURE team — one
# Developer ID and one app-specific password cover every app of the team,
# so AudioLog does not need its own.
NOTARY_PROFILE="${NOTARY_PROFILE:-ads-admin-notary}"
TEAM_ID="BHZHRHKZY4"

WANT_NOTARIZE=0
WANT_DMG=0
for arg in "$@"; do
    case "$arg" in
        notarize) WANT_NOTARIZE=1 ;;
        dmg) WANT_DMG=1 ;;
        *) echo "Unknown argument: $arg (expected 'notarize' and/or 'dmg')"; exit 2 ;;
    esac
done

echo "=== Building AudioLog.app v${VERSION} ==="

# ── 1. venv + py2app ────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "ERROR: .venv not found. Run setup.sh first."
    exit 1
fi
source .venv/bin/activate
pip install py2app -q

rm -rf build dist
echo "Running py2app…"
python setup.py py2app 2>&1 | tail -5

if [ ! -d "$APP" ]; then
    echo "ERROR: py2app did not produce $APP"
    exit 1
fi

# ── 2. pick a signing identity ──────────────────────────────────────────────
IS_DEVELOPER_ID=0
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    IDENTITY="$CODESIGN_IDENTITY"
    if security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
        IS_DEVELOPER_ID=1
    fi
else
    # Match by SHA-1 hash: two certs can share the same display name, and
    # codesign refuses an ambiguous name.
    IDENTITY="$(security find-identity -v -p codesigning \
        | grep "Developer ID Application" \
        | head -1 | awk '{print $2}')"
    if [ -n "$IDENTITY" ]; then
        IS_DEVELOPER_ID=1
    else
        IDENTITY="AudioLog Dev Local"
    fi
fi

if [ "$IS_DEVELOPER_ID" = "1" ]; then
    echo "Signing identity: $IDENTITY (Developer ID)"
    SIGN_EXTRA=(--timestamp)
else
    echo "Signing identity: $IDENTITY (self-signed — this Mac only)"
    SIGN_EXTRA=()
    if [ "$WANT_NOTARIZE" = "1" ]; then
        echo "ERROR: notarization needs a Developer ID certificate."
        exit 1
    fi
fi

sign() {  # sign <path> [extra codesign args…]
    local target="$1"; shift
    codesign --force --options runtime "${SIGN_EXTRA[@]+"${SIGN_EXTRA[@]}"}" \
        --sign "$IDENTITY" "$@" "$target"
}

# ── 3. sign inside-out ──────────────────────────────────────────────────────
# Extended attributes left by the build make codesign fail.
xattr -cr "$APP"

echo "Signing nested binaries…"
COUNT=0
while IFS= read -r macho; do
    [ -n "$macho" ] || continue
    sign "$macho"
    COUNT=$((COUNT + 1))
done < <(
    find "$APP" -type f \( -name "*.so" -o -name "*.dylib" \) \
        -not -path "*/Contents/MacOS/*"
    if [ -d "$APP/Contents/Frameworks" ]; then
        find "$APP/Contents/Frameworks" -type f -perm -u+x \
            ! -name "*.so" ! -name "*.dylib" \
            | while read -r f; do
                file -b "$f" | grep -q '^Mach-O' && echo "$f"
              done
    fi
)
echo "  $COUNT nested binaries signed"

# Frameworks are bundles — seal them after their contents.
for fw in "$APP"/Contents/Frameworks/*.framework; do
    [ -d "$fw" ] || continue
    echo "Sealing $(basename "$fw")…"
    sign "$fw"
done

# The interpreter stubs in MacOS/ need the same entitlements as the app.
for exe in "$APP"/Contents/MacOS/*; do
    [ -f "$exe" ] || continue
    sign "$exe" --entitlements "$ENTITLEMENTS"
done

echo "Sealing app bundle…"
sign "$APP" --entitlements "$ENTITLEMENTS"

codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tail -2
codesign -dv --verbose=4 "$APP" 2>&1 \
    | grep -E "^(Authority|TeamIdentifier|CDHash)" | head -4

# ── 4. notarize + staple ────────────────────────────────────────────────────
if [ "$WANT_NOTARIZE" = "1" ]; then
    if ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" \
            >/dev/null 2>&1; then
        cat <<EOF

ERROR: no notary credentials stored under profile "$NOTARY_PROFILE".
Create them once (needs an app-specific password from appleid.apple.com):

  xcrun notarytool store-credentials "$NOTARY_PROFILE" \\
      --apple-id "<your-apple-id-email>" --team-id $TEAM_ID

EOF
        exit 1
    fi

    echo "Zipping for notarization…"
    rm -f "$ZIP"
    ditto -c -k --keepParent --sequesterRsrc "$APP" "$ZIP"

    echo "Submitting to Apple (this takes a few minutes)…"
    xcrun notarytool submit "$ZIP" \
        --keychain-profile "$NOTARY_PROFILE" --wait --timeout 30m

    echo "Stapling ticket…"
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"

    # Re-zip so the archive contains the stapled bundle.
    rm -f "$ZIP"
    ditto -c -k --keepParent --sequesterRsrc "$APP" "$ZIP"
    echo "Notarized archive: $ZIP"

    echo "Gatekeeper check:"
    spctl --assess --type execute --verbose=4 "$APP" 2>&1 | tail -2
fi

# ── 5. summary ──────────────────────────────────────────────────────────────
SIZE=$(du -sh "$APP" | cut -f1)
echo ""
echo "=== Build successful ==="
echo "App: $APP ($SIZE), version $VERSION"
[ -f "$ZIP" ] && echo "Zip: $ZIP ($(du -sh "$ZIP" | cut -f1))"
echo ""
echo "Install locally:  bash install.sh"
echo "Publish release:  bash release.sh <new-version>"

# ── 6. optional DMG ─────────────────────────────────────────────────────────
if [ "$WANT_DMG" = "1" ]; then
    DMG_NAME="AudioLog-${VERSION}.dmg"
    echo "Creating ${DMG_NAME}…"
    rm -f "dist/$DMG_NAME"
    hdiutil create -volname "AudioLog" \
        -srcfolder "$APP" \
        -ov -format UDZO \
        "dist/$DMG_NAME"
    if [ "$IS_DEVELOPER_ID" = "1" ]; then
        codesign --force --timestamp --sign "$IDENTITY" "dist/$DMG_NAME"
    fi
    echo "DMG: dist/$DMG_NAME ($(du -sh "dist/$DMG_NAME" | cut -f1))"
fi
