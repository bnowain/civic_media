@echo off
title Civic Media - Dual Worker Backfill
cd /d "E:\0-Automated-Apps\civic_media"
call "E:\0-Automated-Apps\civic_media\venv\Scripts\activate.bat"
set PYTHONPATH=.

echo.
echo === Dual Worker Backfill Coordinator ===
echo.
echo  Worker 1 (oldest-first): always on
echo  Worker 2 (newest-first): auto-starts when pending ^> 9
echo                            auto-stops when pending ^<= 3
echo.
echo  Both workers + watchdog stop on Ctrl+C.
echo  API server must already be running on port 8000.
echo.

python scripts/backfill_watchdog.py --interval 60
