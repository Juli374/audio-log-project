#!/usr/bin/env bash
# One-time: create a self-signed cert for stable TCC (Accessibility) attribution.
# TCC binds Accessibility approvals to code-signing identity. With an adhoc
# signature (py2app's default), every rebuild produces a new CDHash and
# invalidates approvals. Signing with a stable local identity fixes this.
set -euo pipefail

NAME="${1:-AudioLog Dev Local}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "Creating self-signed code-signing cert \"$NAME\"…"

openssl req -new -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/dev.key" \
    -out "$WORK/dev.crt" \
    -days 3650 \
    -subj "/CN=$NAME" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "extendedKeyUsage=critical,codeSigning" \
    -addext "keyUsage=critical,digitalSignature" 2>&1 | tail -3

openssl pkcs12 -export \
    -out "$WORK/dev.p12" \
    -inkey "$WORK/dev.key" -in "$WORK/dev.crt" \
    -name "$NAME" \
    -passout pass:audiolog \
    -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1

security import "$WORK/dev.p12" \
    -k "$HOME/Library/Keychains/login.keychain-db" \
    -P audiolog \
    -T /usr/bin/codesign -A

echo "Trusting cert (admin password required)…"
security add-trusted-cert -d -r trustRoot \
    -k "$HOME/Library/Keychains/login.keychain-db" \
    "$WORK/dev.crt"

echo
echo "Done. Available code-signing identities:"
security find-identity -v -p codesigning
