"""Heavy‑lifting functions: validation, repair, slicing.
These run inside Celery workers so they can safely block the CPU.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
import tempfile
from typing import List, Tuple
import json
import shutil
import trimesh
import pymeshlab as ml
import sys
import os
import psutil
from typing import Literal

from .config import BLENDER_BIN, BLENDER_SCRIPT, CURAENGINE_BIN, CURAENGINE_PROFILES_DIR, CURAENGINE_DEFINITIONS_DIR, SUPPORTED_EXTS
from typing import Callable, Tuple
SliceFn = Callable[[pathlib.Path, pathlib.Path], Tuple[pathlib.Path, str]]


logger = logging.getLogger(__name__)


def _log_mem(stage: str) -> None:
    proc = psutil.Process(os.getpid())
    rss_mb = proc.memory_info().rss / 1024 / 1024
    vm = psutil.virtual_memory()
    logger.info("[MEM] %s | pid_rss=%.0fMB | host_used=%.0fMB | host_avail=%.0fMB",
                stage, rss_mb, (vm.total - vm.available) / 1024 / 1024, vm.available / 1024 / 1024)

try:
    # NEW: 直接导入解析函数（更快、少开进程）
    from .gcode_status_parser import process_gcode_file as parse_gcode_status
except Exception:
    parse_gcode_status = None


# ---------------------------------------------------------------------------
# Low‑level helpers
# ---------------------------------------------------------------------------
def _run(cmd: List[str], cwd: str | pathlib.Path | None = None, env: dict | None = None) -> str:
    """Run external command *cmd* and raise *RuntimeError* on failure."""
    logger.info("$ %s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    logger.debug(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout or f"Command failed: {' '.join(cmd)}")
    return completed.stdout


# ---------------------------------------------------------------------------
# Individual stages
# ---------------------------------------------------------------------------
def _convert_to_format(
    src: pathlib.Path, target_format: str, output_dir: pathlib.Path
) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / ("converted." + target_format)
    mesh = trimesh.load(str(src), force="mesh")
    mesh.export(str(out_path))
    if not out_path.exists():
        raise RuntimeError(f"Conversion to {target_format} failed: {src}")
    logger.info("Converted %s → %s via trimesh: %s", src.suffix, target_format, out_path)
    return out_path


def _convert_with_trimesh(
    src: pathlib.Path, target_format: str, output_dir: pathlib.Path
) -> pathlib.Path:
    """Use trimesh for conversions involving GLB (PrusaSlicer cannot handle GLB)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"converted.{target_format}"
    mesh = trimesh.load(str(src), force="mesh")
    mesh.export(str(out_path))
    if not out_path.exists():
        raise RuntimeError(f"trimesh conversion to {target_format} failed")
    logger.info("Converted %s → %s via trimesh: %s", src.suffix, target_format, out_path)
    return out_path


# 🔁 NEW: 通用转换入口
def convert_model(
    src_path: pathlib.Path, target_ext: Literal[".stl", ".obj", ".3mf", ".glb"]
) -> pathlib.Path:
    src_ext = src_path.suffix.lower()
    job_root = src_path.parent  # ✅ 指向上传目录 /tmp/job_{hash}

    if src_ext == target_ext:
        return str(src_path)  # 不转换

    # GLB cannot be handled by PrusaSlicer — use trimesh for any GLB conversion
    if src_ext == ".glb" or target_ext == ".glb":
        res = _convert_with_trimesh(src_path, target_ext.lstrip("."), job_root / "converted")
        return {"converted_path": str(res)}

    # existing PrusaSlicer paths (stl/obj/3mf ↔ stl/obj/3mf)
    res = _convert_to_format(src_path, target_ext.lstrip("."), job_root / "converted")
    return {"converted_path": str(res)}


