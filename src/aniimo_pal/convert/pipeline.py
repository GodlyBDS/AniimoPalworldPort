"""Creature conversion: AssetRipper outputs + catalog skeleton -> rigged FBX for Unreal Engine 5.1.

For every skinned part of the creature (catalog ``skeleton.skins``: body, eyes, ...):
  1. mesh GLB written by ``creature extract`` (else AssetRipper's primary export);
  2. bind poses from the UnityPy extract (``*.skin.json``), else the skeleton rest pose;
     parts without skin data follow their renderer's transform;
then all parts are assembled on one skeleton -> ``convert/<creature>/<creature>.rigged.glb`` and a
Blender job writes ``convert/<creature>/<creature>.fbx`` (one skeletal mesh) with every confirmed animation
clip (``.anim`` from the AssetRipper project export) retargeted onto the rig.

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
from aniimo_pal.convert.skin_assembly import assemble_creature_glb, skeleton_locals
from aniimo_pal.convert.unity_anim import parse_anim_file
from aniimo_pal.convert.unity_mesh_glb import glb_is_rigid
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
        unresolved = skeleton.get("unresolved_parts") or []
        if unresolved:
            detail = "; ".join(f"{u['renderer']} ({len(u['candidates'])} candidate meshes: "
                               f"{', '.join(u['candidates'][:3]) or 'none'})" for u in unresolved[:6])
            raise ToolkitError("the creature's renderers use a runtime placeholder mesh and the real meshes could "
                               f"not be identified: {detail}")
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

    # One rigged model per creature: every skinned part (body, eyes, ...) on one shared skeleton.
    outputs, parts = [], []
    identity = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    any_active = any(s.get("active", True) for s in skins)
    for skin in skins:
        if any_active and skin.get("active") is False:
            outputs.append({"mesh": skin.get("mesh_name"), "status": "skipped",
                            "error": "hidden by default in the prefab (alternative state the game switches on, "
                                     "e.g. a Rampage look); not part of the base model"})
            continue
        entry = by_object.get(skin["mesh"])
        if entry is None or not _usable(entry[1], include_proposed):
            outputs.append({"mesh": skin.get("mesh_name"), "status": "skipped",
                            "error": "mesh asset not confirmed in this creature project"})
            continue
        idx, mesh_asset = entry
        rec: dict[str, Any] = {"mesh": mesh_asset.get("name"), "asset_index": idx, "status": "ok"}
        # Mesh GLB: the toolkit's own (written by `creature extract`), else AssetRipper's primary export.
        own = mesh_asset.get("extracted", "")
        own_glb = own[:-4] + ".glb" if own.endswith(".obj") else ""
        glbs = [own_glb] if own_glb and (creature_dir / own_glb).is_file() else []
        glbs += [f for f in (mesh_asset.get("assetripper") or {}).get("primary", []) if f.lower().endswith(".glb")]
        if not glbs:
            outputs.append(dict(rec, status="error", error="no mesh GLB: run `creature extract` (writes it) first"))
            continue
        joints, bind_poses = skin["bones"], None
        if own_glb and glbs[0] == own_glb and glb_is_rigid(creature_dir / own_glb):
            # No skin data: the part follows its renderer's transform (e.g. eyes), unless it is a placeholder.
            if "dummy" in (mesh_asset.get("name") or "").lower():
                outputs.append(dict(rec, status="skipped", error="placeholder mesh (no skin data, named 'dummy')"))
                continue
            anchor = next((p for p in (skin.get("renderer_path"), skin.get("root_bone"), *(skin["bones"] or []))
                           if p and p in locals_), None)
            if anchor is None:
                outputs.append(dict(rec, status="error", error="rigid mesh: no transform in the skeleton to attach to"))
                continue
            joints, bind_poses = [anchor], [identity]
            rec["attached_to"] = anchor
        elif own.endswith(".obj"):
            skin_json = creature_dir / (own[:-4] + ".skin.json")
            if skin_json.is_file():
                bind_poses = json.loads(skin_json.read_text(encoding="utf-8"))["bind_poses"]
        missing = [j for j in joints if j is None or j not in locals_]
        if not joints or missing:
            outputs.append(dict(rec, status="error", error=f"skin joints missing from the skeleton: {missing}"))
            continue
        parts.append({"glb": creature_dir / glbs[0], "joints": joints, "bind_poses": bind_poses})
        outputs.append(rec)

    for u in skeleton.get("unresolved_parts") or []:
        outputs.append({"mesh": u["renderer"], "status": "skipped",
                        "error": f"real mesh behind the runtime placeholder not identified "
                                 f"({len(u['candidates'])} candidates)"})
    model: dict[str, Any] | None = None
    if parts:
        name = _SAFE.sub("_", creature["id"])
        work = creature_dir / "convert" / name
        model = {"name": name, "status": "ok", "parts": len(parts)}
        try:
            model["assembly"] = assemble_creature_glb(parts, locals_, work / f"{name}.rigged.glb",
                                                      root_name=creature["id"])
            report = run_job({"input_model": str(work / f"{name}.rigged.glb"), "output_fbx": str(work / f"{name}.fbx"),
                              "report": str(work / "blender-report.json"), "unity_skeleton": locals_,
                              "bone_map": bone_map or {}, "clips": clips}, blender=blender)
            model["fbx"] = f"convert/{name}/{name}.fbx"
            model["blender"] = {k: report.get(k) for k in ("blender_version", "basis", "meshes", "armatures",
                                                           "bounds", "warnings")}
            model["clips"] = [{k: c.get(k) for k in ("name", "frames", "bones_driven", "unknown_paths",
                                                       "unmapped_paths", "max_head_error_m")} for c in report["clips"]]
            for rec in outputs:
                if rec["status"] == "ok":
                    rec["fbx"], rec["clips"] = model["fbx"], model["clips"]
                    assets[rec["asset_index"]]["converted"] = model["fbx"]
            for c in clips:
                assets[c["asset_index"]]["converted"] = model["fbx"]
        except Exception as exc:
            model.update(status="error", error=f"{type(exc).__name__}: {exc}"[:800])
            for rec in outputs:
                if rec["status"] == "ok":
                    rec.update(status="error", error=model["error"])

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
           "summary": summary, "model": model, "meshes": outputs, "clip_errors": clip_errors,
           "notes": ["Variant materials/textures are applied as material instances at Unreal import (milestone 5).",
                     "Unreal import and in-game behaviour are not verified by this step."]}
    write_json_atomic(doc, creature_dir / "convert" / "report.json")
    return doc
