# start.ps1
# Civic Media Processing Tool — Windows startup script
# Run this from E:\0-Automated-Apps\civic_media\
# Usage: .\start.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  CIVIC MEDIA — Starting up..." -ForegroundColor Cyan
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
Start-Process cmd -ArgumentList "/K", $watchdogPath

Start-Sleep -Seconds 2
Write-Host "        Celery worker started with auto-restart watchdog." -ForegroundColor Green

# ── 4. FastAPI server in this window ─────────────────────────────────────────

Write-Host "  [3/3] Starting FastAPI server..." -ForegroundColor White
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
