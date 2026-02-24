"""
Celery worker lifecycle management — health checks, restart, auto-recovery.

Used by:
  - GET  /api/system/worker-health   (status pill polling)
  - POST /api/system/restart-worker  (manual restart)
  - ensure_worker()                  (auto-recovery before .delay() calls)
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from app.config import BASE_DIR, CELERY_BROKER

logger = logging.getLogger(__name__)

_PID_FILE = BASE_DIR / ".server.pids"
_WATCHDOG_CMD = BASE_DIR / ".celery_watchdog.cmd"

# Cached health result to avoid hammering Redis/Celery on every .delay()
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_CACHE_TTL = 5.0  # seconds


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


def _read_pids() -> dict:
    """Read .server.pids file."""
    if not _PID_FILE.exists():
        return {}
    try:
        return json.loads(_PID_FILE.read_text())
    except Exception:
        return {}


def _write_pids(pids: dict) -> None:
    """Write .server.pids file."""
    try:
        _PID_FILE.write_text(json.dumps(pids))
    except Exception as exc:
        logger.warning("Could not write .server.pids: %s", exc)


def _ping_worker(timeout: float = 2.0) -> bool:
    """Ping Celery worker via control.inspect(). Returns True if any worker responds."""
    try:
        from app.worker import celery_app
        result = celery_app.control.inspect(timeout=timeout).ping()
        return bool(result)
    except Exception:
        return False


def _check_redis() -> bool:
    """Check if Redis broker is reachable."""
    try:
        import redis
        r = redis.from_url(CELERY_BROKER, socket_connect_timeout=2)
        r.ping()
        return True
    except Exception:
        return False


def _count_active_tasks() -> int:
    """Count currently active tasks in the worker."""
    try:
        from app.worker import celery_app
        result = celery_app.control.inspect(timeout=2).active()
        if result:
            return sum(len(tasks) for tasks in result.values())
        return 0
    except Exception:
        return 0


def check_worker_health(use_cache: bool = True) -> dict:
    """
    Check Celery worker health. Returns a status dict.

    Results are cached for _CACHE_TTL seconds to avoid overhead
    on rapid successive calls (e.g. multiple .delay() in one request).
    """
    global _health_cache, _health_cache_ts

    now = time.time()
    if use_cache and _health_cache and (now - _health_cache_ts) < _CACHE_TTL:
        return _health_cache

    pids = _read_pids()
    watchdog_pid = pids.get("watchdog")
    watchdog_alive = _pid_alive(watchdog_pid) if watchdog_pid else False

    worker_online = _ping_worker(timeout=2.0)
    redis_online = _check_redis()
    active_tasks = _count_active_tasks() if worker_online else 0

    result = {
        "worker_online": worker_online,
        "redis_online": redis_online,
        "active_tasks": active_tasks,
        "watchdog_alive": watchdog_alive,
        "watchdog_pid": watchdog_pid,
    }

    _health_cache = result
    _health_cache_ts = now
    return result


def kill_zombies() -> None:
    """Kill orphaned celery processes and the watchdog PID tree."""
    # Kill watchdog PID tree if it exists
    pids = _read_pids()
    watchdog_pid = pids.get("watchdog")
    if watchdog_pid and _pid_alive(watchdog_pid):
        try:
            os.system(f"taskkill /PID {watchdog_pid} /T /F >nul 2>&1")
            logger.info("Killed watchdog PID tree: %s", watchdog_pid)
        except Exception as exc:
            logger.warning("Failed to kill watchdog PID %s: %s", watchdog_pid, exc)

    # Kill any remaining celery.exe processes
    try:
        os.system("taskkill /F /IM celery.exe >nul 2>&1")
    except Exception:
        pass

    # Brief pause to let processes terminate
    time.sleep(0.5)


def restart_worker() -> dict:
    """
    Kill zombie processes, relaunch the celery watchdog, update PIDs, wait for ping.

    Returns a result dict with restart status.
    """
    global _health_cache, _health_cache_ts

    if not _WATCHDOG_CMD.exists():
        return {
            "restarted": False,
            "worker_online": False,
            "message": f"Watchdog script not found: {_WATCHDOG_CMD}",
        }

    logger.info("Restarting Celery worker...")

    # Step 1: Kill zombies
    kill_zombies()

    # Step 2: Launch watchdog in a new hidden window
    try:
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(
            ["cmd", "/C", str(_WATCHDOG_CMD)],
            creationflags=CREATE_NO_WINDOW,
            cwd=str(BASE_DIR),
        )
        new_pid = proc.pid
        logger.info("Launched watchdog with PID %s", new_pid)
    except Exception as exc:
        logger.error("Failed to launch watchdog: %s", exc)
        return {
            "restarted": False,
            "worker_online": False,
            "message": f"Failed to launch watchdog: {exc}",
        }

    # Step 3: Update .server.pids
    pids = _read_pids()
    pids["watchdog"] = new_pid
    _write_pids(pids)

    # Step 4: Poll for worker to come online (up to 15s)
    worker_online = False
    for _ in range(15):
        time.sleep(1.0)
        if _ping_worker(timeout=1.0):
            worker_online = True
            break

    # Invalidate cache
    _health_cache = {}
    _health_cache_ts = 0.0

    if worker_online:
        logger.info("Worker restarted successfully (PID %s)", new_pid)
    else:
        logger.warning("Worker launched but not responding after 15s (PID %s)", new_pid)

    return {
        "restarted": True,
        "worker_online": worker_online,
        "pid": new_pid,
        "message": "Worker restarted successfully" if worker_online else "Worker launched but not yet responding",
    }


def ensure_worker() -> None:
    """
    Call before any .delay(). If the worker is dead, restart it.

    Fast path (~0ms): uses cached health check.
    Slow path (~15s): only when worker needs restart.

    Raises RuntimeError if the worker cannot be recovered.
    """
    # Quick check using cache
    health = check_worker_health(use_cache=True)
    if health.get("worker_online"):
        return

    # Cache might be stale — do a fresh check
    health = check_worker_health(use_cache=False)
    if health.get("worker_online"):
        return

    # Worker is dead — attempt restart
    logger.warning("Worker is offline — attempting auto-restart before task dispatch")
    result = restart_worker()

    if not result.get("worker_online"):
        raise RuntimeError(
            "Celery worker is offline and could not be restarted. "
            "Check Redis and the worker logs."
        )

    logger.info("Auto-restart successful — proceeding with task dispatch")
