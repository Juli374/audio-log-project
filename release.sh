#!/usr/bin/env bash
# Publish a new AudioLog version so installed copies auto-update to it.
#
#   bash release.sh 1.3.0
#   bash release.sh 1.3.0 --dry-run     → build + notarize, publish nothing
#
# What it does: bump VERSION → build + sign + notarize → zip → write the
# appcast.json feed the app polls → commit, tag, push → upload both files to
# Cloudflare R2, which is what installed copies poll.
#
# The app reads:
#   https://pub-7c882e4faf9c4890a62908cca4ec2aff.r2.dev/appcast.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REPO="Juli374/audio-log-project"
R2_BUCKET="audiolog-releases"
R2_ORIGIN="https://pub-7c882e4faf9c4890a62908cca4ec2aff.r2.dev"
NEW_VERSION="${1:-}"
DRY_RUN=0
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1

if [ -z "$NEW_VERSION" ]; then
    echo "Usage: bash release.sh <version> [--dry-run]"
    echo "Current version: $(tr -d '[:space:]' < VERSION)"
    exit 2
fi
NEW_VERSION="${NEW_VERSION#v}"
if ! printf '%s' "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: version must look like 1.3.0, got '$NEW_VERSION'"
    exit 2
fi

OLD_VERSION="$(tr -d '[:space:]' < VERSION)"
if [ "$NEW_VERSION" = "$OLD_VERSION" ]; then
    echo "ERROR: $NEW_VERSION is already the current version."
    exit 2
fi
# The updater only ever moves forward — refuse a lower number.
LOWEST="$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | head -1)"
if [ "$LOWEST" = "$NEW_VERSION" ]; then
    echo "ERROR: $NEW_VERSION is older than the current $OLD_VERSION."
    exit 2
fi

# ── preflight ───────────────────────────────────────────────────────────────
for tool in npx xcrun ditto curl; do
    command -v "$tool" >/dev/null || { echo "ERROR: $tool not found"; exit 1; }
done
npx --yes wrangler@4 whoami >/dev/null 2>&1 || {
    echo "ERROR: wrangler is not logged in — run 'npx wrangler login'"; exit 1; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: uncommitted changes — commit them first:"
    git status --short --untracked-files=no
    exit 1
fi

if git rev-parse "v$NEW_VERSION" >/dev/null 2>&1; then
    echo "ERROR: tag v$NEW_VERSION already exists."
    exit 1
fi

echo "=== Releasing $OLD_VERSION → $NEW_VERSION ==="

# ── bump + build ────────────────────────────────────────────────────────────
printf '%s\n' "$NEW_VERSION" > VERSION
trap 'printf "%s\n" "$OLD_VERSION" > VERSION; echo "Rolled VERSION back to $OLD_VERSION"' ERR

bash build.sh notarize

ZIP="dist/AudioLog-${NEW_VERSION}.zip"
[ -f "$ZIP" ] || { echo "ERROR: $ZIP missing after build"; exit 1; }

SHA256="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
SIZE="$(stat -f%z "$ZIP")"
DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
URL="${R2_ORIGIN}/AudioLog-${NEW_VERSION}.zip"

# ── update feed ─────────────────────────────────────────────────────────────
NOTES_FILE="dist/release-notes.md"
if grep -q "^## \[${NEW_VERSION}\]" CHANGELOG.md 2>/dev/null; then
    awk -v v="$NEW_VERSION" '
        $0 ~ "^## \\[" v "\\]" {found=1; next}
        found && /^## \[/ {exit}
        found {print}
    ' CHANGELOG.md > "$NOTES_FILE"
    echo "Release notes taken from CHANGELOG.md"
else
    printf 'AudioLog %s\n\nSigned with Developer ID and notarized by Apple.\nInstalled copies update themselves — no reinstall needed.\n' \
        "$NEW_VERSION" > "$NOTES_FILE"
    echo "No CHANGELOG entry for $NEW_VERSION — using generic notes."
fi

cat > dist/appcast.json <<EOF
{
  "version": "${NEW_VERSION}",
  "url": "${URL}",
  "sha256": "${SHA256}",
  "size": ${SIZE},
  "published_at": "${DATE}",
  "min_os": "13.0",
  "notes_url": "https://github.com/${REPO}/releases/tag/v${NEW_VERSION}"
}
EOF
echo "Feed:"
cat dist/appcast.json

if [ "$DRY_RUN" = "1" ]; then
    trap - ERR
    echo ""
    echo "=== Dry run — nothing published ==="
    echo "VERSION left at $NEW_VERSION, artifacts in dist/."
    exit 0
fi

# ── publish ─────────────────────────────────────────────────────────────────
trap - ERR

git add VERSION CHANGELOG.md 2>/dev/null || git add VERSION
git commit -m "v${NEW_VERSION}" >/dev/null
git tag "v${NEW_VERSION}"
git push origin HEAD
git push origin "v${NEW_VERSION}"

# Order matters: the zip goes up first, the feed last. Publishing the feed
# before its download exists would send every installed copy at a 404.
# --remote is mandatory — without it wrangler writes to a local simulator
# instead of the real bucket, and the release silently never ships.
echo "Uploading to R2 bucket ${R2_BUCKET}…"
npx --yes wrangler@4 r2 object put \
    "${R2_BUCKET}/AudioLog-${NEW_VERSION}.zip" --file "$ZIP" --remote
npx --yes wrangler@4 r2 object put \
    "${R2_BUCKET}/appcast.json" --file dist/appcast.json \
    --content-type application/json --remote

echo ""
echo "=== Published v${NEW_VERSION} ==="
echo "Verifying the update feed…"
sleep 3
curl -sL "${R2_ORIGIN}/appcast.json" | head -12
echo ""
echo "Installed copies pick this up within ~4 hours, or immediately via"
echo "the menu → «Проверить обновления»."
