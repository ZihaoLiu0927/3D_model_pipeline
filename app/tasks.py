"""Celery application and task definitions (restored)."""

from __future__ import annotations

import os
from pathlib import Path
from celery import Celery

from .config import BROKER_URL, RESULT_BACKEND
from .pipeline import process_model, convert_model
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
    try:
        return process_model(src_file)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5, max_retries=2)

@celery_app.task(bind=True, max_retries=3, soft_time_limit=900, time_limit=1200)
def convert_model_task(self, src_file: str, target_ext: str) -> str:
    """
    Convert model to target_ext ('.stl' | '.obj' | '.3mf').
    Returns absolute path of converted file.
    """
    try:
        return convert_model(Path(src_file), target_ext)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5, max_retries=2)
