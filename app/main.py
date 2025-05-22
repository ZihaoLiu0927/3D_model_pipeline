"""FastAPI entrypoint – lightweight HTTP layer only (fixed file‑save bug).
Queues heavy 3D jobs to Celery and serves final files when ready.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from celery.result import AsyncResult

from .config import MAX_FILE_SIZE_MB, SUPPORTED_EXTS, UPLOAD_ROOT
from .tasks import run_pipeline_task, celery_app

app = FastAPI(title="3D-Model Compliance & Slicing API", version="2.2-celery-mod")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_upload(upload: UploadFile, dest: Path) -> None:
    """Blocking IO helper – runs in threadpool via ``asyncio.to_thread``."""
    with dest.open("wb") as fp:
        shutil.copyfileobj(upload.file, fp)
    upload.file.close()


@app.post("/process", summary="Upload a 3D model and enqueue the slicing job")
async def process(file: UploadFile = File(...)):
    """This function processes the uploaded file and enqueues the slicing job."""
    # Basic guards -----------------------------------------------------------
    if file.size and file.size > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, detail="File too large")

    suffix = Path(file.filename or "model").suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        raise HTTPException(400, detail="Unsupported extension")

    # Persist file (offloaded to thread) ------------------------------------
    work_dir = UPLOAD_ROOT / f"job_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / f"input{suffix}"
    await asyncio.to_thread(_save_upload, file, raw_path)

    # Enqueue Celery task ----------------------------------------------------
    task = run_pipeline_task.delay(str(raw_path))
    return {"task_id": task.id}


@app.get(
    "/result/{task_id}", summary="Check job status and get results when job finished"
)
def get_result(task_id: str):
    """This function checks job status and get results when job finished."""
    res: AsyncResult = AsyncResult(task_id, app=celery_app)

    if res.state == "SUCCESS":
        result_dict = res.result or {}
        return JSONResponse(
            {
                "state": "SUCCESS",
                "validate_log": result_dict.get("validate_log"),
                "slicer_log": result_dict.get("slicer_log"),
            }
        )

    if res.state in {"PENDING", "STARTED"}:
        return JSONResponse({"state": res.state, "progress": res.info or None})

    return JSONResponse({"state": res.state, "error": str(res.info)}, status_code=400)


@app.get(
    "/result/{task_id}/file",
    summary="Download finished slice",
    name="download_slice",
)
def download_slice(task_id: str):
    """
    仅当任务 state == SUCCESS 时允许下载切片文件；
    否则返回 404 / 410。
    """
    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    if res.state != "SUCCESS":
        raise HTTPException(404, "File not ready")

    path = Path(res.result["slice_path"])
    if not path.exists():
        raise HTTPException(410, "Result file expired or purged")

    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )
