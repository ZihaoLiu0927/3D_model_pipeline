"""Centralised configuration & constants.
All tunables come from environment variables so the same image can be reused in
DEV, STAGE and PROD without code changes.
"""

from __future__ import annotations

import os
from pathlib import Path

BLENDER_BIN: str = os.getenv(
    "BLENDER_BIN", "/Applications/Blender.app/Contents/MacOS/Blender"
)
BLENDER_SCRIPT: str = os.getenv("BLENDER_SCRIPT", "app/validate.py")
BAMBUSTUDIO_BIN: str = os.getenv("BAMBUSTUDIO_BIN", "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")

MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
SUPPORTED_EXTS: set[str] = {".obj", ".stl", ".glb", ".gltf", ".3mf"}

BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/0")

UPLOAD_ROOT: Path = Path(os.getenv("UPLOAD_ROOT", "/tmp"))
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