def validate(model_path: pathlib.Path) -> dict:
    """Headless Blender validation via user‑supplied script."""
    _log_mem(f"validate:start file={model_path.stat().st_size // 1024 // 1024}MB")
    raw_output = _run([BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--", str(model_path)])

    try:
        last_json_line = raw_output.strip().splitlines()[-1]  # 抽取最后一行
        return json.loads(last_json_line)  # 返回结构化 JSON
    except Exception as e:
        raise RuntimeError(f"Failed to parse Blender output: {e}")


def repair(src_path: pathlib.Path) -> pathlib.Path:
    """Clean geometry using trimesh + PyMeshLab, returns repaired OBJ."""
    _log_mem("repair:start")
    mesh = trimesh.load(src_path, force="mesh")
    tmp_ply = pathlib.Path(tempfile.mktemp(suffix=".ply"))
    mesh.export(tmp_ply)

    _log_mem("repair:after_trimesh_load")
    ms = ml.MeshSet()
    ms.load_new_mesh(str(tmp_ply))
    ms.apply_filter("meshing_repair_non_manifold_edges")
    ms.apply_filter("meshing_remove_duplicate_faces")
    ms.apply_filter("meshing_remove_unreferenced_vertices")
    ms.apply_filter("meshing_close_holes", maxholesize=1000)

    repaired_path = src_path.with_suffix(".repaired.stl")
    ms.save_current_mesh(str(repaired_path))
    tmp_ply.unlink(missing_ok=True)
    _log_mem("repair:done")
    return repaired_path


def slice_model(
    model_path: pathlib.Path, output_dir: pathlib.Path
) -> Tuple[pathlib.Path, str]:
    """Invoke CuraEngine CLI and return the generated G-code file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    profiles_dir = pathlib.Path(CURAENGINE_PROFILES_DIR)
    fdmprinter_def = pathlib.Path(CURAENGINE_DEFINITIONS_DIR) / "fdmprinter.def.json"
    custom_def = profiles_dir / "custom.def.json"
    out_gcode = output_dir / (model_path.stem + ".gcode")

    cmd = [
        CURAENGINE_BIN, "slice", "-v",
        "-j", str(fdmprinter_def),
        "-j", str(custom_def),
        "-l", str(model_path),
        "-o", str(out_gcode),
    ]

    env = os.environ.copy()
    env["CURA_ENGINE_SEARCH_PATH"] = CURAENGINE_DEFINITIONS_DIR

    _log_mem(f"slice:start file={model_path.stat().st_size // 1024 // 1024}MB")
    slicer_log = _run(cmd, env=env)
    _log_mem("slice:done")

    produced = list(output_dir.iterdir())
    if not produced:
        raise RuntimeError("Slicer produced no output files")

    for f in produced:
        if f.suffix.lower() in {".gcode", ".bgcode"}:
            return f, slicer_log
    return produced[0], slicer_log

# ---------------------------------------------------------------------------
# High‑level orchestration
# ---------------------------------------------------------------------------
from .metrics import StageTimer


def process_model(src_file: str, slice_fn: SliceFn | None = None) -> dict[str, str]:
    """Whole pipeline: validate ➜ repair ➜ slice. Returns path to slice."""
    timer = StageTimer()
    orig_path = pathlib.Path(src_file)
    suffix = orig_path.suffix.lower()

    if suffix not in SUPPORTED_EXTS:
        raise RuntimeError(f"Unsupported extension: {suffix}")

    job_root = orig_path.parent  # ✅ 指向上传目录 /tmp/job_{hash}
    src_path = orig_path  # /tmp/job_{hash}/file.ext

    timer.mark("start convert format and validate")

    # 若为 .3mf 先转换为 .obj
    cleanup_dir: pathlib.Path | None = None
    original_path = src_path
    original_suffix = suffix
    if suffix == ".3mf":
        converted_obj = _convert_to_format(src_path, "obj", job_root / "converted")
        cleanup_dir = converted_obj.parent  # 用于事后删除
        src_path = converted_obj
        suffix = ".obj"

    validate_report = validate(src_path)
    timer.mark("validate done")

    # CuraEngine only accepts .stl — always repair so output is .repaired.stl
    repaired = repair(src_path)
    timer.mark("repair done")
    logger.info("[process_model] repaired model: %s  (exists=%s, size=%s)", repaired, repaired.exists(), repaired.stat().st_size if repaired.exists() else 'N/A')
    
    target_dir = job_root / "sliced"
    if slice_fn is None:
        slice_path, slicer_log = slice_model(repaired, target_dir)
    else:
        slice_path, slicer_log = slice_fn(repaired, target_dir)
    timer.mark("slice done")

    validate_report["slicing_status"] = "SUCCESS"
    if "Low bed adhesion" in slicer_log:
        validate_report.setdefault("warnings", []).append(
            {
                "type": "SLICING",
                "message": "Detected print stability issues: Low bed adhesion. Consider enabling supports and brim.",
            }
        )

    gcode_status = None
    if slice_path.suffix.lower() in {".gcode", ".bgcode"}:
        try:
            if parse_gcode_status is not None:
                # 直接函数调用
                gcode_status = parse_gcode_status(str(slice_path))
            else:
                # 兜底：子进程执行 python -m app.gcode_status_parser <file.gcode>
                out = _run(
                    [sys.executable, "-m", "app.gcode_status_parser", str(slice_path)]
                )
                # 解析器通常整段输出为 JSON；若有日志，取最后一行或整体尝试
                text = out.strip()
                try:
                    gcode_status = json.loads(text.splitlines()[-1])
                except Exception:
                    gcode_status = json.loads(text)
        except Exception as e:
            logger.exception("G-code status parse failed: %s", e)
            gcode_status = {"ok": False, "error": str(e)}
    validate_report["slice_report"] = gcode_status

    if cleanup_dir and cleanup_dir.exists():
        shutil.rmtree(cleanup_dir, ignore_errors=True)

    metrics = timer.snapshot()
    print(metrics)

    return {
        "slice_path": str(slice_path),
        "validate_report": validate_report,
    }
