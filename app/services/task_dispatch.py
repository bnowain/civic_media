"""
Lightweight task dispatch — pushes Celery-compatible messages directly to Redis.

Bypasses Celery's kombu connection pool which can hang indefinitely on Windows
when ghost worker bindings exist. The messages are fully compatible with Celery
workers consuming from the same Redis broker.
"""

import json
import logging
import uuid

import redis

from app.config import CELERY_BROKER

logger = logging.getLogger(__name__)

# Must mirror task_routes in app/worker.py
_LIGHT_TASKS = {
    "tasks.ingest_radio",
    "tasks.primegov_discover",
    "tasks.primegov_download",
    "tasks.transcode_video",
    "tasks.retag_content",
    "tasks.export_clip",
    "tasks.cleanup_clips",
    "tasks.full_ingest",
}


def send_task(task_name: str, args: list | None = None, kwargs: dict | None = None,
              queue: str | None = None) -> str:
    """Push a task message to Redis in Celery's native format.

    If queue is None, auto-routes based on _LIGHT_TASKS mapping.
    Auto-starts the light worker if dispatching to the light queue
    and it isn't running.
    Returns the task ID (UUID string).
    """
    if queue is None:
        queue = "light" if task_name in _LIGHT_TASKS else "celery"

    task_id = str(uuid.uuid4())

    body = json.dumps([
        args or [],          # positional args
        kwargs or {},        # keyword args
        {"callbacks": None, "errbacks": None, "chain": None, "chord": None},
    ])

    headers = {
        "lang": "py",
        "task": task_name,
        "id": task_id,
        "shadow": None,
        "eta": None,
        "expires": None,
        "group": None,
        "group_index": None,
        "retries": 0,
        "timelimit": [None, None],
        "root_id": task_id,
        "parent_id": None,
        "argsrepr": repr(args or []),
        "kwargsrepr": repr(kwargs or {}),
        "origin": "api@dispatch",
        "ignore_result": False,
    }

    properties = {
        "correlation_id": task_id,
        "reply_to": "",
        "delivery_mode": 2,
        "delivery_info": {"exchange": "", "routing_key": queue},
        "priority": 0,
        "body_encoding": "utf-8",
        "delivery_tag": str(uuid.uuid4()),
    }

    message = json.dumps({
        "body": body,
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": headers,
        "properties": properties,
    })

    # Force IPv4 — on Windows 'localhost' resolves IPv6 first, can hang 20+ seconds
    broker_url = CELERY_BROKER.replace("localhost", "127.0.0.1")
    r = redis.from_url(broker_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        r.lpush(queue, message)
        logger.info("Dispatched %s [%s] to queue '%s'", task_name, task_id, queue)
    finally:
        r.close()

    # Auto-start light worker if needed (non-blocking, after task is in Redis)
    if queue == "light":
        try:
            from app.services.worker_manager import ensure_light_worker
            ensure_light_worker()
        except Exception as exc:
            logger.warning("Could not auto-start light worker: %s", exc)

    return task_id
