#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="/opt/homebrew/bin/python3.12"
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

echo ""
echo "=== Setup complete ==="
echo "To run:  source .venv/bin/activate && python run.py"
echo ""
echo "NOTE: Grant these permissions in System Settings → Privacy & Security:"
echo "  1. Accessibility → Terminal (for hotkey + paste simulation)"
echo "  2. Microphone → Terminal (for audio recording)"
