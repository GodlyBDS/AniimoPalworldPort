"""Extract a creature's confirmed assets with UnityPy into its workspace folder.

Outputs (under ``<creature dir>/extract/unitypy/``):

* ``textures/<variant>/<name>.png``: decoded Texture2D images.
* ``meshes/<name>.obj``: mesh geometry via UnityPy's OBJ exporter. OBJ has
  no skin weights or bind poses, so this is a *preview/static* export; skinned
  export for Unreal goes through AssetRipper (glTF) + Blender.
* ``materials/<variant>/<name>.json``: texture slots (pointing at exported
  PNGs), colours and float properties.
* ``animations/<name>.json``: clip metadata (length, sample rate, events, bound
  path hashes). Curves are not exported here (AssetRipper handles clips).
* ``skeleton.json``: bone hierarchy with local transforms from the catalog.
* ``report.json``: every output, its source object and container hash, errors.

Each container's hash is checked against the creature project before anything
is read, so outputs always correspond to the confirmed source files.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter, cab_key, object_id, unitypy_version
from aniimo_pal.errors import ToolkitError
from aniimo_pal.log import get_logger
from aniimo_pal.manifest.io import check_output_location, write_json_atomic

log = get_logger("extract")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str | None, fallback: str) -> str:
    cleaned = _SAFE.sub("_", name or "").strip("._")
    return cleaned[:96] or fallback


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


def extract_creature(creature: dict[str, Any], creature_dir: str | Path, game_root: str | Path, *,
                     include_proposed: bool = False) -> dict[str, Any]:
    creature_dir = Path(creature_dir)
    game_root = Path(game_root)
    out_root = creature_dir / "extract" / "unitypy"
    check_output_location(out_root, [game_root])
    if creature.get("status") == "draft" and not include_proposed:
        raise ToolkitError("creature is still 'draft': confirm/reject proposed associations first "
                           "(aniimo-pal creature review / associate), or pass --include-proposed")

    allowed = {"confirmed"} | ({"proposed"} if include_proposed else set())
    selected = [(i, a) for i, a in enumerate(creature["assets"]) if a.get("association") in allowed]
    by_container: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for i, a in selected:
        by_container[a["ref"]["path"]].append((i, a))

    adapter = UnityPyAdapter()
    outputs: list[dict[str, Any]] = []
    used_names: set[str] = set()
    texture_files: dict[str, str] = {}

    def unique(rel: str) -> str:
        if rel not in used_names:
            used_names.add(rel)
            return rel
        stem, dot, ext = rel.rpartition(".")
        n = 2
        while f"{stem}_{n}.{ext}" in used_names:
            n += 1
        used_names.add(f"{stem}_{n}.{ext}")
        return f"{stem}_{n}.{ext}"

    # Textures first so materials can point at the exported files.
    role_order = {"texture": 0, "mesh": 1, "skeleton": 2, "animation": 3, "material": 4}
    for container, items in sorted(by_container.items()):
        src = game_root / container
        expected = {a["ref"].get("sha256") for _i, a in items}
        try:
            if not src.is_file():
                raise ToolkitError(f"source file missing: {container}")
            actual = _sha256(src)
            if expected != {actual}:
                raise ToolkitError(f"{container} does not match the confirmed sha256; re-scan and re-confirm")
            readers = adapter.open_objects(src)
        except Exception as exc:
            for i, a in items:
                outputs.append({"index": i, "object": a["ref"].get("object"), "role": a["role"], "file": None,
                                "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            continue

        for i, a in sorted(items, key=lambda t: role_order.get(t[1]["role"], 9)):
            rec = {"index": i, "object": a["ref"].get("object"), "role": a["role"], "file": None,
                   "status": "ok", "error": None, "container": container, "sha256": actual}
            try:
                reader = readers.get(a["ref"].get("object"))
                if reader is None:
                    raise ToolkitError("object not found in container (game data changed?)")
                rec["file"] = _export_one(reader, a, out_root, unique, texture_files)
                if rec["file"] is None:
                    rec["status"] = "skipped"
                    rec["error"] = f"role {a['role']!r} has no UnityPy export (handled by later stages)"
                elif a["role"] == "texture":
                    texture_files[a["ref"]["object"]] = rec["file"]
            except Exception as exc:
                rec["status"] = "error"
                rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
                log.warning("extract failed for asset %d (%s): %s", i, a.get("name"), exc)
            outputs.append(rec)
        readers.clear()

    if creature.get("skeleton"):
        write_json_atomic(creature["skeleton"], out_root / "skeleton.json")
        outputs.append({"index": None, "object": creature["skeleton"].get("root_object"), "role": "skeleton-hierarchy",
                        "file": "skeleton.json", "status": "ok", "error": None})

    for rec in outputs:
        if rec["status"] == "ok" and rec["index"] is not None:
            creature["assets"][rec["index"]]["extracted"] = f"extract/unitypy/{rec['file']}"
    errors = sum(1 for r in outputs if r["status"] == "error")
    if errors == 0 and creature.get("status") == "mapped":
        creature["status"] = "extracted"
    creature.setdefault("history", []).append({
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "extracted-unitypy",
        "ok": sum(1 for r in outputs if r["status"] == "ok"), "errors": errors})

    report = {
        "schema": "aniimo-pal/extract-report",
        "schema_version": "0.1.0",
        "generator": {"name": TOOL_NAME, "version": __version__},
        "tool": {"name": "UnityPy", "version": unitypy_version()},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "creature": creature["id"],
        "summary": {"ok": sum(1 for r in outputs if r["status"] == "ok"), "errors": errors,
                    "skipped": sum(1 for r in outputs if r["status"] == "skipped")},
        "outputs": outputs,
    }
    write_json_atomic(report, out_root / "report.json")
    return report


def _scope_dir(asset: dict[str, Any]) -> str:
    """Output sub-folder for an asset's identity scope, e.g. 'normal', 'prismana/shiny_03', 'snow/shared'."""
    parts = []
    if asset.get("region", "any") not in ("any", "base"):
        parts.append(asset["region"])
    parts.append(asset.get("form", "shared"))
    if asset.get("appearance", "any") not in ("any", "standard"):
        parts.append(asset["appearance"])
    return "/".join(parts)


