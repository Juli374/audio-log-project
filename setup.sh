#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Auto-detect Python 3.10+
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ not found. Install via: brew install python@3.12"
    exit 1
fi
echo "Using Python: $PYTHON ($($PYTHON --version))"

VENV_DIR="$SCRIPT_DIR/.venv"
MODEL_NAME="small"
MODEL_DIR="$SCRIPT_DIR/models"
MODEL_FILE="$MODEL_DIR/ggml-${MODEL_NAME}.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL_NAME}.bin"

echo "=== audio-log-project setup ==="

# 1. Create venv
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment…"
    "$PYTHON" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 2. Install dependencies
echo "Installing dependencies…"
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 3. Download model
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_FILE" ]; then
    echo "Downloading whisper model '${MODEL_NAME}' (~500 MB)…"
    curl -L -o "$MODEL_FILE" "$MODEL_URL"
else
    echo "Model already downloaded: $MODEL_FILE"
fi

# 4. Build PasteHelper.app (text injection helper)
PASTE_APP="$SCRIPT_DIR/PasteHelper.app/Contents/MacOS"
if [ ! -f "$PASTE_APP/PasteHelper" ]; then
    echo "Building PasteHelper.app…"
    mkdir -p "$PASTE_APP"
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
    swiftc "$SCRIPT_DIR/PasteHelper.swift" -o "$PASTE_APP/PasteHelper"
    echo "PasteHelper built successfully"
else
    echo "PasteHelper already built"
fi

echo ""
echo "=== Setup complete ==="
echo "To run:  source .venv/bin/activate && python run.py"
echo ""
echo "NOTE: Grant these permissions in System Settings → Privacy & Security:"
echo "  1. Accessibility → Terminal (for hotkey + paste simulation)"
echo "  2. Microphone → Terminal (for audio recording)"
