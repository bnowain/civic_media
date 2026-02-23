# start.ps1
# Civic Media Processing Tool — Windows startup script
# Run this from E:\0-Automated-Apps\civic_media\
#
# Usage:
#   .\start.ps1            — Hidden mode (default): no windows, browser auto-opens
#   .\start.ps1 -Visible   — Visible mode: CMD window + foreground uvicorn (debug)

param(
    [switch]$Visible
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  CIVIC MEDIA — Starting up..." -ForegroundColor Cyan
if (-not $Visible) {
    Write-Host "  (Hidden mode — use -Visible for debug windows)" -ForegroundColor DarkGray
}
Write-Host ""

# ── 1. Redis via Docker ───────────────────────────────────────────────────────

Write-Host "  [1/3] Starting Redis..." -ForegroundColor White
Set-Location $Root
docker compose up -d redis | Out-Null
Write-Host "        Redis ready." -ForegroundColor Green

# ── 2. Load HF token from user environment ────────────────────────────────────

$hfToken = [Environment]::GetEnvironmentVariable("HF_TOKEN", "User")
if (-not $hfToken) {
    Write-Host ""
    Write-Host "  WARNING: HF_TOKEN not found in user environment variables." -ForegroundColor Yellow
    Write-Host "  Diarization will fail without it. Set it with:" -ForegroundColor Yellow
    Write-Host '  [Environment]::SetEnvironmentVariable("HF_TOKEN", "hf_...", "User")' -ForegroundColor Yellow
    Write-Host ""
}

# ── 3. Celery worker with auto-restart watchdog ─────────────────────────────

Write-Host "  [2/3] Starting Celery worker (with watchdog)..." -ForegroundColor White

$watchdogPath = "$Root\.celery_watchdog.cmd"
$watchdogContent = @"
@echo off
title Civic Media - Celery Worker (watchdog)
:restart
cd /d "$Root"
call "$Root\venv\Scripts\activate.bat"
set PYTHONPATH=.
set HF_TOKEN=$hfToken
echo [%date% %time%] Starting Celery worker...
celery -A app.worker.celery_app worker --loglevel=info --concurrency=1 --pool=solo
echo.
echo [%date% %time%] Worker exited. Restarting in 5 seconds... (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto restart
"@

[System.IO.File]::WriteAllText($watchdogPath, $watchdogContent)

if ($Visible) {
    # ── Visible mode: open CMD window like before ──
    Start-Process cmd -ArgumentList "/K", $watchdogPath
} else {
    # ── Hidden mode: launch watchdog with no visible window ──
    $watchdogProc = Start-Process cmd -ArgumentList "/C", $watchdogPath `
        -WindowStyle Hidden -PassThru
}

Start-Sleep -Seconds 2
Write-Host "        Celery worker started with auto-restart watchdog." -ForegroundColor Green

# ── 4. FastAPI server ──────────────────────────────────────────────────────────

Write-Host "  [3/3] Starting FastAPI server..." -ForegroundColor White

if ($Visible) {
    # ── Visible mode: run uvicorn in this window (original behavior) ──
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────┐" -ForegroundColor Cyan
    Write-Host "  │   Open: http://localhost:8000            │" -ForegroundColor Cyan
    Write-Host "  │   API docs: http://localhost:8000/api/docs │" -ForegroundColor Cyan
    Write-Host "  │   Press Ctrl+C to stop the server        │" -ForegroundColor Cyan
    Write-Host "  └─────────────────────────────────────────┘" -ForegroundColor Cyan
    Write-Host ""

    $env:PYTHONPATH = "."
    $env:HF_TOKEN   = $hfToken

    & "$Root\venv\Scripts\activate.ps1"
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

} else {
    # ── Hidden mode: launch uvicorn hidden, save PIDs, open browser ──
    $uvicornPath = "$Root\.uvicorn_start.cmd"
    $uvicornContent = @"
@echo off
title Civic Media - API Server
cd /d "$Root"
call "$Root\venv\Scripts\activate.bat"
set PYTHONPATH=.
set HF_TOKEN=$hfToken
uvicorn app.main:app --host 0.0.0.0 --port 8000
"@
    [System.IO.File]::WriteAllText($uvicornPath, $uvicornContent)

    $uvicornProc = Start-Process cmd -ArgumentList "/C", $uvicornPath `
        -WindowStyle Hidden -PassThru

    # Save PIDs so the web UI shutdown button can kill them
    $pidFile = "$Root\.server.pids"
    $pids = @{
        watchdog = $watchdogProc.Id
        uvicorn  = $uvicornProc.Id
    } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($pidFile, $pids)

    Write-Host "        API server started (hidden)." -ForegroundColor Green
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────┐" -ForegroundColor Cyan
    Write-Host "  │   Server running at http://localhost:8000│" -ForegroundColor Cyan
    Write-Host "  │   Use the UI shutdown button to stop    │" -ForegroundColor Cyan
    Write-Host "  │   PIDs saved to .server.pids            │" -ForegroundColor Cyan
    Write-Host "  └─────────────────────────────────────────┘" -ForegroundColor Cyan
    Write-Host ""

    # Wait a moment for uvicorn to start, then open browser
    Start-Sleep -Seconds 3
    Start-Process "http://localhost:8000"

    Write-Host "  Browser opened. This window will close." -ForegroundColor Green
    Write-Host ""
}
