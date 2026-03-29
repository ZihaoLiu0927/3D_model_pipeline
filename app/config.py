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
CURAENGINE_BIN: str = os.getenv(
    "CURAENGINE_BIN",
    "/Applications/UltiMaker Cura.app/Contents/Resources/CuraEngine",
)
CURAENGINE_PROFILES_DIR: str = os.getenv(
    "CURAENGINE_PROFILES_DIR",
    str(Path(__file__).parent / "profiles"),
)
# 内置 definitions 搜索路径（fdmprinter.def.json 等基础文件所在目录）
CURAENGINE_DEFINITIONS_DIR: str = os.getenv(
    "CURAENGINE_DEFINITIONS_DIR",
    "/Applications/UltiMaker Cura.app/Contents/Resources/share/cura/resources/definitions",
)

MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
SUPPORTED_EXTS: set[str] = {".obj", ".stl", ".glb", ".gltf", ".3mf", ".glb"}
SUPPORTED_EXTS_CONVERT: set[str] = {".stl", ".obj", ".3mf", ".glb"}

BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/0")

UPLOAD_ROOT: Path = Path(os.getenv("UPLOAD_ROOT", "/tmp"))
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Seconds to keep result files before auto-cleanup. Default 1 hour.
RESULT_TTL_SECONDS: int = int(os.getenv("RESULT_TTL_SECONDS", "3600"))
