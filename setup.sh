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
echo "  1) Groq — Groq API (whisper-large-v3-turbo, fastest, needs gsk_... key)"
echo "  2) OpenAI — OpenAI API (gpt-4o-transcribe, needs sk-... key)"
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

# 3. Create initial settings
DATA_DIR="$HOME/Library/Application Support/audio-log"
SETTINGS_FILE="$DATA_DIR/settings.json"
mkdir -p "$DATA_DIR"
if [ "$MODE_CHOICE" = "1" ] && [ ! -f "$SETTINGS_FILE" ]; then
    read -rp "Groq API key (gsk_...): " API_KEY
    cat > "$SETTINGS_FILE" <<SETTINGS
{
  "transcription_mode": "groq",
  "groq_api_key": "$API_KEY",
  "groq_model": "whisper-large-v3-turbo",
  "language": "ru",
  "hotkey_mode": "toggle"
}
SETTINGS
    echo "Settings saved to $SETTINGS_FILE"
elif [ "$MODE_CHOICE" = "2" ] && [ ! -f "$SETTINGS_FILE" ]; then
    read -rp "OpenAI API key (sk-...): " API_KEY
    cat > "$SETTINGS_FILE" <<SETTINGS
{
  "transcription_mode": "api",
  "openai_api_key": "$API_KEY",
  "openai_model": "gpt-4o-transcribe",
  "language": "ru",
  "hotkey_mode": "toggle"
}
SETTINGS
    echo "Settings saved to $SETTINGS_FILE"
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
if [ "$MODE_CHOICE" = "2" ]; then
    echo "Mode: OpenAI API (gpt-4o-transcribe)."
else
    echo "Mode: Groq (whisper-large-v3-turbo)."
fi
echo "API key can be changed in: $SETTINGS_FILE"
echo ""
echo "Next step:  bash install.sh"
echo ""
echo "NOTE: Grant these permissions in System Settings → Privacy & Security:"
echo "  1. Accessibility (for hotkey + text insertion)"
echo "  2. Microphone (for audio recording)"
