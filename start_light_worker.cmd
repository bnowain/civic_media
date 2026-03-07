@echo off
REM Light worker — handles ingest, download, transcode, and other I/O tasks.
REM Consumes from the huey_light queue only (no GPU tasks).
REM
REM Usage: start_light_worker.cmd
REM   or:  start /B start_light_worker.cmd  (background)

cd /d "%~dp0"
call venv\Scripts\activate.bat

echo Starting Huey light worker...
python -m huey.bin.huey_consumer app.worker.huey_light -w 1 -k thread -C --logfile logs/huey_light.log