def _export_one(reader, asset: dict[str, Any], out_root: Path, unique, texture_files: dict[str, str]) -> str | None:
    role = asset["role"]
    variant = _scope_dir(asset)
    fallback = f"obj_{reader.path_id}"
    if role == "texture":
        tex = reader.parse_as_object()
        rel = unique(f"textures/{variant}/{_safe(tex.m_Name, fallback)}.png")
        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tex.image.save(path)
        return rel
    if role == "mesh":
        mesh = reader.parse_as_object()
        rel = unique(f"meshes/{_safe(mesh.m_Name, fallback)}.obj")
        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(mesh.export())
        # Skinned-ready GLB written by the toolkit itself (same conventions as AssetRipper's mesh GLB;
        # AssetRipper 2.0 exported real Aniimo meshes only as JSON dumps).
        try:
            from aniimo_pal.convert.unity_mesh_glb import unity_mesh_to_glb

            unity_mesh_to_glb(mesh, out_root / (rel[:-4] + ".glb"))
        except Exception as exc:  # the OBJ is still useful; conversion reports the missing GLB
            log.warning("mesh %s: GLB not written: %s", mesh.m_Name, exc)
        # Skinning data needed to rig the mesh later (bind poses are Unity row/column e<row><col>).
        d = reader.parse_as_dict(check_read=False)
        bindposes = [[[float(m[f"e{r}{c}"]) for c in range(4)] for r in range(4)] for m in d.get("m_BindPose") or []]
        if bindposes:
            write_json_atomic({"mesh": d.get("m_Name"), "space": "unity", "bind_poses": bindposes,
                               "bone_name_hashes": [int(h) for h in d.get("m_BoneNameHashes") or []],
                               "root_bone_name_hash": d.get("m_RootBoneNameHash"), "provenance": "verified"},
                              out_root / (rel[:-4] + ".skin.json"))
        return rel
    if role == "material":
        d = reader.parse_as_dict(check_read=False)
        props = d.get("m_SavedProperties") or {}
        slots = {}
        for slot, env in props.get("m_TexEnvs") or []:
            tex = env.get("m_Texture") or {}
            if tex.get("m_PathID"):
                obj_id = _resolve_pptr(reader, tex)
                slots[slot] = {"texture_object": obj_id, "file": texture_files.get(obj_id) if obj_id else None,
                               "scale": env.get("m_Scale"), "offset": env.get("m_Offset")}
        doc = {"name": d.get("m_Name"), "shader_path_id": (d.get("m_Shader") or {}).get("m_PathID"),
               "keywords": d.get("m_ValidKeywords") or d.get("m_ShaderKeywords"),
               "textures": slots, "colors": dict(props.get("m_Colors") or []),
               "floats": dict(props.get("m_Floats") or []), "provenance": "verified"}
        rel = unique(f"materials/{variant}/{_safe(d.get('m_Name'), fallback)}.json")
        write_json_atomic(_json_safe(doc), out_root / rel)
        return rel
    if role == "animation":
        d = reader.parse_as_dict(check_read=False)
        muscle = d.get("m_MuscleClip") or {}
        bindings = (d.get("m_ClipBindingConstant") or {}).get("genericBindings") or []
        doc = {"name": d.get("m_Name"), "sample_rate": d.get("m_SampleRate"), "legacy": d.get("m_Legacy"),
               "start_time": muscle.get("m_StartTime"), "stop_time": muscle.get("m_StopTime"),
               "events": [{"time": e.get("time"), "function": e.get("functionName"), "data": e.get("data"),
                           "int": e.get("intParameter"), "float": e.get("floatParameter")}
                          for e in d.get("m_Events") or []],
               "bound_path_hashes": sorted({int(b["path"]) for b in bindings if b.get("path")}),
               "curves_exported": False, "provenance": "verified",
               "note": "curves are exported by the AssetRipper stage, not by UnityPy"}
        rel = unique(f"animations/{_safe(d.get('m_Name'), fallback)}.json")
        write_json_atomic(_json_safe(doc), out_root / rel)
        return rel
    if role == "skeleton" and reader.type.name == "Avatar":
        d = reader.parse_as_dict(check_read=False)
        doc = {"name": d.get("m_Name"), "bone_paths": sorted({p for _h, p in d.get("m_TOS") or [] if p}),
               "provenance": "verified"}
        rel = unique(f"skeleton/{_safe(d.get('m_Name'), fallback)}.json")
        write_json_atomic(doc, out_root / rel)
        return rel
    return None


