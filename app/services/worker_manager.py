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

# Ghost binding flush cooldown — flush at most once per minute on dispatch
_last_binding_flush: float = 0.0
_BINDING_FLUSH_COOLDOWN = 60.0  # seconds


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


def _flush_kombu_bindings() -> None:
    """Flush stale Kombu routing-key registrations left by killed workers.

    Matches the same logic in worker.py's celeryd_init signal, but called
    synchronously during restart so ghost bindings are gone before the new
    worker starts (prevents DuplicateNodenameWarning).
    """
    try:
        import redis
        r = redis.from_url(CELERY_BROKER, socket_connect_timeout=2, socket_timeout=2)
        keys = [
            "_kombu.binding.celeryev",
            "_kombu.binding.celery.pidbox",
            "_kombu.binding.reply.celery.pidbox",
        ]
        deleted = r.delete(*keys)
        if deleted:
            logger.info("Flushed %d stale Kombu binding key(s) from Redis", deleted)
        r.close()
    except Exception as exc:
        logger.warning("Could not flush Kombu bindings (non-fatal): %s", exc)


def get_active_job() -> dict | None:
    """Scan MEDIA_DIR for the most recently updated in-progress pipeline job.

    Returns a dict: {meeting_id, title, stage, pct, detail} or None.
    Uses filesystem scan (fast) instead of Celery inspect (slow).
    """
    import json as _json
    from datetime import datetime, timezone as _tz

    try:
        from app.config import MEDIA_DIR
    except Exception:
        return None

    if not MEDIA_DIR.exists():
        return None

    TERMINAL = {"Complete", "Error", ""}
    MAX_STALE_SECS = 300  # 5 minutes — beyond this, job is likely stuck

    best: dict | None = None
    best_ts = None

    for progress_file in MEDIA_DIR.glob("*/progress.json"):
        try:
            data = _json.loads(progress_file.read_text())
        except Exception:
            continue

        stage = data.get("stage", "")
        if stage in TERMINAL:
            continue

        updated_str = data.get("updated_at")
        if not updated_str:
            continue

        try:
            updated_at = datetime.fromisoformat(updated_str)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=_tz.utc)
        except (ValueError, TypeError):
            continue

        age = (datetime.now(_tz.utc) - updated_at).total_seconds()
        if age > MAX_STALE_SECS:
            continue

        if best_ts is None or updated_at > best_ts:
            best_ts = updated_at
            best = {
                "meeting_id": progress_file.parent.name,
                "stage": stage,
                "pct": data.get("pct", 0),
                "detail": data.get("detail", ""),
                "title": None,
            }

    if not best:
        return None

    # Resolve meeting title and date from DB
    try:
        from app.database import SessionLocal
        from app import models
        db = SessionLocal()
        try:
            meeting = db.query(models.Meeting).filter(
                models.Meeting.meeting_id == best["meeting_id"]
            ).first()
            if meeting:
                best["title"] = meeting.title or meeting.group_name or "Unknown"
                best["meeting_date"] = str(meeting.meeting_date)[:10] if meeting.meeting_date else None
            else:
                best["title"] = "Unknown meeting"
                best["meeting_date"] = None
        finally:
            db.close()
    except Exception:
        best["title"] = "Unknown meeting"
        best["meeting_date"] = None

    return best


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
        "active_job": get_active_job(),
    }

    _health_cache = result
    _health_cache_ts = now
    return result


def _kill_by_cmdline(signature: str, label: str) -> None:
    """Kill all processes whose CommandLine contains `signature` (PowerShell)."""
    try:
        # Use a temp PS1 file to avoid bash $_ expansion issues
        ps_script = (
            f"Get-CimInstance Win32_Process "
            f"| Where-Object {{ $_.CommandLine -like '*{signature}*' }} "
            f"| ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Killed all %s processes matching: %s", label, signature)
    except Exception as exc:
        logger.warning("Could not kill %s processes by cmdline: %s", label, exc)


def kill_zombies() -> None:
    """Kill watchdog cmd.exe, all celery shims, and all python.exe worker processes.

    Three-step kill ensures no orphans survive:
    1. Tracked watchdog PID tree (fast, catches the common case)
    2. Any cmd.exe running .celery_watchdog.cmd (catches untracked watchdogs)
    3. Any python.exe/celery.exe with our app.worker.celery_app signature
       (catches python.exe grandchildren that survive celery.exe kills)
    """
    # Step 1: Kill tracked watchdog PID tree
    pids = _read_pids()
    watchdog_pid = pids.get("watchdog")
    if watchdog_pid and _pid_alive(watchdog_pid):
        try:
            os.system(f"taskkill /PID {watchdog_pid} /T /F >nul 2>&1")
            logger.info("Killed watchdog PID tree: %s", watchdog_pid)
        except Exception as exc:
            logger.warning("Failed to kill watchdog PID %s: %s", watchdog_pid, exc)

    # Step 2: Kill untracked watchdog cmd.exe processes
    _kill_by_cmdline(".celery_watchdog.cmd", "watchdog cmd")

    # Step 3: Kill any remaining celery worker processes by app signature
    # (catches python.exe grandchildren that survive the celery.exe shim kill)
    _kill_by_cmdline("app.worker.celery_app", "celery worker")

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

    # Step 1b: Flush stale Kombu ghost bindings from Redis so the new worker
    # doesn't inherit duplicate node name registrations from killed workers.
    _flush_kombu_bindings()

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

    Also periodically flushes stale Kombu ghost bindings from Redis
    (at most once per minute) so that ghost node registrations left by
    previously killed workers don't accumulate into DuplicateNodenameWarnings.

    Raises RuntimeError if the worker cannot be recovered.
    """
    global _last_binding_flush

    # Flush stale Kombu ghost bindings periodically — fast (~1ms Redis DELETE).
    # Safe to run while the worker is alive; it re-registers on next heartbeat.
    now = time.time()
    if (now - _last_binding_flush) > _BINDING_FLUSH_COOLDOWN:
        _flush_kombu_bindings()
        _last_binding_flush = now

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
