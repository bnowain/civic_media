"""
Huey worker lifecycle management — health checks, restart, auto-recovery.

Used by:
  - GET  /api/system/worker-health   (status pill polling)
  - POST /api/system/restart-worker  (manual restart)
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

_PID_FILE = BASE_DIR / ".server.pids"
_WATCHDOG_CMD = BASE_DIR / ".huey_watchdog.cmd"
_LIGHT_WATCHDOG_CMD = BASE_DIR / ".huey_light_watchdog.cmd"

# Cached health result to avoid overhead on rapid successive calls
_health_cache: dict = {}
_health_cache_ts: float = 0.0
_CACHE_TTL = 5.0  # seconds


def _pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running (cross-platform)."""
    import sys
    if sys.platform == "win32":
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
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False


def _read_pids() -> dict:
    if not _PID_FILE.exists():
        return {}
    try:
        return json.loads(_PID_FILE.read_text())
    except Exception:
        return {}


def _write_pids(pids: dict) -> None:
    try:
        _PID_FILE.write_text(json.dumps(pids))
    except Exception as exc:
        logger.warning("Could not write .server.pids: %s", exc)


def _ping_worker(timeout: float = 3.0) -> bool:
    """Check if a Huey worker is alive.

    Looks for huey_consumer.py or python.exe running huey_consumer in the process list.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH"],
            capture_output=True, text=True, timeout=3,
        )
        # Also check for huey_consumer running
        return "python.exe" in result.stdout and _has_huey_process()
    except Exception:
        return False


def _has_huey_process(queue_filter: str = "huey_consumer") -> bool:
    """Check if any python process is running huey_consumer.

    queue_filter can be 'huey_consumer' (any worker) or a more specific
    string like 'app.worker.huey_light' to check the light worker only.
    """
    import sys as _sys
    if _sys.platform != "win32":
        # Linux: check via pgrep
        try:
            result = subprocess.run(
                ["pgrep", "-f", queue_filter],
                capture_output=True, text=True, timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    try:
        result = subprocess.run(
            ["powershell", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
             f"| Where-Object {{ $_.CommandLine -like '*{queue_filter}*' }} "
             f"| Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=5,
        )
        count = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        return count > 0
    except Exception:
        # Fallback: check watchdog PID
        pids = _read_pids()
        watchdog_pid = pids.get("watchdog")
        return watchdog_pid is not None and _pid_alive(watchdog_pid)


def _count_active_tasks() -> int:
    """Count currently active tasks via the processing_jobs DB table."""
    try:
        from app.database import SessionLocal
        from app import models
        db = SessionLocal()
        try:
            return db.query(models.ProcessingJob).filter(
                models.ProcessingJob.status == "running"
            ).count()
        finally:
            db.close()
    except Exception:
        return 0


def get_active_job() -> dict | None:
    """Scan MEDIA_DIR for the most recently updated in-progress pipeline job.

    Returns a dict: {meeting_id, title, stage, pct, detail} or None.
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
    MAX_STALE_SECS = 1800

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
    """Check Huey worker health. Returns a status dict."""
    global _health_cache, _health_cache_ts

    now = time.time()
    if use_cache and _health_cache and (now - _health_cache_ts) < _CACHE_TTL:
        return _health_cache

    pids = _read_pids()
    watchdog_pid = pids.get("watchdog")
    watchdog_alive = _pid_alive(watchdog_pid) if watchdog_pid else False

    worker_online = _has_huey_process()
    active_tasks = _count_active_tasks() if worker_online else 0

    result = {
        "worker_online": worker_online,
        "redis_online": True,  # No Redis dependency — always "online"
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
    """Kill watchdog cmd.exe and all huey worker processes."""
    # Step 1: Kill tracked watchdog PID tree
    pids = _read_pids()
    watchdog_pid = pids.get("watchdog")
    if watchdog_pid and _pid_alive(watchdog_pid):
        try:
            os.system(f"taskkill /PID {watchdog_pid} /T /F >nul 2>&1")
            logger.info("Killed watchdog PID tree: %s", watchdog_pid)
        except Exception as exc:
            logger.warning("Failed to kill watchdog PID %s: %s", watchdog_pid, exc)

    # Step 1b: Kill tracked light watchdog PID tree
    light_watchdog_pid = pids.get("light_watchdog")
    if light_watchdog_pid and _pid_alive(light_watchdog_pid):
        try:
            os.system(f"taskkill /PID {light_watchdog_pid} /T /F >nul 2>&1")
            logger.info("Killed light watchdog PID tree: %s", light_watchdog_pid)
        except Exception as exc:
            logger.warning("Failed to kill light watchdog PID %s: %s", light_watchdog_pid, exc)

    # Step 2: Kill untracked watchdog cmd.exe processes
    _kill_by_cmdline(".huey_watchdog.cmd", "GPU watchdog cmd")
    _kill_by_cmdline(".huey_light_watchdog.cmd", "light watchdog cmd")
    _kill_by_cmdline(".celery_watchdog.cmd", "legacy watchdog cmd")

    # Step 3: Kill any huey_consumer processes
    _kill_by_cmdline("huey_consumer", "huey worker")

    time.sleep(0.5)


def _launch_watchdog(cmd_path: Path, pid_key: str) -> int | None:
    """Launch a watchdog .cmd in a hidden window. Returns PID or None."""
    import sys as _sys

    if _sys.platform == "win32":
        try:
            CREATE_NO_WINDOW = 0x08000000
            proc = subprocess.Popen(
                ["cmd", "/C", str(cmd_path)],
                creationflags=CREATE_NO_WINDOW,
                cwd=str(BASE_DIR),
            )
            pid = proc.pid
            logger.info("Launched %s with PID %s", cmd_path.name, pid)
            pids = _read_pids()
            pids[pid_key] = pid
            _write_pids(pids)
            return pid
        except Exception as exc:
            logger.error("Failed to launch %s: %s", cmd_path.name, exc)
            return None
    else:
        # Linux: launch huey_consumer directly as background process
        consumer_module = "app.worker.huey" if "light" not in cmd_path.name else "app.worker.huey_light"
        log_file = "logs/huey.log" if "light" not in cmd_path.name else "logs/huey_light.log"
        try:
            (BASE_DIR / "logs").mkdir(exist_ok=True)
            with open(BASE_DIR / log_file, "a") as lf:
                proc = subprocess.Popen(
                    ["python", "-m", "huey.bin.huey_consumer", consumer_module,
                     "-w", "1", "-k", "thread", "-C"],
                    stdout=lf, stderr=lf,
                    cwd=str(BASE_DIR),
                )
            pid = proc.pid
            logger.info("Launched %s consumer with PID %s", consumer_module, pid)
            pids = _read_pids()
            pids[pid_key] = pid
            _write_pids(pids)
            return pid
        except Exception as exc:
            logger.error("Failed to launch %s consumer: %s", consumer_module, exc)
            return None


def restart_worker() -> dict:
    """Kill zombie processes, relaunch both huey watchdogs, update PIDs, wait for ping."""
    global _health_cache, _health_cache_ts

    if not _WATCHDOG_CMD.exists():
        return {
            "restarted": False,
            "worker_online": False,
            "message": f"Watchdog script not found: {_WATCHDOG_CMD}",
        }

    logger.info("Restarting Huey workers...")

    # Step 1: Kill zombies
    kill_zombies()

    # Step 2: Launch GPU watchdog
    gpu_pid = _launch_watchdog(_WATCHDOG_CMD, "watchdog")

    # Step 3: Launch light watchdog
    light_pid = None
    if _LIGHT_WATCHDOG_CMD.exists():
        light_pid = _launch_watchdog(_LIGHT_WATCHDOG_CMD, "light_watchdog")
    else:
        logger.warning("Light watchdog script not found: %s", _LIGHT_WATCHDOG_CMD)

    # Step 4: Poll for worker to come online (up to 15s)
    worker_online = False
    for _ in range(15):
        time.sleep(1.0)
        if _has_huey_process():
            worker_online = True
            break

    # Invalidate cache
    _health_cache = {}
    _health_cache_ts = 0.0

    if worker_online:
        logger.info("Workers restarted successfully (GPU PID %s, light PID %s)", gpu_pid, light_pid)
    else:
        logger.warning("Workers launched but not responding after 15s")

    return {
        "restarted": True,
        "worker_online": worker_online,
        "gpu_pid": gpu_pid,
        "light_pid": light_pid,
        "message": "Workers restarted successfully" if worker_online else "Workers launched but not yet responding",
    }


def ensure_workers() -> None:
    """Auto-start Huey workers if not already running. Called on API startup."""
    # "app.worker.huey -" matches "app.worker.huey -w 1" but not "app.worker.huey_light"
    if _has_huey_process("app.worker.huey -"):
        logger.info("Huey GPU worker already running — skipping auto-start")
    elif _WATCHDOG_CMD.exists():
        logger.info("Auto-starting Huey GPU worker...")
        _launch_watchdog(_WATCHDOG_CMD, "watchdog")
    else:
        logger.warning("Cannot auto-start GPU worker: %s not found", _WATCHDOG_CMD)

    if _has_huey_process("app.worker.huey_light"):
        logger.info("Huey light worker already running — skipping auto-start")
    elif _LIGHT_WATCHDOG_CMD.exists():
        logger.info("Auto-starting Huey light worker...")
        _launch_watchdog(_LIGHT_WATCHDOG_CMD, "light_watchdog")
    else:
        logger.warning("Cannot auto-start light worker: %s not found", _LIGHT_WATCHDOG_CMD)
