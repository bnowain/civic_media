# start.ps1
# Civic Media Processing Tool - Windows startup script
# Run this from E:\0-Automated-Apps\civic_media\
#
# Usage:
#   .\start.ps1            - Hidden mode (default): no windows, browser auto-opens
#   .\start.ps1 -Visible   - Visible mode: CMD windows + foreground uvicorn (debug)

param(
    [switch]$Visible
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  CIVIC MEDIA - Starting up..." -ForegroundColor Cyan
if (-not $Visible) {
    Write-Host "  (Hidden mode - use -Visible for debug windows)" -ForegroundColor DarkGray
}
Write-Host ""

# -- 1. Load HF token from user environment --

$hfToken = [Environment]::GetEnvironmentVariable("HF_TOKEN", "User")
if (-not $hfToken) {
    Write-Host ""
    Write-Host "  WARNING: HF_TOKEN not found in user environment variables." -ForegroundColor Yellow
    Write-Host "  Diarization will fail without it. Set it with:" -ForegroundColor Yellow
    Write-Host '  [Environment]::SetEnvironmentVariable("HF_TOKEN", "hf_...", "User")' -ForegroundColor Yellow
    Write-Host ""
}

# -- 2. Huey GPU worker (transcription, diarization, embedding) --

Write-Host "  [1/4] Starting Huey GPU worker..." -ForegroundColor White

$gpuWatchdogPath = "$Root\.gpu_worker.cmd"
$gpuWatchdogContent = @"
@echo off
title Civic Media - GPU Worker (huey)
if not exist "$Root\logs" mkdir "$Root\logs"
:restart
cd /d "$Root"
call "$Root\venv\Scripts\activate.bat"
set PYTHONPATH=.
set HF_TOKEN=$hfToken
echo [%date% %time%] Starting Huey GPU worker... >> "$Root\logs\huey.log" 2>&1
python -m huey.bin.huey_consumer app.worker.huey -w 1 -k thread -C --logfile "$Root\logs\huey.log"
echo [%date% %time%] GPU worker exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto restart
"@

[System.IO.File]::WriteAllText($gpuWatchdogPath, $gpuWatchdogContent)

if ($Visible) {
    Start-Process cmd -ArgumentList "/K", $gpuWatchdogPath
} else {
    $gpuProc = Start-Process cmd -ArgumentList @("/C", $gpuWatchdogPath) -WindowStyle Hidden -PassThru
}

Start-Sleep -Seconds 2
Write-Host "        GPU worker started (log: logs/huey.log)" -ForegroundColor Green

# -- 3. Huey light worker (download, transcode, ingest, clips) --

Write-Host "  [2/4] Starting Huey light worker..." -ForegroundColor White

$lightWatchdogPath = "$Root\.light_worker.cmd"
$lightWatchdogContent = @"
@echo off
title Civic Media - Light Worker (huey_light)
if not exist "$Root\logs" mkdir "$Root\logs"
:restart
cd /d "$Root"
call "$Root\venv\Scripts\activate.bat"
set PYTHONPATH=.
set HF_TOKEN=$hfToken
echo [%date% %time%] Starting Huey light worker... >> "$Root\logs\huey_light.log" 2>&1
python -m huey.bin.huey_consumer app.worker.huey_light -w 1 -k thread -C --logfile "$Root\logs\huey_light.log"
echo [%date% %time%] Light worker exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto restart
"@

[System.IO.File]::WriteAllText($lightWatchdogPath, $lightWatchdogContent)

if ($Visible) {
    Start-Process cmd -ArgumentList "/K", $lightWatchdogPath
} else {
    $lightProc = Start-Process cmd -ArgumentList @("/C", $lightWatchdogPath) -WindowStyle Hidden -PassThru
}

Start-Sleep -Seconds 2
Write-Host "        Light worker started (log: logs/huey_light.log)" -ForegroundColor Green

# -- 3b. Free port 8000 if something is already holding it --

Write-Host "  [3/4] Checking port 8000..." -ForegroundColor White

$stalePID = (netstat -ano 2>$null | Select-String ":8000\s" |
    Where-Object { $_ -match "LISTENING" } |
    ForEach-Object { ($_.ToString().Trim() -split "\s+")[-1] } |
    Select-Object -First 1)

if ($stalePID) {
    Write-Host "        Port 8000 in use (PID $stalePID). Killing stale process..." -ForegroundColor Yellow
    taskkill /PID $stalePID /F | Out-Null
    Start-Sleep -Seconds 2
    Write-Host "        Port 8000 freed." -ForegroundColor Green
} else {
    Write-Host "        Port 8000 available." -ForegroundColor Green
}

# -- 4. FastAPI server --

Write-Host "  [4/4] Starting FastAPI server..." -ForegroundColor White

if ($Visible) {
    Write-Host ""
    Write-Host "  Open: http://localhost:8000" -ForegroundColor Cyan
    Write-Host "  API docs: http://localhost:8000/api/docs" -ForegroundColor Cyan
    Write-Host "  Press Ctrl+C to stop the server" -ForegroundColor Cyan
    Write-Host ""

    $env:PYTHONPATH = "."
    $env:HF_TOKEN   = $hfToken

    & "$Root\venv\Scripts\activate.ps1"
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

} else {
    # Build uvicorn CMD script
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

    $uvicornProc = Start-Process cmd -ArgumentList @("/C", $uvicornPath) -WindowStyle Hidden -PassThru

    # Save PIDs so the web UI shutdown button can kill them
    $pidFile = "$Root\.server.pids"
    $pidsObj = @{ gpu_worker = $gpuProc.Id; light_worker = $lightProc.Id; uvicorn = $uvicornProc.Id }
    $pidsJson = $pidsObj | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($pidFile, $pidsJson)

    Write-Host "        API server started (hidden)." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Server running at http://localhost:8000" -ForegroundColor Cyan
    Write-Host "  Use the UI shutdown button to stop" -ForegroundColor Cyan
    Write-Host "  PIDs saved to .server.pids" -ForegroundColor Cyan
    Write-Host ""

    # Wait for uvicorn to start, then open browser
    Start-Sleep -Seconds 3
    Start-Process "http://127.0.0.1:8000"

    Write-Host "  Browser opened. This window will close." -ForegroundColor Green
    Write-Host ""
}
