"""Evidence straight from an original bundle (read-only): prefab material chains and clip layouts.

The catalog works from the object inventory, which records only some classes and
fields. When a result looks wrong (no materials, no animation), these probes read
the original container again with UnityPy and print every link as stored, so each
step can be checked:

* ``probe_prefab``: hierarchy -> components -> renderer mesh/material slots (raw
  PPtrs, resolved where possible) -> material shader and texture slots -> textures;
  every MonoBehaviour with its script class and all object references it holds
  (components that assign meshes or materials at runtime show up here).
* ``probe_clip``: the compiled clip's curve sets with field names, types and sizes,
  whether the curve counts add up to the bindings, and the decoder's verdict.

References into other bundles are followed when an object list (``objects.json``)
says which container holds the target serialized file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter, cab_key, iter_asset_path_strings, iter_pptrs, object_id
from aniimo_pal.errors import ToolkitError

RENDERERS = ("SkinnedMeshRenderer", "MeshRenderer")


class _Resolver:
    """Loads containers on demand and resolves PPtrs across serialized files."""

    def __init__(self, root: Path, objects_doc: dict[str, Any] | None):
        self.root = root
        self.adapter = UnityPyAdapter()
        self.readers: dict[str, Any] = {}
        self.loaded: set[str] = set()
        self.envs: list[Any] = []
        self.cab_container: dict[str, str] = {}
        self.object_container: dict[str, str] = {}
        self.asset_index: dict[str, list[dict[str, Any]]] | None = (objects_doc or {}).get("asset_index")
        if objects_doc:
            for c in objects_doc.get("containers", []):
                for sf in c.get("serialized_files", []):
                    self.cab_container.setdefault(sf["cab"], c["path"])

    def load(self, container: str) -> None:
        if container in self.loaded:
            return
        self.loaded.add(container)
        path = self.root / container
        if not path.is_file():
            raise ToolkitError(f"container not found: {container}")
        objs = self.adapter.open_objects(path)
        self.envs.append(objs.pop("__env__", None))  # keep each environment alive while its readers are used
        self.readers.update(objs)
        for oid in objs:
            self.object_container.setdefault(oid, container)

    def target(self, reader, pptr: dict[str, Any]) -> tuple[str | None, str]:
        """(object id, status) for a PPtr read from ``reader``'s file."""
        path_id = int(pptr.get("m_PathID") or 0)
        if not path_id:
            return None, "null (empty slot)"
        file_id = int(pptr.get("m_FileID") or 0)
        sf = reader.assets_file
        if file_id == 0:
            cab = cab_key(sf.name)
        elif 0 < file_id <= len(sf.externals):
            cab = cab_key(sf.externals[file_id - 1].path)
        else:
            return None, f"bad file index {file_id}"
        oid = object_id(cab, path_id)
        if oid not in self.readers and cab in self.cab_container:
            try:
                self.load(self.cab_container[cab])
            except Exception as exc:
                return oid, f"container {self.cab_container[cab]} not readable: {exc}"
        if oid in self.readers:
            return oid, "resolved"
        if cab in self.cab_container:
            return oid, f"object missing from {self.cab_container[cab]}"
        return oid, f"serialized file {cab} not in any scanned container"

    def read(self, oid: str | None) -> dict[str, Any] | None:
        reader = self.readers.get(oid) if oid else None
        if reader is None:
            return None
        try:
            return reader.parse_as_dict(check_read=False)
        except Exception as exc:
            return {"_error": f"{type(exc).__name__}: {exc}"[:300]}

    def summary(self, oid: str | None) -> dict[str, Any]:
        reader = self.readers.get(oid) if oid else None
        if reader is None:
            return {}
        out: dict[str, Any] = {"class": reader.type.name, "container": self.object_container.get(oid)}
        try:
            out["name"] = reader.peek_name() or None
        except Exception:
            pass
        return out


