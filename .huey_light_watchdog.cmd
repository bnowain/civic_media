@echo off
title Civic Media - Huey Light Worker (watchdog)
if not exist "E:\0-Automated-Apps\civic_media\logs" mkdir "E:\0-Automated-Apps\civic_media\logs"
:restart
cd /d "E:\0-Automated-Apps\civic_media"
call "E:\0-Automated-Apps\civic_media\venv\Scripts\activate.bat"
set PYTHONPATH=.
echo [%date% %time%] Starting Huey light worker... >> "E:\0-Automated-Apps\civic_media\logs\huey_light.log" 2>&1
python -m huey.bin.huey_consumer app.worker.huey_light -w 1 -k thread -C >> "E:\0-Automated-Apps\civic_media\logs\huey_light.log" 2>&1
echo [%date% %time%] Light worker exited (see logs\huey_light.log). Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto restart
