"""Celery application and task definitions (restored)."""

from __future__ import annotations

import os
import shutil, logging
from pathlib import Path
from typing import Tuple
from celery import Celery
from kombu import Queue

from .config import BROKER_URL, RESULT_BACKEND, RESULT_TTL_SECONDS
from .pipeline import (
    validate, repair, slice_model, _convert_to_format, convert_model
)
from .gcode_status_parser import process_gcode_file as parse_gcode_status 
# 一些稳定性环境变量（也可在 docker-compose 里设）
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

celery_app = Celery("pipeline", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_track_started=True,
    acks_late=True,

    # ✅ 把默认队列从 Celery 的 "celery" 改为你自己的 "default"
    task_default_queue="default",

    # ✅ 明确声明两个队列，并给各自 routing_key（与 Redis 不是强绑定，但可读性&可移植性更好）
    task_queues=(
        Queue("default", routing_key="default"),
        Queue("slice",   routing_key="slice"),
    ),

    # ✅ 路由规则：只把 slice_task 丢到 slice 队列；其他任务走 default
    task_routes={
        "app.tasks.slice_task": {"queue": "slice", "routing_key": "slice"},
        # 可选：显式指定 run_pipeline_task / convert_model_task 走 default
        "app.tasks.run_pipeline_task": {"queue": "default", "routing_key": "default"},
        "app.tasks.convert_model_task": {"queue": "default", "routing_key": "default"},
    },

    # 可选：Redis 可见性超时，防止 worker 崩溃后消息永久丢失
    broker_transport_options={"visibility_timeout": 3600},
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Cleanup task
# ---------------------------------------------------------------------------
@celery_app.task(name="app.tasks.cleanup_job_dir")
def cleanup_job_dir(job_dir: str) -> None:
    """Delete an entire job directory after TTL expires."""
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.info("Auto-cleaned job dir: %s", job_dir)
    except Exception:
        logger.exception("Failed to clean job dir: %s", job_dir)


# --- 专用切片任务：绑定到 "slice" 队列，返回 (gcode_path, slicer_log) ---------------
@celery_app.task(name="app.tasks.slice_task", queue="slice", bind=True)
def slice_task(self, model_path: str, output_dir: str) -> Tuple[str, str]:
    from .pipeline import slice_model
    out_path, log = slice_model(Path(model_path), Path(output_dir))
    return str(out_path), log

# --- 切片完成后的收尾任务：拼装 validate_report、解析 gcode、清理等 -------------------
@celery_app.task(name="app.tasks.finalize_after_slice", bind=True)
def finalize_after_slice(self, slice_result: Tuple[str, str], context: dict) -> dict:
    """
    slice_result: (out_path_str, slicer_log)
    context: {
      "validate_report": dict,
      "cleanup_dir": str | None,
      "original_path": str,
      "skipped_repair": bool
    }
    """
    out_path_str, slicer_log = slice_result
    validate_report = context.get("validate_report", {}) or {}
    cleanup_dir = context.get("cleanup_dir")
    # 写入切片日志/告警
    validate_report["slicing_status"] = "SUCCESS"
    if "Low bed adhesion" in (slicer_log or ""):
        validate_report.setdefault("warnings", []).append({
            "type": "SLICING",
            "message": "Detected print stability issues: Low bed adhesion. Consider enabling supports and brim.",
        })

    # 解析 G-code 状态
    gcode_status = None
    try:
        from .gcode_status_parser import process_gcode_file as parse_gcode_status
        gcode_status = parse_gcode_status(out_path_str)
    except Exception as e:
        logger.exception("G-code status parse failed: %s", e)
        gcode_status = {"ok": False, "error": str(e)}

    validate_report["slice_report"] = gcode_status

    # 清理临时转换目录
    if cleanup_dir:
        try:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        except Exception:
            logger.exception("cleanup failed for %s", cleanup_dir)

    # 立即删除上传的原始文件和修复中间文件（gcode 仍保留供下载）
    original_path = context.get("original_path")
    if original_path:
        p = Path(original_path)
        p.unlink(missing_ok=True)
        p.with_suffix(".repaired.stl").unlink(missing_ok=True)

    # TTL 到期后删除整个 job 目录（包含 gcode）
    job_dir = Path(out_path_str).parent.parent
    cleanup_job_dir.apply_async(args=[str(job_dir)], countdown=RESULT_TTL_SECONDS)
    logger.info("Scheduled cleanup of %s in %ds", job_dir, RESULT_TTL_SECONDS)

    return {
        "slice_path": out_path_str,
        "validate_report": validate_report,
    }

# --- 顶层流水线任务：不再等待子任务，交棒给 chain -----------------------------------
@celery_app.task(bind=True, max_retries=3, soft_time_limit=3600, time_limit=3900,
                 name="app.tasks.run_pipeline_task")
def run_pipeline_task(self, src_file: str) -> dict:
    """
    顶层编排：convert(.3mf→obj) → validate → (可选) repair → 交给 slice_task(queue='slice') → finalize_after_slice
    这里不再同步等待切片结果，而是用 self.replace(slice_sig | finalize_sig)。
    """
    from .pipeline import StageTimer
    timer = StageTimer()

    src_path = Path(src_file)
    suffix = src_path.suffix.lower()
    if suffix not in {".stl", ".obj", ".3mf", ".glb"}:
        raise self.retry(exc=RuntimeError(f"Unsupported extension: {suffix}"), countdown=5)

    job_root = src_path.parent
    original_path = src_path
    original_suffix = suffix

    # 若为 .3mf 先转 obj（或你原来的策略）
    cleanup_dir = None
    if suffix == ".3mf":
        converted_obj = _convert_to_format(src_path, "obj", job_root / "converted")
        cleanup_dir = converted_obj.parent
        src_path = converted_obj

    # validate
    validate_report = validate(src_path)

    # CuraEngine only accepts .stl — always repair so output is .repaired.stl
    repaired = repair(src_path)
    logger.info("[run_pipeline_task] repaired model: %s  (exists=%s, size=%s)", repaired, repaired.exists(), repaired.stat().st_size if repaired.exists() else 'N/A')

    # 交由 slice 专用队列（串行化）
    target_dir = job_root / "sliced"
    slice_sig = slice_task.s(str(repaired), str(target_dir)).set(queue="slice")

    # 把必要上下文传给 finalize
    context = {
        "validate_report": validate_report,
        "cleanup_dir": str(cleanup_dir) if cleanup_dir else None,
        "original_path": str(original_path),
    }
    finalize_sig = finalize_after_slice.s(context)

    # 交棒：父任务被替换为 chain 的最后结果（对外 task_id 不变）
    raise self.replace(slice_sig | finalize_sig)

@celery_app.task(bind=True, max_retries=3, soft_time_limit=900, time_limit=1200, name="app.tasks.convert_model_task")
def convert_model_task(self, src_file: str, target_ext: str) -> str:
    """
    Convert model to target_ext ('.stl' | '.obj' | '.3mf').
    Returns absolute path of converted file.
    """
    try:
        result = convert_model(Path(src_file), target_ext)
        # 立即删除原始上传文件
        Path(src_file).unlink(missing_ok=True)
        # TTL 到期后删除整个 job 目录
        job_dir = Path(src_file).parent
        cleanup_job_dir.apply_async(args=[str(job_dir)], countdown=RESULT_TTL_SECONDS)
        logger.info("Scheduled cleanup of %s in %ds", job_dir, RESULT_TTL_SECONDS)
        return result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5, max_retries=2)


