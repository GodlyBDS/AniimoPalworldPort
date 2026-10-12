"""Creature conversion: AssetRipper outputs + catalog skeleton -> rigged FBX for Unreal Engine 5.1.

For each skinned mesh of the creature (catalog ``skeleton.skins``):
  1. mesh GLB from the AssetRipper primary export (``asset.assetripper.primary``);
  2. bind poses from the UnityPy extract (``*.skin.json``), else the skeleton rest pose;
  3. skin assembly -> ``convert/<mesh>/<mesh>.rigged.glb``;
  4. Blender job -> ``convert/<mesh>/<mesh>.fbx`` with every confirmed animation clip
     (``.anim`` from the AssetRipper project export) retargeted onto the rig.

Variant materials and textures (Shiny/Prismana/regional) are applied as
material instances during the Unreal import (milestone 5), so one rigged mesh
serves all identities that share it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.convert.blender_runner import run_job
from aniimo_pal.convert.skin_assembly import assemble_skinned_glb, skeleton_locals
from aniimo_pal.convert.unity_anim import parse_anim_file
from aniimo_pal.errors import ToolkitError
from aniimo_pal.manifest.io import write_json_atomic

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _usable(asset: dict[str, Any], include_proposed: bool) -> bool:
    return asset.get("association") == "confirmed" or (include_proposed and asset.get("association") == "proposed")


def convert_creature(creature: dict[str, Any], creature_dir: str | Path, *, blender: str | None = None,
                     bone_map: dict[str, str] | None = None, include_proposed: bool = False) -> dict[str, Any]:
    creature_dir = Path(creature_dir)
    skeleton = creature.get("skeleton") or {}
    skins = skeleton.get("skins") or []
    if not skins:
        raise ToolkitError("creature has no skinned mesh in its skeleton (re-run catalog build with this toolkit "
                           "version and recreate the project, or add skeleton.skins)")
    locals_ = skeleton_locals(skeleton)
    assets = creature["assets"]
    by_object = {a["ref"].get("object"): (i, a) for i, a in enumerate(assets)}

    clips, clip_errors = [], []
    for i, a in enumerate(assets):
        if a["role"] != "animation" or not _usable(a, include_proposed):
            continue
        files = [f for f in (a.get("assetripper") or {}).get("project", []) if f.endswith(".anim")]
        if not files:
            clip_errors.append({"index": i, "name": a.get("name"), "error": "no .anim from AssetRipper project export"})
            continue
        try:
            clip = parse_anim_file(creature_dir / files[0])
            clip["asset_index"] = i
            clips.append(clip)
        except Exception as exc:  # keep converting other clips
            clip_errors.append({"index": i, "name": a.get("name"), "error": f"{type(exc).__name__}: {exc}"})

    outputs = []
    for skin in skins:
        entry = by_object.get(skin["mesh"])
        if entry is None or not _usable(entry[1], include_proposed):
            outputs.append({"mesh": skin.get("mesh_name"), "status": "skipped",
                            "error": "mesh asset not confirmed in this creature project"})
            continue
        idx, mesh_asset = entry
        glbs = [f for f in (mesh_asset.get("assetripper") or {}).get("primary", []) if f.lower().endswith(".glb")]
        if not glbs:
            outputs.append({"mesh": mesh_asset.get("name"), "status": "error",
                            "error": "no mesh GLB: run `creature export-assetripper --mode primary` first"})
            continue
        name = _SAFE.sub("_", mesh_asset.get("name") or f"mesh_{idx}")
        work = creature_dir / "convert" / name
        bind_poses = None
        if mesh_asset.get("extracted", "").endswith(".obj"):
            skin_json = creature_dir / (mesh_asset["extracted"][:-4] + ".skin.json")
            if skin_json.is_file():
                bind_poses = json.loads(skin_json.read_text(encoding="utf-8"))["bind_poses"]
        rec: dict[str, Any] = {"mesh": mesh_asset.get("name"), "asset_index": idx, "status": "ok"}
        try:
            rec["assembly"] = assemble_skinned_glb(creature_dir / glbs[0], locals_, skin["bones"],
                                                   work / f"{name}.rigged.glb", bind_poses=bind_poses,
                                                   root_name=creature["id"])
            report = run_job({"input_model": str(work / f"{name}.rigged.glb"), "output_fbx": str(work / f"{name}.fbx"),
                              "report": str(work / "blender-report.json"), "unity_skeleton": locals_,
                              "bone_map": bone_map or {}, "clips": clips}, blender=blender)
            rec["fbx"] = f"convert/{name}/{name}.fbx"
            rec["blender"] = {k: report.get(k) for k in ("blender_version", "basis", "meshes", "armatures", "bounds",
                                                         "warnings")}
            rec["clips"] = [{k: c.get(k) for k in ("name", "frames", "bones_driven", "unknown_paths",
                                                     "unmapped_paths", "max_head_error_m")} for c in report["clips"]]
            assets[idx]["converted"] = rec["fbx"]
            for c in clips:
                assets[c["asset_index"]]["converted"] = rec["fbx"]
        except Exception as exc:
            rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:800])
        outputs.append(rec)

    errors = sum(1 for o in outputs if o["status"] == "error") + len(clip_errors)
    if errors == 0 and any(o["status"] == "ok" for o in outputs) and creature.get("status") in ("mapped", "extracted"):
        creature["status"] = "converted"
    creature.setdefault("history", []).append({"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                               "action": "converted", "errors": errors})
    summary = {"meshes_ok": sum(1 for o in outputs if o["status"] == "ok"), "errors": errors,
               "clips": len(clips), "clip_errors": len(clip_errors)}
    doc = {"schema": "aniimo-pal/convert-summary", "schema_version": "0.1.0",
           "generator": {"name": TOOL_NAME, "version": __version__},
           "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "creature": creature["id"],
           "summary": summary, "meshes": outputs, "clip_errors": clip_errors,
           "notes": ["Variant materials/textures are applied as material instances at Unreal import (milestone 5).",
                     "Unreal import and in-game behaviour are not verified by this step."]}
    write_json_atomic(doc, creature_dir / "convert" / "report.json")
    return doc
