#!/usr/bin/env python3
"""
backfill_watchdog.py — Dual-worker backfill coordinator.

Manages two Celery workers processing from opposite ends of the queue:
  Worker 1 (oldest-first): always running, fed via /next-process
  Worker 2 (newest-first): auto-started when pending > 9,
           auto-stopped when workers converge within 3 meetings.

Transcode jobs (CPU-only) are fed first via /next-transcode, clearing
the path for processing. Once all pending transcodes are done, the
watchdog falls back to feeding process jobs only.

Both workers are started as subprocesses using the venv celery.exe.
Ctrl+C stops everything (both workers + watchdog).

Usage:
    python scripts/backfill_watchdog.py
    python scripts/backfill_watchdog.py --interval 45
    python scripts/backfill_watchdog.py --single   # force single-worker mode
"""

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_FILE = LOG_DIR / "backfill_watchdog.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

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

CELERY_EXE = str(Path(__file__).parent.parent / "venv" / "Scripts" / "celery.exe")
PROJECT_DIR = str(Path(__file__).parent.parent)

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


def get_queue_len() -> int:
    """Check how many process_video tasks are in the Redis queue."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, db=0)
        return r.llen("celery")
    except Exception:
        return -1


def get_status() -> dict:
    """Get backfill status including process_pending and transcode_pending."""
    status = api_get("status")
    if "error" in status:
        return {"process_pending": -1, "transcode_pending": -1}
    return status


# ── Worker lifecycle ─────────────────────────────────────────────────────────

class WorkerHandle:
    """Manages a single Celery worker subprocess."""

    def __init__(self, name: str, node_name: str):
        self.name = name
        self.node_name = node_name
        self.proc = None
        self.start_time = None
        self.ready = False

    def start(self):
        if self.alive():
            logger.info("%s already running (PID %d)", self.name, self.proc.pid)
            return
        log_path = str(LOG_DIR / f"celery_{self.node_name}.log")
        cmd = [
            CELERY_EXE, "-A", "app.worker", "worker",
            "--loglevel=info", "--concurrency=1", "--pool=solo",
            "-n", f"{self.node_name}@%h",
            "--logfile", log_path,
        ]
        logger.info("Starting %s: %s", self.name, " ".join(cmd[-6:]))
        self.proc = subprocess.Popen(
            cmd, cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.start_time = time.time()
        self.ready = False
        logger.info("%s started (PID %d) — ready in ~%ds",
                    self.name, self.proc.pid, WORKER_STARTUP_SECS)

    def stop(self, reason: str = ""):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            self.ready = False
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

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def check_ready(self):
        """Mark ready after startup delay."""
        if self.ready or not self.start_time:
            return
        if time.time() - self.start_time >= WORKER_STARTUP_SECS:
            self.ready = True
            logger.info("%s is ready (%.0fs elapsed)", self.name,
                        time.time() - self.start_time)

    @property
    def status(self) -> str:
        if self.ready:
            return "ready"
        if self.alive():
            return "starting"
        return "off"


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dual-worker backfill coordinator")
    parser.add_argument("--interval", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--single", action="store_true",
                        help="Force single-worker mode (no Worker 2)")
    args = parser.parse_args()

    dual_mode = not args.single
    total_oldest = 0
    total_newest = 0
    total_transcodes = 0

    worker1 = WorkerHandle("Worker 1", "worker1")
    worker2 = WorkerHandle("Worker 2", "worker2")

    # Track what each worker last queued
    oldest_date = None
    newest_date = None

    def cleanup(*_):
        worker2.stop("watchdog exit")
        worker1.stop("watchdog exit")
        logger.info("Watchdog stopped. Queued: %d oldest, %d newest, %d transcodes",
                    total_oldest, total_newest, total_transcodes)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    logger.info("=" * 60)
    logger.info("Backfill watchdog started — interval=%ds, dual=%s",
                args.interval, dual_mode)
    if dual_mode:
        logger.info("Worker 2 activates when pending > %d, stops at pending <= %d",
                    ACTIVATE_THRESHOLD, CONVERGE_GAP)
    logger.info(gpu_stats())

    # Always start Worker 1
    worker1.start()

    while True:
        try:
            worker1.check_ready()
            worker2.check_ready()

            queue_len = get_queue_len()
            status = get_status()
            pending = status.get("process_pending", -1)
            transcode_pending = status.get("transcode_pending", 0)

            # ── Worker 1 health check ────────────────────────────────────
            if not worker1.alive():
                logger.warning("Worker 1 died — restarting")
                worker1.start()

            # ── Worker 2 lifecycle ───────────────────────────────────────
            if dual_mode:
                if not worker2.alive() and pending > ACTIVATE_THRESHOLD:
                    start_msg = f"pending={pending} > {ACTIVATE_THRESHOLD}"
                    logger.info("Activating Worker 2 — %s", start_msg)
                    worker2.start()

                if worker2.alive() and pending >= 0 and pending <= CONVERGE_GAP:
                    worker2.stop(f"converged: pending={pending} <= {CONVERGE_GAP}")

            logger.info("Queue: %s | Process: %s | Transcode: %s | W1: %s | W2: %s | "
                        "Oldest: %s | Newest: %s | %s",
                        queue_len if queue_len >= 0 else "?",
                        pending if pending >= 0 else "?",
                        transcode_pending,
                        worker1.status, worker2.status,
                        oldest_date or "—", newest_date or "—",
                        gpu_stats())

            # ── Feed transcodes first (CPU-only, clears the path for processing) ──
            if transcode_pending > 0 and queue_len >= 0 and queue_len < 1:
                if worker1.ready:
                    result = api_post("next-transcode")
                    if result.get("queued"):
                        total_transcodes += 1
                        logger.info("Transcode queued: %s — %s (%s) [#%d]",
                                    result.get("meeting_date", "?"),
                                    result.get("title", "?")[:50],
                                    result.get("meeting_id", "?")[:8],
                                    total_transcodes)
                        # Re-check queue before feeding process jobs
                        queue_len = get_queue_len()
                    elif result.get("skipped"):
                        logger.info("Transcode skipped: %s (%s)",
                                    result.get("meeting_id", "?")[:8],
                                    result.get("reason", "?"))
                    elif result.get("nothing_to_do"):
                        logger.info("Transcode: nothing to do")
                    elif result.get("error"):
                        logger.error("Transcode error: %s", result["error"])

            # ── Feed Worker 1 (oldest-first) ─────────────────────────────
            if worker1.ready and queue_len >= 0 and queue_len < 1:
                result = api_post("next-process")
                if result.get("queued"):
                    total_oldest += 1
                    oldest_date = result["meeting_date"]
                    logger.info("W1 queued: %s — %s (%s) [#%d]",
                                result["meeting_date"],
                                result["title"][:50],
                                result["meeting_id"][:8], total_oldest)
                elif result.get("nothing_to_do"):
                    if not worker2.alive() and transcode_pending == 0:
                        logger.info("All done — backfill complete!")
                        cleanup()
                    elif transcode_pending > 0:
                        logger.info("W1: no process jobs (transcode_pending=%d)",
                                    transcode_pending)
                    else:
                        logger.info("W1: nothing left (W2 still running)")
                elif result.get("error"):
                    logger.error("W1 error: %s", result["error"])

            # ── Feed Worker 2 (newest-first) ─────────────────────────────
            if worker2.ready and queue_len >= 0 and queue_len < 1:
                result = api_post("next-process-newest")
                if result.get("queued"):
                    total_newest += 1
                    newest_date = result["meeting_date"]
                    logger.info("W2 queued: %s — %s (%s) [#%d]",
                                result["meeting_date"],
                                result["title"][:50],
                                result["meeting_id"][:8], total_newest)
                elif result.get("nothing_to_do"):
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