def _ref(res: _Resolver, reader, pptr: dict[str, Any]) -> dict[str, Any]:
    oid, status = res.target(reader, pptr)
    return {"file_id": pptr.get("m_FileID"), "path_id": pptr.get("m_PathID"), "object": oid, "status": status,
            **res.summary(oid)}


def _shader_name(d: dict[str, Any] | None) -> str | None:
    if not d:
        return None
    return (d.get("m_ParsedForm") or {}).get("m_Name") or d.get("m_Name")


def _material(res: _Resolver, oid: str | None) -> dict[str, Any]:
    d = res.read(oid)
    if not d or "_error" in (d or {}):
        return {"error": (d or {}).get("_error", "not readable")}
    reader = res.readers[oid]
    shader = _ref(res, reader, d.get("m_Shader") or {})
    shader["shader_name"] = _shader_name(res.read(shader.get("object"))) if shader["status"] == "resolved" else None
    props = d.get("m_SavedProperties") or {}
    textures = []
    for slot, env in props.get("m_TexEnvs") or []:
        tex = env.get("m_Texture") or {}
        entry = {"slot": slot, **_ref(res, reader, tex)}
        td = res.read(entry["object"]) if entry["status"] == "resolved" else None
        if td and "_error" not in td:
            entry.update(width=td.get("m_Width"), height=td.get("m_Height"), format=td.get("m_TextureFormat"))
        textures.append(entry)
    return {"name": d.get("m_Name"), "shader": shader, "texture_slots": textures,
            "colors": {k: v for k, v in props.get("m_Colors") or []},
            "keywords": d.get("m_ValidKeywords") or d.get("m_ShaderKeywords")}


def _script_class(res: _Resolver, reader, d: dict[str, Any]) -> dict[str, Any]:
    script = _ref(res, reader, d.get("m_Script") or {})
    sd = res.read(script.get("object")) if script["status"] == "resolved" else None
    if sd and "_error" not in sd:
        script.update(class_name=sd.get("m_ClassName"), namespace=sd.get("m_Namespace"),
                      assembly=sd.get("m_AssemblyName"))
    return script


def _follow(res: _Resolver, oid: str, depth: int, seen: set[str] | None = None) -> dict[str, Any]:
    """Decode a referenced data object (ScriptableObject/MonoBehaviour) and list what it references in turn.

    Materials are expanded to shader and texture slots; further MonoBehaviours are followed to ``depth``.
    """
    seen = seen if seen is not None else set()
    if oid in seen:
        return {"note": "already shown"}
    seen.add(oid)
    reader = res.readers.get(oid)
    d = res.read(oid) or {}
    if reader is None or "_error" in d:
        return {"error": d.get("_error", "not readable")}
    out: dict[str, Any] = {"name": d.get("m_Name"), "script": _script_class(res, reader, d),
                           "fields": sorted(k for k in d if not k.startswith("m_GameObject"))[:60], "references": []}
    strings = list(iter_asset_path_strings(d))
    if strings:  # assets named by path (e.g. PlatformData m_MeshPath.m_Path / m_MaterialList[k].m_Path)
        out["asset_path_strings"] = [_resolve_path_string(res, field, value) for field, value in strings[:200]]
    for field, file_id, path_id in iter_pptrs(d):
        if field in ("m_GameObject", "m_Script"):
            continue
        ref = {"field": field, **_ref(res, reader, {"m_FileID": file_id, "m_PathID": path_id})}
        if ref["status"] == "resolved":
            if ref.get("class") == "Material":
                ref["material"] = _material(res, ref["object"])
            elif ref.get("class") == "Mesh":
                md = res.read(ref["object"]) or {}
                ref["vertex_count"] = (md.get("m_VertexData") or {}).get("m_VertexCount")
                ref["bones"] = len(md.get("m_BindPose") or [])
            elif ref.get("class") == "MonoBehaviour" and depth > 1:
                ref["follow"] = _follow(res, ref["object"], depth - 1, seen)
        out["references"].append(ref)
        if len(out["references"]) >= 300:
            out["truncated"] = True
            break
    return out


