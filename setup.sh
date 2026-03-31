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
echo "=== audio-log-project setup ==="
echo ""
echo "Transcription mode:"
echo "  1) API — OpenAI API (no local model, needs API key, fast)"
echo "  2) Local — Whisper model (~500 MB download, works offline)"
echo ""
read -rp "Choose [1/2, default=1]: " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-1}"

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

# 3. Download model (only for local mode)
if [ "$MODE_CHOICE" = "2" ]; then
    MODEL_NAME="small"
    MODEL_DIR="$SCRIPT_DIR/models"
    MODEL_FILE="$MODEL_DIR/ggml-${MODEL_NAME}.bin"
    MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL_NAME}.bin"
    mkdir -p "$MODEL_DIR"
    if [ ! -f "$MODEL_FILE" ]; then
        echo "Downloading whisper model '${MODEL_NAME}' (~500 MB)…"
        curl -L -o "$MODEL_FILE" "$MODEL_URL"
    else
        echo "Model already downloaded: $MODEL_FILE"
    fi
else
    echo "API mode selected — skipping model download"
fi

# 3b. Create initial settings
DATA_DIR="$HOME/Library/Application Support/audio-log"
SETTINGS_FILE="$DATA_DIR/settings.json"
mkdir -p "$DATA_DIR"
if [ "$MODE_CHOICE" = "1" ] && [ ! -f "$SETTINGS_FILE" ]; then
    read -rp "OpenAI API key (sk-...): " API_KEY
    cat > "$SETTINGS_FILE" <<SETTINGS
{
  "transcription_mode": "api",
  "openai_api_key": "$API_KEY",
  "openai_model": "gpt-4o-mini-transcribe",
  "language": "ru",
  "hotkey_mode": "hold"
}
SETTINGS
    echo "Settings saved to $SETTINGS_FILE"
elif [ "$MODE_CHOICE" = "2" ] && [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" <<SETTINGS
{
  "transcription_mode": "local",
  "language": "ru",
  "hotkey_mode": "hold"
}
SETTINGS
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
echo ""
if [ "$MODE_CHOICE" = "1" ]; then
    echo "Mode: API (OpenAI). No local model."
    echo "API key can be changed in: $SETTINGS_FILE"
else
    echo "Mode: Local (Whisper). Model: models/ggml-small.bin"
fi
echo ""
echo "Next step:  bash install.sh"
echo ""
echo "NOTE: Grant these permissions in System Settings → Privacy & Security:"
echo "  1. Accessibility (for hotkey + text insertion)"
echo "  2. Microphone (for audio recording)"
