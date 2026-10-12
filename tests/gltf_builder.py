"""Write small skinned glTF files from a Unity-space skeleton (tests only).

Mirrors AssetRipper's Unity->glTF conversion (GlbCoordinateConversion.cs):
positions (x, y, z) -> (-x, y, z); quaternions (x, y, z, w) -> (x, -y, -z, w).
"""

from __future__ import annotations

import base64
import json
import math
import struct
from pathlib import Path

import numpy as np


def _u2g_pos(p):
    return [-p[0], p[1], p[2]]


def _u2g_rot(q):
    return [q[0], -q[1], -q[2], q[3]]


def _trs(t, r, s):
    x, y, z, w = r
    m = np.eye(4)
    m[:3, :3] = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]) * np.array(s)[None, :]
    m[:3, 3] = t
    return m


def quat_axis_angle(axis, degrees):
    a = math.radians(degrees) / 2
    n = math.sqrt(sum(c * c for c in axis))
    return [axis[0] / n * math.sin(a), axis[1] / n * math.sin(a), axis[2] / n * math.sin(a), math.cos(a)]


def skinned_gltf(path: Path, skeleton: dict[str, dict], root_name: str = "Creature") -> Path:
    """skeleton: {unity_path: {"position", "rotation", "scale"}} for bones (no "" entry)."""
    paths = sorted(skeleton, key=lambda p: (p.count("/"), p))
    nodes = [{"name": root_name, "children": []}]
    index = {}
    world = {}
    for p in paths:
        loc = skeleton[p]
        node = {"name": p.rsplit("/", 1)[-1], "translation": _u2g_pos(loc["position"]),
                "rotation": _u2g_rot(loc["rotation"]), "scale": list(loc.get("scale", [1, 1, 1]))}
        index[p] = len(nodes)
        nodes.append(node)
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        m = _trs(node["translation"], node["rotation"], node["scale"])
        if parent:
            nodes[index[parent]].setdefault("children", []).append(index[p])
            world[p] = world[parent] @ m
        else:
            nodes[0]["children"].append(index[p])
            world[p] = m
    # One quad per bone (4 vertices around the joint), fully weighted to that bone.
    positions, joints, weights, indices = [], [], [], []
    for j, p in enumerate(paths):
        c = world[p][:3, 3]
        base = len(positions)
        for dx, dz in ((-0.02, -0.02), (0.02, -0.02), (0.02, 0.02), (-0.02, 0.02)):
            positions.append([c[0] + dx, c[1], c[2] + dz])
            joints.append([j, 0, 0, 0])
            weights.append([1.0, 0.0, 0.0, 0.0])
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    ibms = [np.linalg.inv(world[p]) for p in paths]
    pos_b = b"".join(struct.pack("<3f", *v) for v in positions)
    jnt_b = b"".join(struct.pack("<4H", *v) for v in joints)
    wgt_b = b"".join(struct.pack("<4f", *v) for v in weights)
    idx_b = b"".join(struct.pack("<H", i) for i in indices)
    idx_b += b"\x00" * ((4 - len(idx_b) % 4) % 4)
    ibm_b = b"".join(struct.pack("<16f", *m.T.flatten()) for m in ibms)  # column-major
    blobs = [pos_b, jnt_b, wgt_b, idx_b, ibm_b]
    views, offset = [], 0
    for b in blobs:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(b)})
        offset += len(b)
    pos = np.array(positions)
    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
         "min": pos.min(axis=0).tolist(), "max": pos.max(axis=0).tolist()},
        {"bufferView": 1, "componentType": 5123, "count": len(joints), "type": "VEC4"},
        {"bufferView": 2, "componentType": 5126, "count": len(weights), "type": "VEC4"},
        {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        {"bufferView": 4, "componentType": 5126, "count": len(ibms), "type": "MAT4"},
    ]
    mesh_node = len(nodes)
    nodes.append({"name": f"{root_name}_Body", "mesh": 0, "skin": 0})
    nodes[0]["children"].append(mesh_node)
    doc = {
        "asset": {"version": "2.0", "generator": "aniimo-pal tests"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": nodes,
        "meshes": [{"name": f"{root_name}_Body", "primitives": [{
            "attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2}, "indices": 3,
            "material": 0}]}],
        "materials": [{"name": f"{root_name}_Mat", "pbrMetallicRoughness": {"baseColorFactor": [1, 0.5, 0.2, 1]}}],
        "skins": [{"joints": [index[p] for p in paths], "inverseBindMatrices": 4, "skeleton": index[paths[0]]}],
        "accessors": accessors, "bufferViews": views,
        "buffers": [{"byteLength": offset,
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(b"".join(blobs)).decode()}],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def assetripper_like_mesh_glb(path: Path, skeleton: dict[str, dict], joints: list[str]) -> Path:
    """A GLB shaped like AssetRipper's mesh export: positions + JOINTS_0/WEIGHTS_0, but NO skin.

    Vertices: one quad per joint around the joint's Unity world position, converted to glTF (-x, y, z).
    """
    from aniimo_pal.convert.skin_assembly import unity_world, write_glb

    world = unity_world(skeleton)
    positions, jidx, weights, indices = [], [], [], []
    for j, p in enumerate(joints):
        c = world[p][:3, 3]
        base = len(positions)
        for dx, dz in ((-0.02, -0.02), (0.02, -0.02), (0.02, 0.02), (-0.02, 0.02)):
            positions.append([-(c[0] + dx), c[1], c[2] + dz])
            jidx.append([j, 0, 0, 0])
            weights.append([1.0, 0.0, 0.0, 0.0])
        indices += [base, base + 2, base + 1, base, base + 3, base + 2]
    blobs = [b"".join(struct.pack("<3f", *v) for v in positions), b"".join(struct.pack("<4H", *v) for v in jidx),
             b"".join(struct.pack("<4f", *v) for v in weights), b"".join(struct.pack("<H", i) for i in indices)]
    blobs[3] += b"\x00" * ((4 - len(blobs[3]) % 4) % 4)
    views, offset = [], 0
    for b in blobs:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(b)})
        offset += len(b)
    pos = np.array(positions)
    doc = {
        "asset": {"version": "2.0", "generator": "SharpGLTF-like test"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Creature_Body", "children": [1]}, {"name": "SubMesh_0", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2}, "indices": 3}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3",
             "min": pos.min(axis=0).tolist(), "max": pos.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": 5123, "count": len(jidx), "type": "VEC4"},
            {"bufferView": 2, "componentType": 5126, "count": len(weights), "type": "VEC4"},
            {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
        "bufferViews": views, "buffers": [{"byteLength": offset}],
    }
    return write_glb(path, doc, b"".join(blobs))
