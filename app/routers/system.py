"""
System management endpoints.

GET   /api/system/status          — Check worker process health (PID-based).
GET   /api/system/worker-health   — Celery worker + Redis health (ping-based).
POST  /api/system/restart-worker  — Kill zombies and relaunch celery watchdog.
POST  /api/system/shutdown        — Gracefully stop all server processes.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

_PID_FILE = BASE_DIR / ".server.pids"


def _pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running (Windows)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


@router.get("/status")
def system_status():
    """Return health of background worker processes."""
    if not _PID_FILE.exists():
        return {"worker_alive": False, "pid_file": False}

    try:
        pids = json.loads(_PID_FILE.read_text())
    except Exception:
        return {"worker_alive": False, "pid_file": False}

    watchdog_pid = pids.get("watchdog")
    worker_alive = _pid_alive(watchdog_pid) if watchdog_pid else False
    return {"worker_alive": worker_alive, "watchdog_pid": watchdog_pid}


@router.get("/worker-health")
def worker_health():
    """Return Celery worker and Redis health status for the UI status pill."""
    from app.services.worker_manager import check_worker_health
    return check_worker_health(use_cache=False)


@router.post("/restart-worker")
def restart_worker():
    """Kill zombie workers and relaunch the celery watchdog."""
    from app.services.worker_manager import restart_worker as _restart
    return _restart()


@router.post("/shutdown")
async def system_shutdown():
    """Kill the watchdog/worker, clean up PID file, then exit the API server.

    The uvicorn PID in .server.pids is the cmd.exe wrapper that launched us.
    Killing it with /T /F would kill *this* process before we can respond or
    clean up.  So we only kill the watchdog (worker) here, delete the PID
    file, send the response, then os._exit after a brief delay.
    """
    killed = []
    pids = {}

    if _PID_FILE.exists():
        try:
            pids = json.loads(_PID_FILE.read_text())
        except Exception:
            pids = {}

        # Only kill the watchdog (celery worker tree) -- not our own uvicorn
        watchdog_pid = pids.get("watchdog")
        if watchdog_pid and _pid_alive(watchdog_pid):
            try:
                os.system(f"taskkill /PID {watchdog_pid} /T /F >nul 2>&1")
                killed.append("watchdog")
            except Exception as exc:
                logger.warning("Failed to kill watchdog (PID %s): %s", watchdog_pid, exc)

        try:
            _PID_FILE.unlink()
        except Exception:
            pass

    logger.info("Shutdown requested -- killed %s, exiting in 500ms", killed)

    # Schedule hard exit after response is sent
    async def _exit_soon():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.get_event_loop().create_task(_exit_soon())

    return {"status": "shutting_down", "killed": killed}
