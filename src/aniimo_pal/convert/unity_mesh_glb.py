"""Write a Unity Mesh (read with UnityPy) as a GLB with skin attributes, without AssetRipper.

Why: on the real Aniimo install, AssetRipper 2.0 exported the creature meshes
only as JSON data dumps (no GLB), so conversion had nothing to work with.
UnityPy decodes the mesh itself (vertex streams, compressed meshes, index
buffers), so the toolkit builds the GLB directly.

The output matches what :mod:`aniimo_pal.convert.skin_assembly` expects from
AssetRipper's mesh GLB (checked against a real AssetRipper export in the
tests): Unity -> glTF space by mirroring X (position and normal ``(-x, y, z)``),
triangle winding reversed to keep faces outward after the mirror, UV ``v``
flipped (``1 - v``), one primitive per submesh, and ``JOINTS_0`` /
``WEIGHTS_0`` holding the renderer's bone indices and weights.

Real meshes also come without weights: one bone per vertex (Unity stores only
the bone index; weight 1 is implied) or no skin data at all (e.g. eyes that
follow their renderer's transform). The latter are written with every vertex
on joint 0 and marked ``extras.aniimo_rigid``; conversion binds that joint to
the renderer's own transform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from aniimo_pal.convert.skin_assembly import write_glb
from aniimo_pal.errors import ToolkitError

FLOAT, UINT16, UINT32 = 5126, 5123, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


class _Builder:
    def __init__(self) -> None:
        self.binary = bytearray()
        self.views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []

    def add(self, array: np.ndarray, component: int, kind: str, target: int, minmax: bool = False) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        data = array.tobytes()
        self.views.append({"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data), "target": target})
        self.binary += data
        acc: dict[str, Any] = {"bufferView": len(self.views) - 1, "componentType": component,
                               "count": int(array.shape[0]), "type": kind}
        if minmax:
            acc["min"] = [float(x) for x in array.min(axis=0)]
            acc["max"] = [float(x) for x in array.max(axis=0)]
        self.accessors.append(acc)
        return len(self.accessors) - 1


def mesh_arrays(mesh: Any) -> dict[str, Any]:
    """Vertex/index data of a UnityPy Mesh object in Unity space (no conversion)."""
    from UnityPy.helpers.MeshHelper import MeshHandler

    h = MeshHandler(mesh)
    h.process()
    if not h.m_Vertices:
        raise ToolkitError(f"mesh {getattr(mesh, 'm_Name', '?')!r} has no vertices (empty or runtime-generated)")
    n = len(h.m_Vertices)
    out: dict[str, Any] = {"name": getattr(mesh, "m_Name", None) or "mesh",
                           "positions": np.asarray(h.m_Vertices, dtype=np.float32).reshape(n, -1)[:, :3],
                           "submeshes": [list(tri) for tri in h.get_triangles()]}
    if h.m_Normals and len(h.m_Normals) == n:
        out["normals"] = np.asarray(h.m_Normals, dtype=np.float32).reshape(n, -1)[:, :3]
    uv0 = getattr(h, "m_UV0", None)
    if uv0 and len(uv0) == n:
        out["uv0"] = np.asarray(uv0, dtype=np.float32).reshape(n, -1)[:, :2]
    if h.m_BoneIndices and len(h.m_BoneIndices) == n:
        out["joints"] = _pad4(np.asarray(h.m_BoneIndices, dtype=np.int64).reshape(n, -1))
        if h.m_BoneWeights and len(h.m_BoneWeights) == n:
            out["weights"] = _pad4(np.asarray(h.m_BoneWeights, dtype=np.float32).reshape(n, -1))
            out["skin_source"] = "stored-weights"
        else:
            # One bone per vertex: Unity stores only the bone index (no weight channel).
            out["weights"] = np.zeros((n, 4), dtype=np.float32)
            out["weights"][:, 0] = 1.0
            out["skin_source"] = "single-bone"
    else:
        out["skin_source"] = "none"  # rigid: follows its renderer's transform
    return out


def _pad4(a: np.ndarray) -> np.ndarray:
    if a.shape[1] < 4:
        a = np.concatenate([a, np.zeros((a.shape[0], 4 - a.shape[1]), dtype=a.dtype)], axis=1)
    return a[:, :4]


def arrays_to_glb(arrays: dict[str, Any], out_path: str | Path) -> dict[str, Any]:
    """Write mesh arrays (Unity space) as a glTF-space GLB; returns a short report."""
    b = _Builder()
    mirror = np.array([-1.0, 1.0, 1.0], dtype=np.float32)
    attributes = {"POSITION": b.add(np.ascontiguousarray(arrays["positions"] * mirror), FLOAT, "VEC3",
                                    ARRAY_BUFFER, minmax=True)}
    if "normals" in arrays:
        normals = arrays["normals"] * mirror
        length = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.where(length > 0, normals / np.where(length > 0, length, 1), normals)
        attributes["NORMAL"] = b.add(np.ascontiguousarray(normals, dtype=np.float32), FLOAT, "VEC3", ARRAY_BUFFER)
    if "uv0" in arrays:
        uv = arrays["uv0"].copy()
        uv[:, 1] = 1.0 - uv[:, 1]
        attributes["TEXCOORD_0"] = b.add(np.ascontiguousarray(uv, dtype=np.float32), FLOAT, "VEC2", ARRAY_BUFFER)
    skinned = "joints" in arrays
    rigid = not skinned
    if rigid:
        # No skin data: bind every vertex to one joint (the renderer's transform, chosen at assembly time).
        n = arrays["positions"].shape[0]
        arrays = dict(arrays, joints=np.zeros((n, 4), dtype=np.int64),
                      weights=np.tile(np.array([1.0, 0, 0, 0], dtype=np.float32), (n, 1)))
    joints = arrays["joints"]
    if joints.min() < 0 or joints.max() > 65535:
        raise ToolkitError("bone index out of range for glTF JOINTS_0")
    weights = arrays["weights"].astype(np.float32)
    total = weights.sum(axis=1, keepdims=True)
    weights = np.where(total > 0, weights / np.where(total > 0, total, 1), weights)
    attributes["JOINTS_0"] = b.add(np.ascontiguousarray(joints, dtype=np.uint16), UINT16, "VEC4", ARRAY_BUFFER)
    attributes["WEIGHTS_0"] = b.add(np.ascontiguousarray(weights, dtype=np.float32), FLOAT, "VEC4", ARRAY_BUFFER)
    primitives = []
    triangles = 0
    for tris in arrays["submeshes"]:
        tris = [t for t in tris if len(t) == 3]
        if not tris:
            continue
        idx = np.asarray(tris, dtype=np.uint32)[:, [0, 2, 1]]  # mirrored X: reverse winding
        triangles += len(idx)
        primitives.append({"attributes": dict(attributes),
                           "indices": b.add(idx.reshape(-1), UINT32, "SCALAR", ELEMENT_ARRAY_BUFFER),
                           "mode": 4})
    if not primitives:
        raise ToolkitError(f"mesh {arrays['name']!r} has no triangles")
    doc = {"asset": {"version": "2.0", "generator": "aniimo-pal unity_mesh_glb"},
           "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"name": arrays["name"], "mesh": 0, "extras": {"aniimo_skin_source": arrays.get(
               "skin_source", "none" if rigid else "stored-weights"), "aniimo_rigid": rigid}}],
           "meshes": [{"name": arrays["name"], "primitives": primitives}],
           "buffers": [{"byteLength": len(b.binary)}], "bufferViews": b.views, "accessors": b.accessors}
    write_glb(out_path, doc, bytes(b.binary))
    return {"vertices": int(arrays["positions"].shape[0]), "triangles": triangles, "submeshes": len(primitives),
            "skinned": skinned, "rigid": rigid}


def glb_is_rigid(path: str | Path) -> bool:
    """True if the GLB was written for a mesh without skin data (see :func:`arrays_to_glb`)."""
    from aniimo_pal.convert.skin_assembly import read_glb

    doc, _ = read_glb(path)
    return any((n.get("extras") or {}).get("aniimo_rigid") for n in doc.get("nodes", []))


def unity_mesh_to_glb(mesh: Any, out_path: str | Path) -> dict[str, Any]:
    return arrays_to_glb(mesh_arrays(mesh), out_path)