def _resolve_path_string(res: _Resolver, field: str, value: str) -> dict[str, Any]:
    """One path string -> the object the bundles list under exactly that path (expanded like references)."""
    out: dict[str, Any] = {"field": field, "path": value}
    if res.asset_index is None:
        out["status"] = "objects.json has no asset path index: run the inventory with toolkit 0.6.2 or later"
        return out
    copies = res.asset_index.get(value.replace("\\", "/").lower())
    if not copies:
        out["status"] = "path not listed by any scanned bundle"
        return out
    out["copies"] = [{"container": c["container"], "object": c["object"], "meshes": c.get("meshes")} for c in copies]
    oid = copies[-1]["object"] if len(copies) == 1 else max(
        copies, key=lambda c: (c["container"].rsplit("/", 1)[-1].startswith("xpt"), c["container"]))["object"]
    cab = oid.rsplit(":", 1)[0]
    if oid not in res.readers and cab in res.cab_container:
        try:
            res.load(res.cab_container[cab])
        except Exception as exc:
            out["status"] = f"container not readable: {exc}"
            return out
    out.update(object=oid, status="resolved" if oid in res.readers else "listed, but object not found",
               **res.summary(oid))
    if out.get("class") == "Material":
        out["material"] = _material(res, oid)
    elif out.get("class") == "Mesh":
        md = res.read(oid) or {}
        out["vertex_count"] = (md.get("m_VertexData") or {}).get("m_VertexCount")
        out["bones"] = len(md.get("m_BindPose") or [])
    meshes = copies[0].get("meshes") or []
    if out.get("class") != "Mesh" and meshes:
        out["model_meshes"] = []
        for mid in meshes[:8]:
            if mid not in res.readers and mid.rsplit(":", 1)[0] in res.cab_container:
                try:
                    res.load(res.cab_container[mid.rsplit(":", 1)[0]])
                except Exception:
                    pass
            out["model_meshes"].append({"object": mid, **res.summary(mid)})
    return out


def _reachable_classes(refs: list[dict[str, Any]]) -> set[str]:
    out = set()
    for r in refs:
        out.add(r.get("class") or "?")
        follow = r.get("follow") or {}
        out |= _reachable_classes(follow.get("references") or [])
        out |= {p.get("class") for p in follow.get("asset_path_strings") or [] if p.get("class")}
        if any(p.get("model_meshes") for p in follow.get("asset_path_strings") or []):
            out.add("Mesh")
    return out


