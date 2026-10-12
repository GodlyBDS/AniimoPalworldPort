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


def bones_from_hashes(hashes: list[int], paths: list[str], root_name: str | None = None) -> list[str | None]:
    """Unity ``m_BoneNameHashes`` (CRC32 of the bone path) -> skeleton paths, in the mesh's bone order.

    The hashed string's form is not documented; path, bone name and root-prefixed path are all tried.
    """
    from aniimo_pal.catalog.builder import path_hash

    lookup: dict[int, str] = {}
    for p in paths:
        for key in (p.rsplit("/", 1)[-1], f"{root_name}/{p}" if root_name else None, p):
            if key:
                lookup[path_hash(key)] = p
    return [lookup.get(int(h)) for h in hashes]


def _own_clip(creature_dir: Path, asset: dict[str, Any]) -> dict[str, Any]:
    """Clip decoded by `creature extract` (``animations/<name>.clip.json``), or why there is none."""
    meta_rel = asset.get("extracted") or ""
    if not meta_rel.endswith(".json") or not (creature_dir / meta_rel).is_file():
        return {"error": "not extracted yet: run `creature extract`"}
    meta = json.loads((creature_dir / meta_rel).read_text(encoding="utf-8"))
    if meta.get("clip_file"):
        path = creature_dir / "extract" / "unitypy" / meta["clip_file"]
        if path.is_file():
            return {"clip": json.loads(path.read_text(encoding="utf-8"))}
    if meta.get("decode_error"):
        return {"error": f"clip not decoded: {meta['decode_error']}"}
    return {"error": "extracted before clip decoding existed: run `creature extract` again"}


def _has_texture(creature_dir: Path, material_asset: dict[str, Any] | None) -> bool:
    if not material_asset or not material_asset.get("extracted"):
        return False
    path = creature_dir / material_asset["extracted"]
    if not path.is_file():
        return False
    doc = json.loads(path.read_text(encoding="utf-8"))
    return any((t or {}).get("file") for t in (doc.get("textures") or {}).values())


