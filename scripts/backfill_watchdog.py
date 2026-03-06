#!/usr/bin/env python3
"""
backfill_watchdog.py — Worker coordinator with queue-based routing.

Manages three Celery workers:
  Worker 1 (GPU, oldest-first): always running, consumes from "celery" queue
  Worker 2 (GPU, newest-first): auto-started when pending > 9, consumes from "celery" queue
  Light worker: auto-started on demand, consumes from "light" queue
    (ingest, download, transcode, export, retag, cleanup)

GPU workers only consume from "celery" queue (-Q celery).
Light worker only consumes from "light" queue (-Q light).
This prevents ingest/download/transcode from waiting behind multi-hour
transcription jobs, and prevents GPU workers from being blocked by I/O tasks.

PID files are written to .worker_pids/ for external visibility.

Modes:
  --orchestrate-only: Skip worker lifecycle management (start/stop/restart).
    Only do task feeding and stuck detection via the API. Use this from WSL
    when workers are already running on the Windows side.

Usage:
    python scripts/backfill_watchdog.py
    python scripts/backfill_watchdog.py --interval 45
    python scripts/backfill_watchdog.py --single           # force single GPU worker
    python scripts/backfill_watchdog.py --orchestrate-only  # WSL: feed tasks only
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

IS_WINDOWS = sys.platform == "win32"

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "logs"
PID_DIR = BASE_DIR / ".worker_pids"
LOG_FILE = LOG_DIR / "backfill_watchdog.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PID_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

API_BASE = "http://localhost:8000"
GROUP = "Board of Supervisors"

# Celery executable: Windows venv uses Scripts/celery.exe,
# WSL/Linux can call celery.exe via Windows interop or use a native celery.
if IS_WINDOWS:
    CELERY_EXE = str(BASE_DIR / "venv" / "Scripts" / "celery.exe")
else:
    # WSL: celery.exe works via Windows interop layer
    CELERY_EXE = str(BASE_DIR / "venv" / "Scripts" / "celery.exe")
PROJECT_DIR = str(BASE_DIR)


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform)."""
    if IS_WINDOWS:
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        # POSIX: signal 0 checks existence without killing
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

# ── Thresholds ───────────────────────────────────────────────────────────────

ACTIVATE_THRESHOLD = 9    # start Worker 2 when process_pending > this
CONVERGE_GAP = 3          # stop Worker 2 when process_pending <= this
WORKER_STARTUP_SECS = 100 # wait this long for a worker to be ready (mingle phase)


# ── Helpers ──────────────────────────────────────────────────────────────────

def gpu_stats() -> str:
    """Return a one-line GPU summary, or 'n/a' on failure."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        ).strip()
        used, free, util = [x.strip() for x in out.split(",")]
        return f"GPU: {used}MB used, {free}MB free, {util}% util"
    except Exception:
        return "GPU: n/a"


def api_post(endpoint: str) -> dict:
    """POST to a backfill endpoint and return JSON response."""
    url = f"{API_BASE}/api/backfill/{endpoint}?group_name={GROUP.replace(' ', '+')}"
    req = Request(url, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except URLError as e:
        return {"error": str(e)}


def api_get(endpoint: str) -> dict:
    """GET a backfill endpoint and return JSON response."""
    url = f"{API_BASE}/api/backfill/{endpoint}"
    try:
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except URLError as e:
        return {"error": str(e)}


def get_queue_len(queue: str = "celery") -> int:
    """Check how many tasks are in a Redis queue."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, db=0)
        return r.llen(queue)
    except Exception:
        return -1


def get_status() -> dict:
    """Get backfill status including process_pending and transcode_pending."""
    status = api_get("status")
    if "error" in status:
        return {"process_pending": -1, "transcode_pending": -1}
    return status


def check_and_reset_stuck() -> int:
    """Check for stuck jobs and reset them via API. Returns count reset."""
    stuck_info = api_get("stuck")
    count = stuck_info.get("count", 0)
    if count > 0:
        result = api_post("reset-stuck")
        reset_count = result.get("count", 0)
        if reset_count > 0:
            logger.warning("Watchdog self-heal: reset %d stuck job(s)", reset_count)
        return reset_count
    return 0


# ── Worker lifecycle ─────────────────────────────────────────────────────────

