"""Celery application and task definitions (restored)."""

from __future__ import annotations

import os
from celery import Celery

from .config import BROKER_URL, RESULT_BACKEND
from .pipeline import process_model
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

celery_app = Celery("pipeline", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(task_track_started=True, acks_late=True)

# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=3, soft_time_limit=3600, time_limit=3900)
def run_pipeline_task(self, src_file: str) -> dict[str, str]:
    """Validate → repair → slice. Returns absolute path to slice file."""
    return process_model(src_file)