def _completeness(creature: dict[str, Any], creature_dir: Path, skins: list[dict[str, Any]],
                  outputs: list[dict[str, Any]], clips: list[dict[str, Any]], include_proposed: bool) -> dict[str, Any]:
    """Geometry, materials/textures and animations judged separately: an FBX alone is not a usable port."""
    any_active = any(s.get("active", True) for s in skins)
    base = [s for s in skins if s.get("state") in (None, "default") and not (any_active and s.get("active") is False)]
    base_names = {s.get("mesh_name") for s in base}
    ok = {o["mesh"] for o in outputs if o["status"] == "ok"}
    unresolved = [u["renderer"] for u in (creature.get("skeleton") or {}).get("unresolved_parts") or []
                  if u.get("state") in (None, "default")]
    geometry_missing = sorted(n for n in base_names if n not in ok) + unresolved
    materials = {(a.get("ref") or {}).get("object"): a for a in creature["assets"]
                 if a["role"] == "material" and _usable(a, include_proposed)}
    untextured = []
    for s in base:
        if s.get("mesh_name") not in ok:
            continue
        if not any(_has_texture(creature_dir, materials.get(m)) for m in s.get("materials") or []):
            untextured.append({"part": s.get("mesh_name"), "reason": s.get("materials_missing") or (
                "material not extracted or has no texture" if s.get("materials") else "no material")})
    anims = [a for a in creature["assets"] if a["role"] == "animation" and _usable(a, include_proposed)]

    def grade(have: int, total: int) -> str:
        return "missing" if total and not have else ("complete" if have >= total else "incomplete")

    out = {
        "geometry": {"status": "missing" if not ok else ("complete" if not geometry_missing else "incomplete"),
                     "parts_ok": len(ok & base_names), "missing_parts": geometry_missing},
        "materials": {"status": grade(len([s for s in base if s.get("mesh_name") in ok]) - len(untextured),
                                      len([s for s in base if s.get("mesh_name") in ok])) if ok else "missing",
                      "untextured_parts": untextured},
        "animations": {"status": grade(len(clips), len(anims)) if anims else "none in project",
                       "converted": len(clips), "in_project": len(anims)},
    }
    out["ready_for_next_step"] = all(out[k]["status"] in ("complete", "none in project")
                                     for k in ("geometry", "materials", "animations"))
    return out


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
        own = _own_clip(creature_dir, a)
        files = [f for f in (a.get("assetripper") or {}).get("project", []) if f.endswith(".anim")]
        if own.get("clip") is None and not files:
            clip_errors.append({"index": i, "name": a.get("name"), "error": own.get("error") or
                                "not decoded by `creature extract` and no .anim from the AssetRipper project export"})
            continue
        try:
            clip = own["clip"] if own.get("clip") is not None else parse_anim_file(creature_dir / files[0])
            clip["asset_index"] = i
            clips.append(clip)
        except Exception as exc:  # keep converting other clips
            clip_errors.append({"index": i, "name": a.get("name"), "error": f"{type(exc).__name__}: {exc}"})

    # One rigged model per creature: every skinned part (body, eyes, ...) on one shared skeleton.
    outputs, parts = [], []
    identity = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    any_active = any(s.get("active", True) for s in skins)
    has_base_parts = any(s.get("state") in (None, "default") for s in skins)
    for skin in skins:
        if has_base_parts and skin.get("state") not in (None, "default"):
            outputs.append({"mesh": skin.get("mesh_name"), "status": "skipped", "state": skin["state"],
                            "error": f"alternative look '{skin['state']}' (the game switches to it, e.g. in combat); "
                                     "not part of the base model"})
            continue
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
                skin_data = json.loads(skin_json.read_text(encoding="utf-8"))
                bind_poses = skin_data["bind_poses"]
                if not joints or None in joints or len(joints) != len(bind_poses):
                    # The renderer's bone list is empty or does not fit this mesh (mesh assigned at runtime):
                    # rebuild the joint order from the mesh's own bone hashes.
                    rebuilt = bones_from_hashes(skin_data.get("bone_name_hashes") or [], list(locals_),
                                                skeleton.get("root_name"))
                    if rebuilt and None not in rebuilt and len(rebuilt) == len(bind_poses):
                        joints = rebuilt
                        rec["joints_from"] = "mesh-bone-hashes"
                if len(joints) != len(bind_poses):
                    outputs.append(dict(rec, status="error",
                                        error=f"mesh has {len(bind_poses)} bind poses but the renderer lists "
                                              f"{len(joints)} bones, and the mesh's bone hashes do not match "
                                              "this skeleton"))
                    continue
        missing = [j for j in joints if j is None or j not in locals_]
        if not joints or missing:
            outputs.append(dict(rec, status="error", error=f"skin joints missing from the skeleton: {missing}"))
            continue
        parts.append({"glb": creature_dir / glbs[0], "joints": joints, "bind_poses": bind_poses})
        outputs.append(rec)

    for u in skeleton.get("unresolved_parts") or []:
        cands = u.get("candidates") or []
        outputs.append({"mesh": u["renderer"], "status": "skipped",
                        "error": "real mesh (assigned at runtime) not identified: " +
                                 (f"{len(cands)} candidates ({', '.join(cands[:3])})" if cands
                                  else "no mesh with this name in the object list")})
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
            checks = {c.get("name"): (c.get("decoder") or {}).get("validation") for c in clips}
            model["clips"] = [dict({k: c.get(k) for k in ("name", "frames", "bones_driven", "unknown_paths",
                                                            "unmapped_paths", "max_head_error_m",
                                                            "evaluated_check")},
                                   # Decoded-data checks (Unity space, before Blender): see muscle_clip.validate_clip.
                                   validation=checks.get(c.get("name"))) for c in report["clips"]]
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

    completeness = _completeness(creature, creature_dir, skins, outputs, clips, include_proposed)
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
           "summary": summary, "completeness": completeness, "model": model, "meshes": outputs,
           "clip_errors": clip_errors,
           "notes": ["Variant materials/textures are applied as material instances at Unreal import (milestone 5).",
                     "Unreal import and in-game behaviour are not verified by this step."]}
    write_json_atomic(doc, creature_dir / "convert" / "report.json")
    return doc
