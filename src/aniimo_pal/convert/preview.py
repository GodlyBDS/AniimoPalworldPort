"""Build a Blender preview scene of a converted creature: every variant side by side, every animation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from aniimo_pal.convert.blender_runner import run_job, script_path
from aniimo_pal.errors import ToolkitError


def variant_label(form: str, appearance: str) -> str:
    base = "Prismana" if form == "prismana" else "Normal"
    if appearance == "shiny":
        return f"{'Prismana ' if base == 'Prismana' else ''}Shiny (look not identified)"
    if appearance.startswith("shiny_"):
        return f"{base} Shiny {int(appearance.split('_')[1])}" if base == "Prismana" else f"Shiny {int(appearance.split('_')[1])}"
    return base


def _material_main_texture(creature_dir: Path, material_asset: dict[str, Any]) -> str | None:
    """Albedo texture file of one extracted material (UnityPy extract JSON)."""
    if not material_asset.get("extracted"):
        return None
    doc = json.loads((creature_dir / material_asset["extracted"]).read_text(encoding="utf-8"))
    textures = doc.get("textures") or {}
    for slot in ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo"):
        if (textures.get(slot) or {}).get("file"):
            return str(creature_dir / "extract" / "unitypy" / textures[slot]["file"])
    for tex in textures.values():
        if tex.get("file"):
            return str(creature_dir / "extract" / "unitypy" / tex["file"])
    return None


def _part_textures(creature: dict[str, Any], creature_dir: Path, prefab: dict[str, Any]) -> dict[str, str]:
    """{mesh name: albedo texture} for each skinned part of this prefab, from its renderer's own material."""
    by_object = {(a.get("ref") or {}).get("object"): a for a in creature["assets"] if a["role"] == "material"}
    out = {}
    for part in prefab.get("parts") or []:
        for mat_id in part.get("materials") or []:
            mat = by_object.get(mat_id)
            tex = _material_main_texture(creature_dir, mat) if mat and mat.get("association") != "rejected" else None
            if tex and part.get("mesh_name"):
                out[part["mesh_name"]] = tex
                break
    return out


def _main_texture(creature: dict[str, Any], creature_dir: Path, form: str, appearance: str) -> str | None:
    """Main (albedo) texture file of the material that belongs to this identity, from the UnityPy extract."""
    best = None
    for a in creature["assets"]:
        if a["role"] != "material" or a.get("association") != "confirmed" or not a.get("extracted"):
            continue
        f_ok = a.get("form") in (form, "shared")
        app = a.get("appearance", "any")
        a_ok = app in (appearance, "any") or (app == "shiny" and appearance.startswith("shiny"))
        if not (f_ok and a_ok):
            continue
        score = (a.get("form") == form) + (app == appearance)
        if best is None or score > best[0]:
            best = (score, a)
    if best is None:
        return None
    doc = json.loads((creature_dir / best[1]["extracted"]).read_text(encoding="utf-8"))
    for slot in ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo"):
        tex = (doc.get("textures") or {}).get(slot) or {}
        if tex.get("file"):
            return str(creature_dir / "extract" / "unitypy" / tex["file"])
    for tex in (doc.get("textures") or {}).values():
        if tex.get("file"):
            return str(creature_dir / "extract" / "unitypy" / tex["file"])
    return None


def preview_job(creature: dict[str, Any], creature_dir: str | Path) -> dict[str, Any]:
    creature_dir = Path(creature_dir).resolve()  # Blender runs from another working directory
    fbx = next((a.get("converted") for a in creature["assets"] if a["role"] == "mesh" and a.get("converted")), None)
    if not fbx or not (creature_dir / fbx).is_file():
        raise ToolkitError("no converted FBX yet: run `creature convert` first")
    order = {"normal": 0, "prismana": 1}
    prefabs = sorted((p for p in creature.get("prefabs", []) if p.get("form") in order),
                     key=lambda p: (order[p["form"]], p.get("appearance") != "standard", p.get("appearance", "")))
    variants, seen = [], set()
    for p in prefabs:
        app = p.get("appearance", "standard")
        # Unidentified Shiny prefabs: one preview per distinct material set (the data may hold several looks).
        sig = tuple(sorted({m for part in p.get("parts") or [] for m in part.get("materials") or []})) \
            if app == "shiny" else None
        key = (p["form"], app, sig)
        if key in seen:
            continue
        seen.add(key)
        label = variant_label(p["form"], app)
        if app == "shiny" and sum(1 for q in prefabs if q.get("appearance") == "shiny" and q["form"] == p["form"]) > 1:
            label += f" [{p.get('name')}]"
        part_textures = _part_textures(creature, creature_dir, p)
        texture = _main_texture(creature, creature_dir, p["form"], app)
        missing = [{"part": part.get("mesh_name"), "reason": part.get("materials_missing") or
                    "material has no extracted texture"}
                   for part in p.get("parts") or [] if part.get("mesh_name") not in part_textures
                   and part.get("state") in (None, "default")]
        if not part_textures and not texture:
            label += " - NO TEXTURE"
        variants.append({"label": label, "prefab": p.get("name"), "texture": texture, "part_textures": part_textures,
                         "missing_textures": missing})
    if not variants:
        texture = _main_texture(creature, creature_dir, "normal", "standard")
        variants = [{"label": "Normal" + ("" if texture else " - NO TEXTURE"), "texture": texture,
                     "missing_textures": []}]
    out = creature_dir / "preview"
    return {"fbx": str(creature_dir / fbx), "variants": variants, "output_blend": str(out / "preview.blend"),
            "render": str(out / "preview.png"), "report": str(out / "preview-report.json")}


def build_preview(creature: dict[str, Any], creature_dir: str | Path, *, blender: str | None = None,
                  render: bool = True) -> dict[str, Any]:
    job = preview_job(creature, creature_dir)
    png = job.pop("render")
    Path(job["output_blend"]).parent.mkdir(parents=True, exist_ok=True)
    report = run_job(job, blender=blender, script="preview_scene.py")
    if render and blender:
        report["render"], report["render_error"] = render_still(blender, report["blend"], png, report["render_frame"])
    elif render:
        report["render_error"] = "still image needs a Blender executable (in-process bpy skips rendering)"
    Path(job["report"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def render_still(blender: str, blend: str, png: str, frame: int, timeout: float = 600) -> tuple[str | None, str | None]:
    """Render a still in a separate Blender process; returns (png or None, error or None)."""
    cmd = [blender, "-b", blend, "--factory-startup", "--python", str(script_path("render_still.py")), "--", png,
           str(frame)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot run Blender: {exc}"
    if proc.returncode == 0 and Path(png).is_file():
        return png, None
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    return None, "rendering failed (this machine may lack OpenGL): " + " | ".join(tail)
