"""
System management endpoints.

POST  /api/system/shutdown  — Gracefully stop all server processes.
GET   /api/system/status    — Check worker process health.
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


@router.post("/shutdown")
async def system_shutdown():
    """Kill background processes and shut down the API server."""
    killed = []

    if _PID_FILE.exists():
        try:
            pids = json.loads(_PID_FILE.read_text())
        except Exception:
            pids = {}

        for name, pid in pids.items():
            if pid and _pid_alive(pid):
                try:
                    os.system(f"taskkill /PID {pid} /T /F >nul 2>&1")
                    killed.append(name)
                except Exception as exc:
                    logger.warning("Failed to kill %s (PID %s): %s", name, pid, exc)

        try:
            _PID_FILE.unlink()
        except Exception:
            pass

    logger.info("Shutdown requested — killed %s, exiting in 500ms", killed)

    # Schedule hard exit after response is sent
    async def _exit_soon():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.get_event_loop().create_task(_exit_soon())

    return {"status": "shutting_down", "killed": killed}
