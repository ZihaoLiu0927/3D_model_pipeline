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

from .config import MAX_FILE_SIZE_MB, SUPPORTED_EXTS, SUPPORTED_EXTS_CONVERT, UPLOAD_ROOT
from .tasks import run_pipeline_task, convert_model_task, celery_app

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
        payload = res.result or {}
        if "validate_report" in payload:
            # slicing / validate 流
            return JSONResponse({"state": "SUCCESS", **payload["validate_report"]})
        elif "converted_path" in payload:
            return JSONResponse({"state": "SUCCESS"})
        else:
            return JSONResponse({"state": "SUCCESS", "result": payload})

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
    
    payload = res.result or {}
    if "validate_report" in payload:
        path = Path(payload["slice_path"])
        if not path.exists():
            raise HTTPException(410, "Result file expired or purged")
    elif "converted_path" in payload:
        path = Path(payload["converted_path"])
        if not path.exists():
            raise HTTPException(410, "Result file expired or purged")
    else:
        raise HTTPException(410, "Result file expired or purged")
    
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )
    
@app.post("/convert", summary="Convert 3D model to another format")
async def convert(
    target_format: str,
    file: UploadFile = File(...),
):
    """
    上传模型并转换成目标格式。
    参数 `target_format` 必须是 stl | obj | 3mf（不区分大小写）。
    """
    target_ext = f".{target_format.lower()}"
    if target_ext not in {".stl", ".obj", ".3mf"}:
        raise HTTPException(400, "target_format must be stl, obj or 3mf")

    # 大小/扩展名校验同 /process 端点
    if file.size and file.size > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, "File too large")

    suffix = Path(file.filename or "model").suffix.lower()
    if suffix not in SUPPORTED_EXTS_CONVERT:
        raise HTTPException(400, "Unsupported source extension")

    # 保存上传
    work_dir = UPLOAD_ROOT / f"job_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / f"input{suffix}"
    await asyncio.to_thread(_save_upload, file, raw_path)

    # 入队
    task = convert_model_task.delay(str(raw_path), target_ext)
    return {"task_id": task.id}
