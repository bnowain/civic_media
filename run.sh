#!/usr/bin/env bash
# run.sh — Start the Civic Media Processing Tool
#
# Prerequisites (install once):
#   - ffmpeg and ffprobe:      sudo apt install ffmpeg
#   - Tesseract OCR:           sudo apt install tesseract-ocr
#   - Poppler (for pdf2image): sudo apt install poppler-utils
#   - Docker (for Redis):      https://docs.docker.com/get-docker/
#   - Python 3.11+:            https://www.python.org/
#
# First run:
#   pip install -r requirements.txt
#   (For CUDA: pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121)
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

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# ── Preflight checks ──────────────────────────────────────────────────────────

echo "▶ Checking prerequisites..."

if ! command -v ffmpeg &>/dev/null; then
  echo "✗ ffmpeg not found. Install: sudo apt install ffmpeg"
  exit 1
fi

if ! command -v tesseract &>/dev/null; then
  echo "⚠  tesseract not found — OCR fallback will be unavailable."
  echo "   Install: sudo apt install tesseract-ocr"
fi

if [[ -z "$HF_TOKEN" ]]; then
  echo "⚠  HF_TOKEN not set — pyannote diarization will fail."
  echo "   Export your HuggingFace token: export HF_TOKEN=hf_..."
fi

echo "✓ Prerequisites OK"

# ── Start Redis ───────────────────────────────────────────────────────────────

if command -v docker &>/dev/null && command -v docker-compose &>/dev/null; then
  echo "▶ Starting Redis..."
  docker compose up -d redis
  sleep 1
elif command -v redis-server &>/dev/null; then
  if ! redis-cli ping &>/dev/null 2>&1; then
    echo "▶ Starting local Redis..."
    redis-server --daemonize yes --logfile /tmp/civic-redis.log
    sleep 1
  fi
else
  echo "⚠  Redis not found. Install Docker or redis-server."
  echo "   The pipeline tasks will not run without Redis."
fi

# ── Start Celery worker with auto-restart watchdog ────────────────────────────

CELERY_WATCHDOG_PID=""

celery_watchdog() {
  while true; do
    echo "[$(date)] Starting Celery worker..."
    celery -A app.worker.celery_app worker \
      --loglevel=info \
      --concurrency=1 \
      --queues=celery \
      --logfile=celery.log \
      --pidfile=celery.pid || true
    echo "[$(date)] Worker exited. Restarting in 5 seconds..."
    sleep 5
  done
}

cleanup() {
  echo ""
  echo "▶ Shutting down..."
  if [[ -n "$CELERY_WATCHDOG_PID" ]]; then
    kill "$CELERY_WATCHDOG_PID" 2>/dev/null || true
  fi
  if [[ -f celery.pid ]]; then
    kill "$(cat celery.pid)" 2>/dev/null || true
    rm -f celery.pid
  fi
  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

echo "▶ Starting Celery worker (with auto-restart watchdog)..."
celery_watchdog &
CELERY_WATCHDOG_PID=$!
sleep 2
echo "   Celery worker started (log: celery.log, watchdog PID: $CELERY_WATCHDOG_PID)"

# ── Start FastAPI ──────────────────────────────────────────────────────────────

echo "▶ Starting FastAPI server at http://${HOST}:${PORT}"
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
