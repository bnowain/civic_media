# restart_server.ps1
# Restarts ONLY the uvicorn API server — workers keep running untouched.
# Use this after code changes that don't affect worker logic.
#
# Usage:
#   .\restart_server.ps1

$Root = $PSScriptRoot

Write-Host ""
Write-Host "  CIVIC MEDIA - Restarting API server only..." -ForegroundColor Cyan
Write-Host ""

# Kill existing uvicorn using saved PID
$pidFile = "$Root\.server.pids"
if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile | ConvertFrom-Json
    if ($pids.uvicorn) {
        Write-Host "  Stopping uvicorn (PID $($pids.uvicorn))..." -ForegroundColor White
        taskkill /PID $pids.uvicorn /F 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }
} else {
    # No PID file — kill anything on port 8000
    Write-Host "  No PID file found. Freeing port 8000..." -ForegroundColor Yellow
    $stalePID = (netstat -ano 2>$null | Select-String ":8000\s" |
        Where-Object { $_ -match "LISTENING" } |
        ForEach-Object { ($_.ToString().Trim() -split "\s+")[-1] } |
        Select-Object -First 1)
    if ($stalePID) {
        taskkill /PID $stalePID /F | Out-Null
        Start-Sleep -Seconds 2
    }
}

# Start new uvicorn
Write-Host "  Starting uvicorn..." -ForegroundColor White
$newProc = Start-Process cmd -ArgumentList @("/C", "$Root\.uvicorn_start.cmd") -WindowStyle Hidden -PassThru

# Update PID file
if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile | ConvertFrom-Json
    $pids.uvicorn = $newProc.Id
    $pids | ConvertTo-Json -Compress | Set-Content $pidFile
} else {
    @{ uvicorn = $newProc.Id } | ConvertTo-Json -Compress | Set-Content $pidFile
}

Start-Sleep -Seconds 2
Write-Host "  API server restarted (PID $($newProc.Id))" -ForegroundColor Green
Write-Host "  http://localhost:8000" -ForegroundColor Cyan
Write-Host ""
