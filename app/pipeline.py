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
from typing import Literal

from .config import BLENDER_BIN, BLENDER_SCRIPT, PRUSASLICER_BIN, SUPPORTED_EXTS

logger = logging.getLogger(__name__)

try:
    # NEW: 直接导入解析函数（更快、少开进程）
    from .gcode_status_parser import process_gcode_file as parse_gcode_status
except Exception:
    parse_gcode_status = None


# ---------------------------------------------------------------------------
# Low‑level helpers
# ---------------------------------------------------------------------------
def _run(cmd: List[str], cwd: str | pathlib.Path | None = None) -> str:
    """Run external command *cmd* and raise *RuntimeError* on failure."""
    logger.info("$ %s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=cwd,
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
    obj_path = output_dir / ("converted." + target_format)

    result = subprocess.run(
        [
            PRUSASLICER_BIN,
            ("--export-" + target_format), 
            "--output",
            str(obj_path),  
            str(src), 
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not obj_path.exists():
        raise RuntimeError(f"3MF→OBJ conversion failed:\n{result.stdout}")

    logger.info("Converted 3MF → OBJ: %s", obj_path)
    return obj_path


# 🔁 NEW: 通用转换入口
def convert_model(
    src_path: pathlib.Path, target_ext: Literal[".stl", ".obj", ".3mf"]
) -> pathlib.Path:
    src_ext = src_path.suffix.lower()
    job_root = src_path.parent  # ✅ 指向上传目录 /tmp/job_{hash}

    if src_ext == target_ext:
        return str(src_path)  # 不转换

    # 1) to 3mf
    if target_ext == ".3mf":
        res = _convert_to_format(src_path, "3mf", job_root / "converted")
        return {"converted_path": str(res)}

    # 2) to stl
    if target_ext == ".stl":
        res = _convert_to_format(src_path, "stl", job_root / "converted")
        return {"converted_path": str(res)}

    # 3) to obj
    if target_ext == ".obj":
        res = _convert_to_format(src_path, "obj", job_root / "converted")
        return {"converted_path": str(res)}

    raise RuntimeError(f"Unsupported conversion: {src_ext} → {target_ext}")


def validate(model_path: pathlib.Path) -> dict:
    """Headless Blender validation via user‑supplied script."""
    raw_output = _run([BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--", str(model_path)])

    try:
        last_json_line = raw_output.strip().splitlines()[-1]  # 抽取最后一行
        return json.loads(last_json_line)  # 返回结构化 JSON
    except Exception as e:
        raise RuntimeError(f"Failed to parse Blender output: {e}")


def repair(src_path: pathlib.Path) -> pathlib.Path:
    """Clean geometry using trimesh + PyMeshLab, returns repaired OBJ."""

    mesh = trimesh.load(src_path, force="mesh")
    tmp_ply = pathlib.Path(tempfile.mktemp(suffix=".ply"))
    mesh.export(tmp_ply)

    ms = ml.MeshSet()
    ms.load_new_mesh(str(tmp_ply))
    ms.apply_filter("meshing_repair_non_manifold_edges")
    ms.apply_filter("meshing_remove_duplicate_faces")
    ms.apply_filter("meshing_remove_unreferenced_vertices")
    ms.apply_filter("meshing_close_holes", maxholesize=1000)

    repaired_path = src_path.with_suffix(".repaired.stl")
    ms.save_current_mesh(str(repaired_path))
    tmp_ply.unlink(missing_ok=True)
    return repaired_path


def slice_model(
    model_path: pathlib.Path, output_dir: pathlib.Path
) -> Tuple[pathlib.Path, str]:
    """Invoke Bambu Studio CLI and return the generated slice (G‑code/3MF)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # ✅ 修正这里：不要写成 "app/app/no_bgcode.ini"
    profile = _resolve_profile("no_bgcode.ini")   # 放在 app/no_bgcode.ini 即可
    out_gcode = output_dir / (model_path.stem + ".gcode")

    cmd = [PRUSASLICER_BIN]
    if profile is not None:
        cmd += ["--load", str(profile)]
    # 用规范参数导出 G-code（PrusaSlicer）
    cmd += [
        "--export-gcode",
        "--output",
        str(out_gcode),   # 直接指定目标文件名，避免遍历目录找
        str(model_path),
    ]

    slicer_log = _run(cmd)

    produced = list(output_dir.iterdir())
    if not produced:
        raise RuntimeError("Slicer produced no output files")

    for f in produced:
        if f.suffix.lower() in {".gcode", ".bgcode"}:
            return f, slicer_log
    return produced[0], slicer_log


def _resolve_profile(fname: str) -> pathlib.Path | None:
    """
    尝试解析切片配置文件的位置，依次：
    1) 绝对路径（如果传进来的就是绝对路径）
    2) 与本文件同目录（app/no_bgcode.ini）
    3) 项目根目录（app 的上一级）下的 app/<fname>
    找不到则返回 None
    """
    p = pathlib.Path(fname)
    if p.is_absolute() and p.exists():
        return p

    here = pathlib.Path(__file__).resolve().parent              # .../app
    cand1 = here / fname                                        # app/no_bgcode.ini
    if cand1.exists():
        return cand1

    proj_root = here.parent                                     # 项目根
    cand2 = proj_root / "app" / fname                           # <root>/app/no_bgcode.ini
    if cand2.exists():
        return cand2

    return None

# ---------------------------------------------------------------------------
# High‑level orchestration
# ---------------------------------------------------------------------------
from .metrics import StageTimer


def process_model(src_file: str) -> dict[str, str]:
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

    if original_suffix != ".3mf":
        repaired = repair(src_path)
        timer.mark("repair done")
    else:
        # skip fix for .3mf file for now
        repaired = original_path

    slice_path, slicer_log = slice_model(repaired, job_root / "sliced")
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
