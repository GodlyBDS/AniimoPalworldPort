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
    """Write a rigged GLB. ``joints``: renderer bone paths in m_Bones order."""
    if not joints or any(j is None or j not in locals_ for j in joints):
        missing = [j for j in joints if j is None or j not in locals_]
        raise ToolkitError(f"skin joints missing from the skeleton: {missing or 'no joints'}")
    doc, binary = read_glb(mesh_glb)
    warnings: list[str] = []

    # Joint nodes: every joint plus its ancestors, so the hierarchy is complete.
    needed = set()
    for j in joints:
        parts = j.split("/")
        needed |= {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    paths = sorted(needed, key=lambda p: (p.count("/"), p))
    nodes = doc.setdefault("nodes", [])
    mesh_nodes = [i for i, n in enumerate(nodes) if "mesh" in n]
    skinned_meshes = {i for i, m in enumerate(doc.get("meshes", []))
                      if any("JOINTS_0" in p.get("attributes", {}) for p in m.get("primitives", []))}
    if not skinned_meshes:
        raise ToolkitError(f"{mesh_glb} has no JOINTS_0/WEIGHTS_0 attributes; nothing to skin")
    old_roots = list((doc.get("scenes") or [{"nodes": list(range(len(nodes)))}])[doc.get("scene", 0)]["nodes"])

    index: dict[str, int] = {}
    root = len(nodes)
    nodes.append({"name": root_name, "children": []})
    for p in paths:
        loc = locals_[p]
        pos, rot = loc["position"], loc["rotation"]
        node = {"name": p.rsplit("/", 1)[-1], "translation": [-pos[0], pos[1], pos[2]],
                "rotation": [rot[0], -rot[1], -rot[2], rot[3]], "scale": list(loc.get("scale", [1, 1, 1]))}
        index[p] = len(nodes)
        nodes.append(node)
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        nodes[index[parent] if parent else root].setdefault("children", []).append(index[p])
    nodes[root]["children"] += old_roots

    world = unity_world(locals_)
    rest_bind = [np.linalg.inv(world[j]) for j in joints]
    if bind_poses:
        if len(bind_poses) != len(joints):
            raise ToolkitError(f"{len(bind_poses)} bind poses for {len(joints)} joints")
        binds = [np.array(b, dtype=float) for b in bind_poses]
        deviation = max(float(np.abs(b - r).max()) for b, r in zip(binds, rest_bind, strict=True))
        if deviation > 1e-3:
            warnings.append(f"bind pose differs from the skeleton rest pose (max element diff {deviation:.4g}); "
                            "the mesh will deform from its bind pose, which is correct, but check the rest pose")
        source = "mesh-bind-poses"
    else:
        binds, deviation, source = rest_bind, 0.0, "skeleton-rest-pose"
        warnings.append("no bind poses supplied; computed from the skeleton rest pose")
    ibms = [S @ b @ S for b in binds]

    pad = (4 - len(binary) % 4) % 4
    binary += b"\x00" * pad
    offset = len(binary)
    ibm_bytes = b"".join(struct.pack("<16f", *m.T.flatten()) for m in ibms)  # glTF is column-major
    binary += ibm_bytes
    doc.setdefault("bufferViews", []).append({"buffer": 0, "byteOffset": offset, "byteLength": len(ibm_bytes)})
    doc.setdefault("accessors", []).append({"bufferView": len(doc["bufferViews"]) - 1, "componentType": 5126,
                                            "count": len(ibms), "type": "MAT4"})
    if not doc.get("buffers"):
        doc["buffers"] = [{}]
    doc["buffers"][0]["byteLength"] = len(binary)
    doc["buffers"][0].pop("uri", None)
    doc.setdefault("skins", []).append({"name": f"{root_name}_Skin", "joints": [index[j] for j in joints],
                                        "inverseBindMatrices": len(doc["accessors"]) - 1,
                                        "skeleton": index[paths[0]]})
    skin_index = len(doc["skins"]) - 1
    bound = 0
    for i in mesh_nodes:
        if nodes[i]["mesh"] in skinned_meshes:
            nodes[i]["skin"] = skin_index
            bound += 1
    doc["scenes"] = [{"nodes": [root]}]
    doc["scene"] = 0
    doc.setdefault("asset", {"version": "2.0"})["generator"] = "aniimo-pal skin assembly"
    write_glb(out_path, doc, binary)
    return {"output": str(out_path), "joints": len(joints), "joint_nodes": len(paths), "skinned_mesh_nodes": bound,
            "bind_pose_source": source, "bind_pose_max_deviation": deviation, "warnings": warnings}
