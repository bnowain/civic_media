#!/usr/bin/env bash
# celery-cmd.sh — Run Celery commands from WSL using the Windows venv.
#
# Only needed when running from WSL/Termux. On native Windows, use celery
# directly after activating the venv.
#
# Usage:
#   bash scripts/celery-cmd.sh inspect ping
#   bash scripts/celery-cmd.sh inspect active
#   bash scripts/celery-cmd.sh call app.tasks.process_video --args='["abc123"]'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CELERY_EXE="$SCRIPT_DIR/venv/Scripts/celery.exe"

if [[ ! -f "$CELERY_EXE" ]]; then
    echo "ERROR: celery.exe not found at $CELERY_EXE"
    echo "Make sure the Windows venv is set up."
    exit 1
fi

cd "$SCRIPT_DIR"
"$CELERY_EXE" -A app.worker.celery_app "$@"
