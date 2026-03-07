@echo off
title Civic Media - Huey Worker (watchdog)
if not exist "E:\0-Automated-Apps\civic_media\logs" mkdir "E:\0-Automated-Apps\civic_media\logs"
:restart
cd /d "E:\0-Automated-Apps\civic_media"
call "E:\0-Automated-Apps\civic_media\venv\Scripts\activate.bat"
set PYTHONPATH=.
set HF_TOKEN=%HF_TOKEN%
echo [%date% %time%] Starting Huey GPU worker... >> "E:\0-Automated-Apps\civic_media\logs\huey.log" 2>&1
python -m huey.bin.huey_consumer app.worker.huey -w 1 -k thread -C >> "E:\0-Automated-Apps\civic_media\logs\huey.log" 2>&1
echo [%date% %time%] Worker exited (see logs\huey.log). Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto restart
