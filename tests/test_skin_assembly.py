import json
import math

import numpy as np
import pytest

pytest.importorskip("numpy")

import gltf_builder as gb  # noqa: E402

from aniimo_pal.convert.skin_assembly import (  # noqa: E402
    assemble_skinned_glb,
    read_glb,
    skeleton_locals,
    unity_world,
    write_glb,
)
from aniimo_pal.errors import ToolkitError  # noqa: E402


def _q(axis, deg):
    a = math.radians(deg) / 2
    n = math.sqrt(sum(c * c for c in axis))
    return [axis[0] / n * math.sin(a), axis[1] / n * math.sin(a), axis[2] / n * math.sin(a), math.cos(a)]


SKELETON = {
    "Root": {"position": [0, 0.5, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]},
    "Root/Hips": {"position": [0, 0.2, 0.1], "rotation": _q([0, 1, 0], 30), "scale": [1, 1, 1]},
    "Root/Hips/Spine": {"position": [0, 0.25, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]},
    "Root/Hips/Tail": {"position": [0.1, 0, -0.3], "rotation": _q([1, 0, 0], -20), "scale": [1, 1, 1]},
}
JOINTS = ["Root", "Root/Hips", "Root/Hips/Tail", "Root/Hips/Spine"]  # renderer order, not sorted


def test_glb_roundtrip(tmp_path):
    p = write_glb(tmp_path / "x.glb", {"asset": {"version": "2.0"}}, b"\x01\x02\x03")
    doc, binary = read_glb(p)
    assert doc == {"asset": {"version": "2.0"}} and binary[:3] == b"\x01\x02\x03"
    (tmp_path / "bad.glb").write_bytes(b"nope")
    with pytest.raises(ToolkitError):
        read_glb(tmp_path / "bad.glb")


def test_assembly_structure_and_bind_matrices(tmp_path):
    mesh = gb.assetripper_like_mesh_glb(tmp_path / "mesh.glb", SKELETON, JOINTS)
    rep = assemble_skinned_glb(mesh, SKELETON, JOINTS, tmp_path / "rigged.glb", root_name="aniimo_test")
    assert rep["joints"] == 4 and rep["skinned_mesh_nodes"] == 1 and rep["bind_pose_source"] == "skeleton-rest-pose"
    doc, binary = read_glb(tmp_path / "rigged.glb")
    skin = doc["skins"][0]
    names = [doc["nodes"][j]["name"] for j in skin["joints"]]
    assert names == ["Root", "Hips", "Tail", "Spine"]  # m_Bones order preserved
    hips = next(n for n in doc["nodes"] if n.get("name") == "Hips")
    assert hips["rotation"] == pytest.approx([0, -_q([0, 1, 0], 30)[1], 0, _q([0, 1, 0], 30)[3]])
    tail = next(n for n in doc["nodes"] if n.get("name") == "Tail")
    assert tail["translation"] == pytest.approx([-0.1, 0, -0.3])
    # IBM of each joint = inverse of its glTF world matrix.
    acc = doc["accessors"][skin["inverseBindMatrices"]]
    view = doc["bufferViews"][acc["bufferView"]]
    ibms = np.frombuffer(binary[view["byteOffset"]:view["byteOffset"] + view["byteLength"]], dtype="<f4")
    ibms = ibms.reshape(-1, 4, 4).transpose(0, 2, 1)
    world = unity_world(SKELETON)
    flip = np.diag([-1.0, 1, 1, 1])
    for k, j in enumerate(JOINTS):
        assert np.allclose(ibms[k] @ (flip @ world[j] @ flip), np.eye(4), atol=1e-5)
    mesh_node = next(n for n in doc["nodes"] if "mesh" in n)
    assert mesh_node["skin"] == 0
    assert doc["scenes"][0]["nodes"] == [next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "aniimo_test")]


def test_assembly_uses_supplied_bind_poses_and_warns_on_mismatch(tmp_path):
    mesh = gb.assetripper_like_mesh_glb(tmp_path / "mesh.glb", SKELETON, JOINTS)
    world = unity_world(SKELETON)
    binds = [np.linalg.inv(world[j]).tolist() for j in JOINTS]
    rep = assemble_skinned_glb(mesh, SKELETON, JOINTS, tmp_path / "a.glb", bind_poses=binds)
    assert rep["bind_pose_source"] == "mesh-bind-poses" and rep["warnings"] == []
    binds[0][1][3] += 0.5
    rep = assemble_skinned_glb(mesh, SKELETON, JOINTS, tmp_path / "b.glb", bind_poses=binds)
    assert any("bind pose differs" in w for w in rep["warnings"])
    with pytest.raises(ToolkitError, match="bind poses for"):
        assemble_skinned_glb(mesh, SKELETON, JOINTS, tmp_path / "c.glb", bind_poses=binds[:2])


def test_assembly_errors(tmp_path):
    mesh = gb.assetripper_like_mesh_glb(tmp_path / "mesh.glb", SKELETON, JOINTS)
    with pytest.raises(ToolkitError, match="missing from the skeleton"):
        assemble_skinned_glb(mesh, SKELETON, ["Root", None], tmp_path / "x.glb")
    doc, binary = read_glb(mesh)
    del doc["meshes"][0]["primitives"][0]["attributes"]["JOINTS_0"]
    write_glb(tmp_path / "rigid.glb", doc, binary)
    with pytest.raises(ToolkitError, match="nothing to skin"):
        assemble_skinned_glb(tmp_path / "rigid.glb", SKELETON, JOINTS, tmp_path / "y.glb")


def test_skeleton_locals_from_catalog_bones():
    bones = [{"path": "Root", "local": {"position": {"x": 1, "y": 2, "z": 3},
                                        "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                                        "scale": {"x": 1, "y": 1, "z": 1}}},
             {"path": "Root/A", "local": None}]
    out = skeleton_locals({"bones": bones})
    assert out["Root"]["position"] == [1, 2, 3]
    assert out["Root/A"] == {"position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]}
    json.dumps(out)


def test_rigged_mesh_vertices_sit_on_their_bones(tmp_path):
    bpy = pytest.importorskip("bpy")
    from aniimo_pal.convert.blender_runner import run_job

    mesh = gb.assetripper_like_mesh_glb(tmp_path / "mesh.glb", SKELETON, JOINTS)
    assemble_skinned_glb(mesh, SKELETON, JOINTS, tmp_path / "rigged.glb")
    report = run_job({"input_model": str(tmp_path / "rigged.glb"), "output_fbx": str(tmp_path / "m.fbx")})
    assert report["meshes"][0]["skinned"] and report["meshes"][0]["unweighted_vertices"] == 0
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    obj = next(o for o in bpy.data.objects if o.type == "MESH")
    for v in obj.data.vertices:
        group = max(v.groups, key=lambda g: g.weight)
        bone = arm.data.bones[obj.vertex_groups[group.group].name]
        dist = ((obj.matrix_world @ v.co) - (arm.matrix_world @ bone.head_local)).length
        assert dist < 0.05, (bone.name, dist)
