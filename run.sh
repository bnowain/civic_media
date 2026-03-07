#!/usr/bin/env bash
# run.sh — Start the Civic Media Processing Tool
#
# Prerequisites (install once):
#   - ffmpeg and ffprobe:      sudo apt install ffmpeg
#   - Tesseract OCR:           sudo apt install tesseract-ocr
#   - Poppler (for pdf2image): sudo apt install poppler-utils
#   - Python 3.11+:            https://www.python.org/
#   - pip install huey
#
# No Redis required — Huey uses SQLite for task queuing.
#
# HuggingFace token (required for pyannote):
#   1. Create account at https://huggingface.co
#   2. Accept licence at https://hf.co/pyannote/speaker-diarization-3.1
#   3. Create token at https://hf.co/settings/tokens
#   4. Export: export HF_TOKEN=hf_your_token_here

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONPATH="$SCRIPT_DIR"
export HF_TOKEN="${HF_TOKEN:-}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"
export WHISPER_DEVICE="${WHISPER_DEVICE:-cuda}"
export WHISPER_COMPUTE="${WHISPER_COMPUTE:-float16}"
export CIVIC_MEDIA_STORAGE_ROOT="${CIVIC_MEDIA_STORAGE_ROOT:-}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# ── Preflight checks ──────────────────────────────────────────────────────────

echo "Checking prerequisites..."

if ! command -v ffmpeg &>/dev/null; then
  echo "ffmpeg not found. Install: sudo apt install ffmpeg"
  exit 1
fi

if ! command -v tesseract &>/dev/null; then
  echo "Warning: tesseract not found — OCR fallback will be unavailable."
  echo "   Install: sudo apt install tesseract-ocr"
fi

if [[ -z "$HF_TOKEN" ]]; then
  echo "Warning: HF_TOKEN not set — pyannote diarization will fail."
  echo "   Export your HuggingFace token: export HF_TOKEN=hf_..."
fi

echo "Prerequisites OK"

# ── Start Huey GPU worker with auto-restart watchdog ─────────────────────────

GPU_WATCHDOG_PID=""
LIGHT_WATCHDOG_PID=""

gpu_watchdog() {
  while true; do
    echo "[$(date)] Starting Huey GPU worker..."
    python -m huey.bin.huey_consumer app.worker.huey \
      -w 1 -k thread -C \
      --logfile=logs/huey.log || true
    echo "[$(date)] GPU worker exited. Restarting in 5 seconds..."
    sleep 5
  done
}

light_watchdog() {
  while true; do
    echo "[$(date)] Starting Huey light worker..."
    python -m huey.bin.huey_consumer app.worker.huey_light \
      -w 1 -k thread -C \
      --logfile=logs/huey_light.log || true
    echo "[$(date)] Light worker exited. Restarting in 5 seconds..."
    sleep 5
  done
}

cleanup() {
  echo ""
  echo "Shutting down..."
  [[ -n "$GPU_WATCHDOG_PID" ]] && kill "$GPU_WATCHDOG_PID" 2>/dev/null || true
  [[ -n "$LIGHT_WATCHDOG_PID" ]] && kill "$LIGHT_WATCHDOG_PID" 2>/dev/null || true
  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

mkdir -p logs

echo "Starting Huey GPU worker (with auto-restart watchdog)..."
gpu_watchdog &
GPU_WATCHDOG_PID=$!
sleep 1

echo "Starting Huey light worker (with auto-restart watchdog)..."
light_watchdog &
LIGHT_WATCHDOG_PID=$!
sleep 1

echo "   GPU worker started (log: logs/huey.log)"
echo "   Light worker started (log: logs/huey_light.log)"

# ── Start FastAPI ──────────────────────────────────────────────────────────────

echo "Starting FastAPI server at http://${HOST}:${PORT}"
echo ""
echo "  Open your browser: http://localhost:${PORT}"
echo ""
echo "  Press Ctrl+C to stop everything."
echo ""

uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --reload \
  --log-level info
