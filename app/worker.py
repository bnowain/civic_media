"""
Celery worker entry point.
Import this module to get a configured Celery application instance.
"""

from celery import Celery

from app.config import CELERY_BACKEND, CELERY_BROKER

celery_app = Celery(
    "civic_media",
    broker=CELERY_BROKER,
    backend=CELERY_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # One task at a time per worker — ML models are memory-intensive
    worker_concurrency=1,
    # Acknowledge tasks only after completion (safer for long jobs)
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Keep results for 24 hours
    result_expires=86400,
)