def _resolve_pptr(reader, pptr: dict[str, Any]) -> str | None:
    """Object id for a PPtr read from ``reader``'s file (same rules as the inventory)."""
    file_id = int(pptr.get("m_FileID", 0))
    sf = reader.assets_file
    if file_id == 0:
        return object_id(cab_key(sf.name), int(pptr["m_PathID"]))
    if 0 < file_id <= len(sf.externals):
        return object_id(cab_key(sf.externals[file_id - 1].path), int(pptr["m_PathID"]))
    return None


# --- AssetRipper route ----------------------------------------------------------

_ROLE_EXTENSIONS = {
    "primary": {"texture": (".png", ".jpg", ".tga", ".exr"), "mesh": (".glb", ".gltf"), "animation": (".anim",),
                "material": (".mat", ".json"), "skeleton": (".asset", ".json"), "prefab": (".glb", ".gltf")},
    "project": {"texture": (".png", ".jpg", ".tga", ".exr", ".asset"), "mesh": (".asset",), "animation": (".anim",),
                "material": (".mat",), "skeleton": (".asset",), "prefab": (".prefab",),
                "other": (".controller", ".overrideController")},
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# Build-time suffixes seen on real object names (M_Parmon_10184_Body_-1771000540, MI_..._01a_9632378D):
# a negative or 9+ digit number, or 8 hex digits containing a letter. 7-digit form IDs are kept.
_BUILD_SUFFIX = re.compile(r"_(?:-\d{6,}|\d{9,}|(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8})$")


def _related_unassigned(files: list[str], assigned: set[str], unmatched_names: list[str], limit: int = 60) -> list[str]:
    distinctive = {t for n in unmatched_names for t in _norm(n).split("_") if len(t) >= 4 and not t.isalpha()} or \
        {t for n in unmatched_names for t in _norm(n).split("_") if len(t) >= 5}
    rest = [f for f in files if f not in assigned and not f.endswith(".meta")]
    related = [f for f in rest if distinctive & set(_norm(Path(f).stem).split("_"))]
    return (related or rest)[:limit]


def _name_keys(name: str) -> list[str]:
    """Match keys for an object name: exact, then without a build-time hash suffix."""
    keys = [_norm(name)]
    stripped = _BUILD_SUFFIX.sub("", name)
    if stripped != name and stripped:
        keys.append(_norm(stripped))
    return keys


def assetripper_export_creature(creature: dict[str, Any], creature_dir: str | Path, game_root: str | Path,
                                executable, *, mode: str = "primary", include_proposed: bool = False,
                                replace: bool = False) -> dict[str, Any]:
    """Export a creature's source containers with AssetRipper and map outputs to its assets."""
    import shutil

    from aniimo_pal.adapters.assetripper import AssetRipperSession

    if mode not in _ROLE_EXTENSIONS:
        raise ToolkitError("mode must be 'primary' or 'project'")
    creature_dir = Path(creature_dir).resolve()
    game_root = Path(game_root)
    base = creature_dir / "extract" / "assetripper"
    out_dir = base / mode
    check_output_location(base, [game_root])
    if out_dir.exists() and any(out_dir.iterdir()):
        if not replace:
            raise ToolkitError(f"{out_dir} already has an export; pass --replace to redo it")
        if creature_dir not in out_dir.resolve().parents:  # defensive: only ever delete inside the creature dir
            raise ToolkitError(f"refusing to delete {out_dir}")
        shutil.rmtree(out_dir)

    allowed = {"confirmed"} | ({"proposed"} if include_proposed else set())
    selected = [(i, a) for i, a in enumerate(creature["assets"]) if a.get("association") in allowed]
    if not selected:
        raise ToolkitError("no confirmed assets to export")
    containers: dict[str, str] = {}
    for _i, a in selected:
        containers.setdefault(a["ref"]["path"], a["ref"].get("sha256"))
    for rel, digest in sorted(containers.items()):
        src = game_root / rel
        if not src.is_file() or _sha256(src) != digest:
            raise ToolkitError(f"{rel} is missing or does not match the confirmed sha256; re-scan and re-confirm")

    with AssetRipperSession(executable, base / "logs") as session:
        session.load_files([game_root / rel for rel in sorted(containers)])
        session.export(out_dir, mode)
        log_tail = session.log_tail(5)

    files_by_stem: dict[str, list[str]] = defaultdict(list)
    all_files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    for p in all_files:
        files_by_stem[_norm(p.stem)].append(p.relative_to(creature_dir).as_posix())

    def find(name: str | None, role: str) -> list[str]:
        if not name:
            return []
        exts = _ROLE_EXTENSIONS[mode].get(role, ())
        for i, key in enumerate(_name_keys(name)):
            found = [f for f in files_by_stem.get(key, []) if f.lower().endswith(exts)]
            if found and (i == 0 or len(found) == 1):  # suffix-stripped matches only when unambiguous
                return found
        return []

    matches = []
    for i, a in selected:
        found = find(a.get("name"), a["role"])
        matches.append({"index": i, "name": a.get("name"), "role": a["role"], "files": found})
        if found:
            a.setdefault("assetripper", {})[mode] = found
    prefab_matches = []
    for p in creature.get("prefabs", []):
        found = find(p.get("name"), "prefab")
        prefab_matches.append({"object": p["object"], "name": p.get("name"), "variant": p.get("variant"), "files": found})
        if found:
            p.setdefault("assetripper", {})[mode] = found

    creature.setdefault("history", []).append({
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": f"assetripper-{mode}",
        "matched": sum(1 for m in matches if m["files"]), "unmatched": sum(1 for m in matches if not m["files"])})
    report = {
        "schema": "aniimo-pal/assetripper-report",
        "schema_version": "0.1.0",
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "creature": creature["id"],
        "mode": mode,
        "containers": sorted(containers),
        "summary": {"assets_matched": sum(1 for m in matches if m["files"]),
                    "assets_unmatched": sum(1 for m in matches if not m["files"]),
                    "prefabs_matched": sum(1 for m in prefab_matches if m["files"]),
                    "files_written": len(all_files)},
        "assets": matches,
        "prefabs": prefab_matches,
        # Exported files no asset was matched to, preferring ones that share a name part with an unmatched
        # asset (e.g. the creature ID): shows how AssetRipper named things when matching fails.
        "unassigned_files_sample": _related_unassigned(
            [f.relative_to(creature_dir).as_posix() for f in all_files],
            {f for m in matches + prefab_matches for f in m["files"]},
            [m["name"] for m in matches if not m["files"] and m["name"]]),
        "extension_counts": dict(sorted(Counter(p.suffix.lower() or "(none)" for p in all_files).items())),
        "log_tail": log_tail,
    }
    write_json_atomic(report, base / f"report-{mode}.json")
    return report