def probe_prefab(root: str | Path, *, name: str, container: str | None = None, object_ref: str | None = None,
                 objects_doc: dict[str, Any] | None = None, include_materials: bool = True,
                 follow_depth: int = 2) -> dict[str, Any]:
    root = Path(root)
    res = _Resolver(root, objects_doc)
    if container is None and object_ref and objects_doc:
        container = next((o.get("container") for o in objects_doc.get("objects", []) if o["id"] == object_ref), None)
    if container is None:
        raise ToolkitError("give the container (relative to the game root) or an object id found in the object list")
    res.load(container)
    gos = [(oid, r) for oid, r in res.readers.items()
           if r.type.name == "GameObject"
           and res.object_container.get(oid) == container]
    matches = []
    for oid, r in gos:
        if object_ref and oid != object_ref:
            continue
        try:
            if r.peek_name() == name:
                matches.append((oid, r))
        except Exception:
            continue
    if not matches:
        raise ToolkitError(f"no GameObject named {name!r} in {container}")
    oid, root_reader = matches[0]
    prefab_objects = {o for o, _r in res.readers.items() if res.object_container.get(o) == container}
    out: dict[str, Any] = {"schema": "aniimo-pal/probe-prefab", "name": name, "object": oid, "container": container,
                           "matches": len(matches), "nodes": [], "renderers": [], "components": []}
    stack = [(oid, "")]
    seen = set()
    while stack:
        go_id, path = stack.pop()
        if go_id in seen:
            continue
        seen.add(go_id)
        go = res.read(go_id) or {}
        go_reader = res.readers[go_id]
        node = {"path": path, "object": go_id, "active": go.get("m_IsActive"), "components": []}
        out["nodes"].append(node)
        transform = None
        for entry in go.get("m_Component") or []:
            comp = entry.get("component") if isinstance(entry, dict) else entry[1] if isinstance(entry, (list, tuple)) else None
            if not comp:
                continue
            cid, status = res.target(go_reader, comp)
            reader = res.readers.get(cid) if cid else None
            cls = reader.type.name if reader is not None else None
            node["components"].append({"class": cls, "object": cid, "status": status})
            if reader is None:
                continue
            d = res.read(cid) or {}
            if cls in ("Transform", "RectTransform"):
                transform = (reader, d)
            elif cls in RENDERERS:
                rec = {"path": path, "class": cls, "object": cid, "enabled": d.get("m_Enabled"),
                       "mesh": _ref(res, reader, d.get("m_Mesh") or {}) if "m_Mesh" in d else None,
                       "material_slots": []}
                if cls == "MeshRenderer":
                    mf = next((c for c in go.get("m_Component") or [] if isinstance(c, dict)), None)
                    rec["mesh_filter_note"] = "mesh comes from the MeshFilter component" if mf else None
                for i, m in enumerate(d.get("m_Materials") or []):
                    slot = {"index": i, **_ref(res, reader, m)}
                    if include_materials and slot["status"] == "resolved":
                        slot["material"] = _material(res, slot["object"])
                    rec["material_slots"].append(slot)
                if not rec["material_slots"]:
                    rec["materials_note"] = "renderer has no material slots in the prefab"
                rec["bones"] = len(d.get("m_Bones") or [])
                out["renderers"].append(rec)
            elif cls == "MonoBehaviour":
                comp_rec = {"path": path, "object": cid, "error": d.get("_error")}
                if "_error" not in d:
                    comp_rec["script"] = _script_class(res, reader, d)
                    refs = []
                    for field, file_id, path_id in iter_pptrs(d):
                        if field in ("m_GameObject", "m_Script"):
                            continue
                        ref = _ref(res, reader, {"m_FileID": file_id, "m_PathID": path_id})
                        refs.append({"field": field, **ref})
                    for ref in refs:  # data assets the component points at (e.g. XRender PlatformData)
                        if ref.get("class") == "MonoBehaviour" and ref["status"] == "resolved" \
                                and ref["object"] not in prefab_objects:
                            ref["follow"] = _follow(res, ref["object"], depth=follow_depth)
                    comp_rec["references"] = refs[:400]
                    comp_rec["reference_count"] = len(refs)
                    comp_rec["referenced_classes"] = sorted({r.get("class") or "?" for r in refs})
                    comp_rec["reachable_classes"] = sorted(_reachable_classes(refs))
                out["components"].append(comp_rec)
        if transform is not None:
            treader, td = transform
            for child in reversed(td.get("m_Children") or []):
                cid, _status = res.target(treader, child)
                cd = res.read(cid) or {}
                go_ref = cd.get("m_GameObject") or {}
                gid, _ = res.target(res.readers[cid], go_ref) if cid in res.readers else (None, "")
                if gid:
                    child_name = res.summary(gid).get("name") or ""
                    stack.append((gid, f"{path}/{child_name}" if path else child_name))
    out["summary"] = {
        "renderers": len(out["renderers"]),
        "renderers_with_mesh": sum(1 for r in out["renderers"] if r["mesh"] and r["mesh"]["status"] == "resolved"),
        "renderers_with_empty_mesh_slot": sum(1 for r in out["renderers"]
                                              if r["mesh"] and r["mesh"]["status"].startswith("null")),
        "material_slots": sum(len(r["material_slots"]) for r in out["renderers"]),
        "material_slots_resolved": sum(1 for r in out["renderers"] for s in r["material_slots"]
                                       if s["status"] == "resolved"),
        "material_slots_empty": sum(1 for r in out["renderers"] for s in r["material_slots"]
                                    if s["status"].startswith("null")),
        "components_reaching_materials": [c["path"] + " " + str((c.get("script") or {}).get("class_name"))
                                          for c in out["components"]
                                          if "Material" in (c.get("reachable_classes") or [])],
        "components_reaching_meshes": [c["path"] + " " + str((c.get("script") or {}).get("class_name"))
                                       for c in out["components"] if "Mesh" in (c.get("reachable_classes") or [])],
    }
    return out


