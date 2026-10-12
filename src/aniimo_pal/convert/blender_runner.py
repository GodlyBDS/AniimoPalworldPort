"""Run Blender jobs from the toolkit.

Two modes, same script (``blender_scripts/convert_model.py``):

* External Blender (normal use): ``blender -b --factory-startup --python <script> -- job.json``.
* In-process ``bpy`` module (tests/CI, or a Python where ``pip install bpy`` works).
  Note: the in-process mode resets bpy's scene for every job.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ToolkitError


class BlenderJobError(ToolkitError):
    """A Blender job failed."""


def script_path(name: str = "convert_model.py") -> Path:
    return Path(str(resources.files("aniimo_pal").joinpath(f"convert/blender_scripts/{name}")))


def bpy_available() -> bool:
    return importlib.util.find_spec("bpy") is not None


def run_job(job: dict[str, Any], *, blender: str | None = None, timeout: float = 1800) -> dict[str, Any]:
    """Run a conversion job; returns the report dict."""
    if blender:
        return _run_external(job, blender, timeout)
    if bpy_available():
        return _run_in_process(job)
    raise BlenderJobError("Blender not available: pass --blender PATH (Blender 3.6+) or install the bpy module")


def _run_in_process(job: dict[str, Any]) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("aniimo_pal_blender_convert_model", script_path())
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        return module.run(job)
    except Exception as exc:
        raise BlenderJobError(f"Blender job failed: {type(exc).__name__}: {exc}") from exc


def _run_external(job: dict[str, Any], blender: str, timeout: float) -> dict[str, Any]:
    job = dict(job)
    with tempfile.TemporaryDirectory(prefix="aniimo-pal-job-") as tmp:
        job.setdefault("report", str(Path(tmp) / "report.json"))
        job_file = Path(tmp) / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        cmd = [blender, "-b", "--factory-startup", "--python", str(script_path()), "--", str(job_file)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BlenderJobError(f"cannot run Blender: {exc}") from exc
        try:
            report = json.loads(Path(job["report"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
        if proc.returncode != 0 or report is None or "error" in report:
            detail = (report or {}).get("error") or proc.stderr[-2000:] or proc.stdout[-2000:]
            raise BlenderJobError(f"Blender job failed (exit {proc.returncode}): {detail}")
        return report