class WorkerHandle:
    """Manages a single Celery worker subprocess."""

    def __init__(self, name: str, node_name: str, queue: str = "celery"):
        self.name = name
        self.node_name = node_name
        self.queue = queue
        self.proc = None
        self.start_time = None
        self.ready = False
        self.pid_file = PID_DIR / f"{node_name}.pid"

    def start(self):
        if self.alive():
            pid = self.proc.pid if self.proc else self._find_existing_pid() or "?"
            logger.info("%s already running (PID %s)", self.name, pid)
            return
        # Guard: check if a celery process with our node name already exists
        # (could be a process started by a previous watchdog instance)
        existing_pid = self._find_existing_pid()
        if existing_pid:
            logger.info("%s already running externally (PID %d) — adopting",
                        self.name, existing_pid)
            self.start_time = time.time() - WORKER_STARTUP_SECS  # assume ready
            self.ready = True
            try:
                self.pid_file.write_text(str(existing_pid))
            except Exception:
                pass
            return
        log_path = str(LOG_DIR / f"celery_{self.node_name}.log")
        cmd = [
            CELERY_EXE, "-A", "app.worker", "worker",
            "--loglevel=info", "--concurrency=1", "--pool=solo",
            "-Q", self.queue,
            "-n", f"{self.node_name}@%h",
            "--logfile", log_path,
            "--without-mingle", "--without-gossip",
        ]
        logger.info("Starting %s: queue=%s node=%s", self.name, self.queue, self.node_name)
        self.proc = subprocess.Popen(
            cmd, cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.start_time = time.time()
        self.ready = False
        # Write PID file
        try:
            self.pid_file.write_text(str(self.proc.pid))
        except Exception:
            pass
        logger.info("%s started (PID %d) — ready in ~%ds",
                    self.name, self.proc.pid, WORKER_STARTUP_SECS)

    def _find_existing_pid(self) -> int | None:
        """Check PID file for an existing live process (cross-platform)."""
        if not self.pid_file.exists():
            return None
        try:
            pid = int(self.pid_file.read_text().strip())
            if _pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass
        return None

    def stop(self, reason: str = ""):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            self.ready = False
            self._remove_pid_file()
            return
        logger.info("Stopping %s (PID %d) — %s", self.name, self.proc.pid, reason)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        logger.info("%s stopped", self.name)
        self.proc = None
        self.ready = False
        self.start_time = None
        self._remove_pid_file()

    def alive(self) -> bool:
        # Check subprocess handle first
        if self.proc is not None and self.proc.poll() is None:
            return True
        # Fallback: check PID file (for adopted workers)
        if self._find_existing_pid() is not None:
            return True
        return False

    def check_ready(self):
        """Mark ready after startup delay."""
        if self.ready or not self.start_time:
            return
        if time.time() - self.start_time >= WORKER_STARTUP_SECS:
            self.ready = True
            logger.info("%s is ready (%.0fs elapsed)", self.name,
                        time.time() - self.start_time)

    def _remove_pid_file(self):
        try:
            self.pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    @property
    def status(self) -> str:
        if self.ready:
            return "ready"
        if self.alive():
            return "starting"
        return "off"


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Worker coordinator with queue routing")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--single", action="store_true",
                        help="Force single GPU worker (no Worker 2)")
    parser.add_argument("--orchestrate-only", action="store_true",
                        help="Skip worker lifecycle management — only feed tasks "
                             "and detect stuck jobs via API. Use from WSL when "
                             "workers are already running on the Windows side.")
    args = parser.parse_args()

    orchestrate_only = args.orchestrate_only
    dual_mode = not args.single
    total_oldest = 0
    total_newest = 0
    total_transcodes = 0

    worker1 = WorkerHandle("Worker 1 (GPU)", "worker1", queue="celery")
    worker2 = WorkerHandle("Worker 2 (GPU)", "worker2", queue="celery")
    light   = WorkerHandle("Light worker",   "light",   queue="light")

    # Track what each worker last queued
    oldest_date = None
    newest_date = None

    def cleanup(*_):
        if not orchestrate_only:
            worker2.stop("watchdog exit")
            worker1.stop("watchdog exit")
            light.stop("watchdog exit")
        logger.info("Watchdog stopped. Queued: %d oldest, %d newest, %d transcodes",
                    total_oldest, total_newest, total_transcodes)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    logger.info("=" * 60)
    mode_label = "orchestrate-only" if orchestrate_only else f"dual={dual_mode}"
    logger.info("Backfill watchdog started — interval=%ds, %s",
                args.interval, mode_label)
    if orchestrate_only:
        logger.info("Orchestrate-only mode: feeding tasks and detecting stuck jobs "
                     "(workers managed externally)")
    else:
        logger.info("Queue routing: GPU workers → 'celery', Light worker → 'light'")
        if dual_mode:
            logger.info("Worker 2 activates when pending > %d, stops at pending <= %d",
                        ACTIVATE_THRESHOLD, CONVERGE_GAP)
    logger.info(gpu_stats())

    # Start workers (skip in orchestrate-only mode)
    if not orchestrate_only:
        worker1.start()
        light.start()

    while True:
        try:
            if not orchestrate_only:
                worker1.check_ready()
                worker2.check_ready()
                light.check_ready()

            queue_len = get_queue_len("celery")
            light_queue_len = get_queue_len("light")
            status = get_status()
            pending = status.get("process_pending", -1)
            transcode_pending = status.get("transcode_pending", 0)

            # ── Worker health checks (skip in orchestrate-only) ───────────
            if not orchestrate_only:
                if not worker1.alive():
                    logger.warning("Worker 1 died — restarting")
                    worker1.start()

                if not light.alive():
                    if light_queue_len > 0:
                        logger.warning("Light worker died with %d tasks queued — restarting",
                                       light_queue_len)
                        light.start()

            # ── Self-heal: reset stuck jobs ────────────────────────────────
            stuck_count = status.get("stuck", 0)
            if stuck_count > 0:
                check_and_reset_stuck()

            # ── Worker 2 lifecycle (skip in orchestrate-only) ─────────────
            if not orchestrate_only and dual_mode:
                if not worker2.alive() and pending > ACTIVATE_THRESHOLD:
                    start_msg = f"pending={pending} > {ACTIVATE_THRESHOLD}"
                    logger.info("Activating Worker 2 — %s", start_msg)
                    worker2.start()

                if worker2.alive() and pending >= 0 and pending <= CONVERGE_GAP:
                    worker2.stop(f"converged: pending={pending} <= {CONVERGE_GAP}")

            if orchestrate_only:
                logger.info("Queue: %s | Light: %s | Process: %s | Transcode: %s | "
                            "Stuck: %s | Oldest: %s | Newest: %s | %s",
                            queue_len if queue_len >= 0 else "?",
                            light_queue_len if light_queue_len >= 0 else "?",
                            pending if pending >= 0 else "?",
                            transcode_pending,
                            stuck_count,
                            oldest_date or "—", newest_date or "—",
                            gpu_stats())
            else:
                logger.info("Queue: %s | Light: %s | Process: %s | Transcode: %s | "
                            "W1: %s | W2: %s | L: %s | "
                            "Oldest: %s | Newest: %s | %s",
                            queue_len if queue_len >= 0 else "?",
                            light_queue_len if light_queue_len >= 0 else "?",
                            pending if pending >= 0 else "?",
                            transcode_pending,
                            worker1.status, worker2.status, light.status,
                            oldest_date or "—", newest_date or "—",
                            gpu_stats())

            # ── In orchestrate-only mode, treat workers as always ready ───
            w1_ready = worker1.ready if not orchestrate_only else True
            w2_ready = worker2.ready if not orchestrate_only else False
            w2_alive = worker2.alive() if not orchestrate_only else False

            # ── Feed transcodes first (now goes to light queue) ───────────
            if transcode_pending > 0 and queue_len >= 0 and queue_len < 1:
                if w1_ready:
                    result = api_post("next-transcode")
                    if result.get("queued"):
                        total_transcodes += 1
                        logger.info("Transcode queued: %s — %s (%s) [#%d]",
                                    result.get("meeting_date", "?"),
                                    result.get("title", "?")[:50],
                                    result.get("meeting_id", "?")[:8],
                                    total_transcodes)
                        queue_len = get_queue_len("celery")
                    elif result.get("skipped"):
                        logger.info("Transcode skipped: %s (%s)",
                                    result.get("meeting_id", "?")[:8],
                                    result.get("reason", "?"))
                    elif result.get("nothing_to_do"):
                        logger.info("Transcode: nothing to do")
                    elif result.get("error"):
                        logger.error("Transcode error: %s", result["error"])

            # ── Feed Worker 1 (oldest-first) ──────────────────────────────
            if w1_ready and queue_len >= 0 and queue_len < 1:
                result = api_post("next-process")
                if result.get("queued"):
                    total_oldest += 1
                    oldest_date = result["meeting_date"]
                    logger.info("W1 queued: %s — %s (%s) [#%d]",
                                result["meeting_date"],
                                result["title"][:50],
                                result["meeting_id"][:8], total_oldest)
                elif result.get("nothing_to_do"):
                    if not w2_alive and transcode_pending == 0:
                        logger.info("All done — backfill complete!")
                        cleanup()
                    elif transcode_pending > 0:
                        logger.info("W1: no process jobs (transcode_pending=%d)",
                                    transcode_pending)
                    else:
                        logger.info("W1: nothing left (W2 still running)")
                elif result.get("error"):
                    logger.error("W1 error: %s", result["error"])

            # ── Feed Worker 2 (newest-first) ──────────────────────────────
            if w2_ready and queue_len >= 0 and queue_len < 1:
                result = api_post("next-process-newest")
                if result.get("queued"):
                    total_newest += 1
                    newest_date = result["meeting_date"]
                    logger.info("W2 queued: %s — %s (%s) [#%d]",
                                result["meeting_date"],
                                result["title"][:50],
                                result["meeting_id"][:8], total_newest)
                elif result.get("nothing_to_do"):
                    if not orchestrate_only:
                        worker2.stop("nothing left for newest-first")
                elif result.get("error"):
                    logger.error("W2 error: %s", result["error"])

        except KeyboardInterrupt:
            cleanup()
        except Exception as e:
            logger.error("Watchdog error: %s", e)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