def probe_clip(root: str | Path, *, name: str, container: str, objects_doc: dict[str, Any] | None = None,
               skeleton_paths: list[str] | None = None, raw: bool = False,
               skeleton_rest: dict[str, dict[str, list[float]]] | None = None) -> dict[str, Any]:
    from aniimo_pal.convert.muscle_clip import (
        ClipNotDecodable,
        avatar_tables,
        decode_clip,
        describe,
        hash_lookup,
        structure_report,
    )

    res = _Resolver(Path(root), objects_doc)
    res.load(container)
    clips = [(oid, r) for oid, r in res.readers.items() if r.type.name == "AnimationClip"]
    match = None
    for oid, r in clips:
        try:
            if r.peek_name() == name:
                match = (oid, r)
                break
        except Exception:
            continue
    if match is None:
        raise ToolkitError(f"no AnimationClip named {name!r} in {container} ({len(clips)} clips)")
    oid, reader = match
    d = reader.parse_as_dict(check_read=False)
    data = ((d.get("m_MuscleClip") or {}).get("m_Clip") or {})
    data = data.get("data", data)
    out = {"schema": "aniimo-pal/probe-clip", "name": name, "object": oid, "container": container,
           "clips_in_container": len(clips), "top_level_fields": sorted(d),
           "muscle_clip_fields": sorted(d.get("m_MuscleClip") or {}),
           "structure": structure_report(d),
           "curve_sets": {k: describe(v) for k, v in data.items()},
           "binding_example": describe(((d.get("m_ClipBindingConstant") or {}).get("genericBindings") or [None])[:1])}
    # Bone names and rest pose: the Avatar(s) in the clip's own bundle (verified), else the creature's skeleton.
    tos, rest_by_hash = [], {}
    for _aid, ar in res.readers.items():
        if ar.type.name == "Avatar" and res.object_container.get(_aid) == container:
            try:
                tables = avatar_tables(ar.parse_as_dict(check_read=False))
            except Exception:
                continue
            tos += tables["tos"]
            rest_by_hash.update(tables["rest_by_hash"])
    hashes = hash_lookup(skeleton_paths or [], tos=tos)
    rest = {hashes[h]: pose for h, pose in rest_by_hash.items() if h in hashes} or (skeleton_rest or None)
    out["bone_names_from"] = ("Avatar TOS in the clip's bundle" if tos else
                              "creature skeleton paths" if skeleton_paths else "none (pass --creature)")
    out["rest_pose_from"] = ("Avatar skeleton pose" if rest_by_hash else
                             "creature skeleton" if skeleton_rest else "none")
    try:
        clip = decode_clip(d, hashes, rest)
        out["decoder"] = {"status": "decoded", "tracks": len(clip["tracks"]), "frames": clip["decoder"]["sampled_frames"],
                          "warnings": clip["warnings"], "details": clip["decoder"],
                          "validation": clip["decoder"].get("validation"),
                          "quantized_search": out["structure"].get("quantized_search")}
    except ClipNotDecodable as exc:
        out["decoder"] = {"status": "not decodable", "reason": str(exc),
                          "quantized_search": exc.report.get("quantized_search")}
    if raw:  # the whole parsed clip, to study an undocumented format offline
        out["raw"] = _json_safe(d)
    return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, float) and value != value:
        return None
    return value
