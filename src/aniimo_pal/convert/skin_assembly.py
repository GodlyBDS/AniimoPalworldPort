"""Assemble a rigged (skinned) glTF from AssetRipper's mesh GLB + the catalog skeleton.

Why: AssetRipper's GLB export (master @ beeb50e) attaches meshes to prefab nodes
only through MeshFilter, as rigid meshes. SkinnedMeshRenderer creatures come out
as a bare node hierarchy, while the standalone mesh GLB keeps JOINTS_0 /
WEIGHTS_0 but has no skin. This module adds the missing pieces:

* joint nodes built from the Unity skeleton (local TRS), converted to glTF
  space exactly like AssetRipper does: position (-x, y, z), quaternion
  (x, -y, -z, w);
* a skin whose joint order is the renderer's ``m_Bones`` order, which is what
  the blend indices refer to;
* inverse bind matrices from the mesh's real Unity bind poses (or computed from
  the skeleton rest pose if none are available), converted by conjugation with
  S = diag(-1, 1, 1): IBM_gltf = S · bindpose_unity · S.

The input files are only read; the result is written as a new GLB.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np

from aniimo_pal.errors import ToolkitError

S = np.diag([-1.0, 1.0, 1.0, 1.0])


def read_glb(path: str | Path) -> tuple[dict[str, Any], bytes]:
    data = Path(path).read_bytes()
    if data[:4] != b"glTF":
        raise ToolkitError(f"{path} is not a GLB file")
    _magic, _version, length = struct.unpack_from("<III", data, 0)
    offset, doc, binary = 12, None, b""
    while offset < length:
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8: offset + 8 + chunk_len]
        if chunk_type == 0x4E4F534A:  # JSON
            doc = json.loads(chunk.decode("utf-8"))
        elif chunk_type == 0x004E4942:  # BIN
            binary = bytes(chunk)
        offset += 8 + chunk_len
    if doc is None:
        raise ToolkitError(f"{path} has no JSON chunk")
    return doc, binary


def write_glb(path: str | Path, doc: dict[str, Any], binary: bytes) -> Path:
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    total = 12 + 8 + len(js) + (8 + len(binary) if binary else 0)
    out = struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
    if binary:
        out += struct.pack("<II", len(binary), 0x004E4942) + binary
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(out)
    return p


def _trs(pos, rot, scale=(1, 1, 1)) -> np.ndarray:
    x, y, z, w = rot
    n = float(np.sqrt(x * x + y * y + z * z + w * w)) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.eye(4)
    m[:3, :3] = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]) * np.array(scale)
    m[:3, 3] = pos
    return m


def skeleton_locals(skeleton: dict[str, Any]) -> dict[str, dict[str, list[float]]]:
    """Catalog skeleton (bones with Unity 'local' TRS) -> {path: {position, rotation, scale}}."""
    out = {}
    for b in skeleton.get("bones") or []:
        loc = b.get("local") or {}
        p, r, sc = loc.get("position") or {}, loc.get("rotation") or {}, loc.get("scale") or {}
        out[b["path"]] = {"position": [p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0)],
                          "rotation": [r.get("x", 0.0), r.get("y", 0.0), r.get("z", 0.0), r.get("w", 1.0)],
                          "scale": [sc.get("x", 1.0), sc.get("y", 1.0), sc.get("z", 1.0)]}
    return out


def unity_world(locals_: dict[str, dict[str, list[float]]]) -> dict[str, np.ndarray]:
    world: dict[str, np.ndarray] = {}
    for p in sorted(locals_, key=lambda p: (p.count("/"), p)):
        loc = locals_[p]
        m = _trs(loc["position"], loc["rotation"], loc.get("scale", (1, 1, 1)))
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        world[p] = world[parent] @ m if parent in world else m
    return world


def assemble_skinned_glb(mesh_glb: str | Path, locals_: dict[str, dict[str, list[float]]], joints: list[str],
                         out_path: str | Path, *, bind_poses: list[list[list[float]]] | None = None,
                         root_name: str = "Creature") -> dict[str, Any]:
    """Write a rigged GLB from one mesh GLB. ``joints``: renderer bone paths in m_Bones order."""
    result = assemble_creature_glb([{"glb": mesh_glb, "joints": joints, "bind_poses": bind_poses}], locals_,
                                   out_path, root_name=root_name)
    part = result["parts"][0]
    return {"output": result["output"], "joints": part["joints"], "joint_nodes": result["joint_nodes"],
            "skinned_mesh_nodes": part["skinned_mesh_nodes"], "bind_pose_source": part["bind_pose_source"],
            "bind_pose_max_deviation": part["bind_pose_max_deviation"], "warnings": result["warnings"]}


def _bind_matrices(joints: list[str], bind_poses: list[list[list[float]]] | None,
                   world: dict[str, np.ndarray], label: str) -> tuple[list[np.ndarray], str, float, list[str]]:
    rest_bind = [np.linalg.inv(world[j]) for j in joints]
    if not bind_poses:
        return rest_bind, "skeleton-rest-pose", 0.0, [f"{label}: no bind poses supplied; computed from the "
                                                      "skeleton rest pose"]
    if len(bind_poses) != len(joints):
        raise ToolkitError(f"{label}: {len(bind_poses)} bind poses for {len(joints)} joints")
    binds = [np.array(b, dtype=float) for b in bind_poses]
    deviation = max(float(np.abs(b - r).max()) for b, r in zip(binds, rest_bind, strict=True))
    warnings = []
    if deviation > 1e-3:
        warnings.append(f"{label}: bind pose differs from the skeleton rest pose (max element diff "
                        f"{deviation:.4g}); the mesh will deform from its bind pose, which is correct, but check "
                        "the rest pose")
    return binds, "mesh-bind-poses", deviation, warnings


def assemble_creature_glb(parts: list[dict[str, Any]], locals_: dict[str, dict[str, list[float]]],
                          out_path: str | Path, *, root_name: str = "Creature") -> dict[str, Any]:
    """Write one rigged GLB holding every mesh part of a creature on one shared joint hierarchy.

    ``parts``: ``{"glb": mesh GLB, "joints": bone paths in that renderer's m_Bones order,
    "bind_poses": Unity bind poses or None}``. Each part keeps its own skin (joint order and inverse bind
    matrices) but all skins use the same joint nodes, so the result is one skeleton with several meshes
    (body, eyes, ...), which is what one Unreal skeletal mesh needs.
    """
    if not parts:
        raise ToolkitError("no mesh parts to assemble")
    for part in parts:
        joints = part["joints"]
        if not joints or any(j is None or j not in locals_ for j in joints):
            missing = [j for j in joints if j is None or j not in locals_]
            raise ToolkitError(f"skin joints missing from the skeleton: {missing or 'no joints'}")
    world = unity_world(locals_)
    warnings: list[str] = []
    doc: dict[str, Any] = {"asset": {"version": "2.0", "generator": "aniimo-pal skin assembly"}, "nodes": [],
                           "meshes": [], "accessors": [], "bufferViews": [], "skins": []}
    binary = bytearray()
    nodes = doc["nodes"]

    # Joint nodes: every joint of every part plus its ancestors, so the hierarchy is complete.
    needed = set()
    for part in parts:
        for j in part["joints"]:
            pieces = j.split("/")
            needed |= {"/".join(pieces[:i]) for i in range(1, len(pieces) + 1)}
    paths = sorted(needed, key=lambda p: (p.count("/"), p))
    root = 0
    nodes.append({"name": root_name, "children": []})
    index: dict[str, int] = {}
    for p in paths:
        loc = locals_[p]
        pos, rot = loc["position"], loc["rotation"]
        index[p] = len(nodes)
        nodes.append({"name": p.rsplit("/", 1)[-1], "translation": [-pos[0], pos[1], pos[2]],
                      "rotation": [rot[0], -rot[1], -rot[2], rot[3]], "scale": list(loc.get("scale", [1, 1, 1]))})
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        nodes[index[parent] if parent else root].setdefault("children", []).append(index[p])

    def add_bytes(data: bytes) -> int:
        while len(binary) % 4:
            binary.append(0)
        offset = len(binary)
        binary.extend(data)
        return offset

    part_reports = []
    for n, part in enumerate(parts):
        label = Path(part["glb"]).stem
        src, src_bin = read_glb(part["glb"])
        skinned_meshes = {i for i, m in enumerate(src.get("meshes", []))
                          if any("JOINTS_0" in p.get("attributes", {}) for p in m.get("primitives", []))}
        if not skinned_meshes:
            raise ToolkitError(f"{part['glb']} has no JOINTS_0/WEIGHTS_0 attributes; nothing to skin")
        base_off = add_bytes(src_bin)
        view_base, acc_base, mesh_base = len(doc["bufferViews"]), len(doc["accessors"]), len(doc["meshes"])
        for v in src.get("bufferViews", []):
            doc["bufferViews"].append(dict(v, buffer=0, byteOffset=base_off + v.get("byteOffset", 0)))
        for a in src.get("accessors", []):
            doc["accessors"].append(dict(a, bufferView=a["bufferView"] + view_base) if "bufferView" in a else dict(a))
        for m in src.get("meshes", []):
            prims = []
            for prim in m.get("primitives", []):
                q = dict(prim, attributes={k: v + acc_base for k, v in prim["attributes"].items()})
                if "indices" in q:
                    q["indices"] = prim["indices"] + acc_base
                q.pop("material", None)  # materials are assigned in the Blender job
                prims.append(q)
            doc["meshes"].append(dict(m, primitives=prims))
        binds, source, deviation, w = _bind_matrices(part["joints"], part.get("bind_poses"), world, label)
        warnings += w
        joint_paths = list(part["joints"])
        if n == 0:
            # Every ancestor of a joint becomes a joint too (no vertex weights; bind = its rest pose). Otherwise
            # Blender's glTF importer places the armature OBJECT at the topmost non-joint node (e.g. a 3ds Max
            # 'Bip001' with its biped rotation), so pose deformation happens in a turned space: a clip that
            # lifts the body moved the real 10351 sideways. Extra joints are appended, so blend indices stay valid.
            extra = [p for p in paths if p not in set(joint_paths)]
            joint_paths += extra
            binds = list(binds) + [np.linalg.inv(world[p]) for p in extra]
        ibms = [S @ b @ S for b in binds]
        ibm_off = add_bytes(b"".join(struct.pack("<16f", *m.T.flatten()) for m in ibms))  # glTF is column-major
        doc["bufferViews"].append({"buffer": 0, "byteOffset": ibm_off, "byteLength": 64 * len(ibms)})
        doc["accessors"].append({"bufferView": len(doc["bufferViews"]) - 1, "componentType": 5126,
                                 "count": len(ibms), "type": "MAT4"})
        doc["skins"].append({"name": f"{root_name}_Skin{n}", "joints": [index[j] for j in joint_paths],
                             "inverseBindMatrices": len(doc["accessors"]) - 1, "skeleton": index[paths[0]]})
        bound = 0
        for node in src.get("nodes", []):
            if "mesh" in node and node["mesh"] in skinned_meshes:
                nodes.append({"name": node.get("name") or label, "mesh": node["mesh"] + mesh_base,
                              "skin": len(doc["skins"]) - 1})
                nodes[root]["children"].append(len(nodes) - 1)
                bound += 1
        part_reports.append({"glb": str(part["glb"]), "joints": len(part["joints"]), "skinned_mesh_nodes": bound,
                             "bind_pose_source": source, "bind_pose_max_deviation": deviation})
    doc["buffers"] = [{"byteLength": len(binary)}]
    doc["scenes"] = [{"nodes": [root]}]
    doc["scene"] = 0
    write_glb(out_path, doc, bytes(binary))
    return {"output": str(out_path), "joint_nodes": len(paths), "parts": part_reports, "warnings": warnings}
