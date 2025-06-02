"""Heavy‑lifting functions: validation, repair, slicing.
These run inside Celery workers so they can safely block the CPU.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
import tempfile
from typing import List, Tuple
import trimesh
import json
import pymeshlab as ml

from .config import BLENDER_BIN, BLENDER_SCRIPT, PRUSASLICER_BIN, SUPPORTED_EXTS

logger = logging.getLogger(__name__)

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


def validate(model_path: pathlib.Path) -> dict:
    """Headless Blender validation via user‑supplied script."""
    raw_output = _run([BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--", str(model_path)])
    print("raw_output： ", raw_output)
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
    slicer_log = _run(
        [
            PRUSASLICER_BIN,
            "--gcode",
            "--output",
            str(output_dir),
            str(model_path),
        ]
    )

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

def process_model(src_file: str) -> dict[str, str]:
    """Whole pipeline: validate ➜ repair ➜ slice. Returns path to slice."""
    src_path = pathlib.Path(src_file)
    suffix = src_path.suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        raise RuntimeError(f"Unsupported extension: {suffix}")

    work_root = src_path.parent
    validate_report  = validate(src_path)
    repaired = repair(src_path)
    slice_path, slicer_log = slice_model(repaired, work_root / "sliced")

    validate_report["slicing_status"] = "SUCCESS"
    if "Low bed adhesion" in slicer_log: 
        validate_report.setdefault("warnings", []).append({
            "type": "SLICING",
            "message": "Detected print stability issues: Low bed adhesion. Consider enabling supports and brim.",
        })

    return {
        "slice_path": str(slice_path),
        "validate_report": validate_report,
    }
