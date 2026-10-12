"""Conversion stage: Unity .anim parsing (pure Python) and Blender jobs (needs the bpy module)."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from aniimo_pal.convert.unity_anim import evaluate_track, parse_anim, parse_anim_file, sample_times

FIXTURES = Path(__file__).parent / "fixtures"

# Unity space -> Blender space as produced by AssetRipper (glTF) + Blender's glTF importer.
D = np.array([[-1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def test_parse_real_assetripper_anim():
    """Fixture: AssetRipper's reconstruction of a synthetic compiled (dense) clip."""
    clip = parse_anim_file(FIXTURES / "Foxlet_Wag.anim")
    assert clip["name"] == "Foxlet_Wag" and clip["sample_rate"] == 30.0
    assert clip["start_time"] == 0.0 and clip["stop_time"] == pytest.approx(2 / 30)
    tracks = {(t["path"], t["channel"]): t for t in clip["tracks"]}
    assert set(tracks) == {("Root/Hips/Tail", "rotation"), ("Root", "position")}
    rot = tracks[("Root/Hips/Tail", "rotation")]
    assert rot["keys"][2]["v"] == pytest.approx([0, 0, 0.5, 0.8660254])  # 60 degrees about Z
    assert clip["events"] == [{"time": 0.0333, "function": "OnHit", "data": None, "float": 0, "int": 0}]
    assert sample_times(clip) == pytest.approx([0, 1 / 30, 2 / 30])
    assert clip["space"] == "unity-local" and clip["warnings"] == []


def test_evaluate_hermite_and_quaternion_normalisation():
    track = {"channel": "position", "keys": [
        {"t": 0.0, "v": [0.0], "in": [0.0], "out": [1.0]},
        {"t": 1.0, "v": [1.0], "in": [1.0], "out": [0.0]}]}
    assert evaluate_track(track, 0.5) == pytest.approx([0.5])  # linear tangents -> straight line
    assert evaluate_track(track, -1) == [0.0] and evaluate_track(track, 2) == [1.0]
    flat = {"channel": "position", "keys": [
        {"t": 0.0, "v": [0.0], "in": [0.0], "out": [0.0]},
        {"t": 1.0, "v": [1.0], "in": [0.0], "out": [0.0]}]}
    assert evaluate_track(flat, 0.25) == pytest.approx([0.15625])  # smoothstep
    stepped = {"channel": "position", "keys": [
        {"t": 0.0, "v": [0.0], "in": [0.0], "out": [math.inf]},
        {"t": 1.0, "v": [1.0], "in": [math.inf], "out": [0.0]}]}
    assert evaluate_track(stepped, 0.9) == [0.0]
    q = {"channel": "rotation", "keys": [
        {"t": 0.0, "v": [0, 0, 0, 1], "in": [0] * 4, "out": [0] * 4},
        {"t": 1.0, "v": [0, 0, 1, 0], "in": [0] * 4, "out": [0] * 4}]}
    assert sum(c * c for c in evaluate_track(q, 0.5)) == pytest.approx(1.0)


def test_parse_errors_and_weighted_warning():
    from aniimo_pal.errors import ToolkitError

    with pytest.raises(ToolkitError):
        parse_anim("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!1 &1\nGameObject:\n  m_Name: x\n")
    text = (FIXTURES / "Foxlet_Wag.anim").read_text().replace("weightedMode: 0", "weightedMode: 3", 1)
    assert parse_anim(text)["warnings"]


# --- Blender (bpy module) -------------------------------------------------------------

def _q(axis, deg):
    a = math.radians(deg) / 2
    n = math.sqrt(sum(c * c for c in axis))
    return [axis[0] / n * math.sin(a), axis[1] / n * math.sin(a), axis[2] / n * math.sin(a), math.cos(a)]


SKELETON = {
    "Root": {"position": [0, 0.5, 0], "rotation": [0, 0, 0, 1]},
    "Root/Hips": {"position": [0, 0.2, 0.1], "rotation": _q([0, 1, 0], 30)},
    "Root/Hips/Tail": {"position": [0.1, 0, -0.3], "rotation": _q([1, 0, 0], -20)},
    "Root/Hips/Spine": {"position": [0, 0.25, 0], "rotation": [0, 0, 0, 1]},
}


def _unity_world(skeleton, overrides):
    """Independent reference: Unity world positions of every bone."""
    def trs(t, r, s=(1, 1, 1)):
        x, y, z, w = r
        m = np.eye(4)
        m[:3, :3] = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                              [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                              [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]) * np.array(s)
        m[:3, 3] = t
        return m
    world = {}
    for p in sorted(skeleton, key=lambda p: p.count("/")):
        loc = dict(skeleton[p], **overrides.get(p, {}))
        m = trs(loc["position"], loc["rotation"])
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        world[p] = world[parent] @ m if parent else m
    return {p: m[:3, 3] for p, m in world.items()}


@pytest.fixture
def bpy_mod():
    return pytest.importorskip("bpy")


def _model(tmp_path):
    import gltf_builder as gb

    return gb.skinned_gltf(tmp_path / "creature.gltf", SKELETON)


def test_convert_static_model(bpy_mod, tmp_path):
    from aniimo_pal.convert.blender_runner import run_job

    report = run_job({"input_model": str(_model(tmp_path)), "output_fbx": str(tmp_path / "out" / "m.fbx"),
                      "report": str(tmp_path / "r.json")})
    assert (tmp_path / "out" / "m.fbx").stat().st_size > 1000
    assert [m["name"] for m in report["meshes"]] == ["Creature_Body"]  # no importer bone-shape helper meshes
    mesh = report["meshes"][0]
    assert mesh["skinned"] and mesh["vertex_groups"] == 4 and mesh["unweighted_vertices"] == 0
    assert report["armatures"][0]["name"] == "Armature"
    assert {b["name"] for b in report["armatures"][0]["bones"]} == {"Root", "Hips", "Tail", "Spine"}
    assert report["clips"] == [] and report["warnings"] == []
    assert report["fbx_settings"]["add_leaf_bones"] is False
    assert json.loads((tmp_path / "r.json").read_text())["output_fbx"].endswith("m.fbx")


def test_clip_transfer_survives_fbx_roundtrip(bpy_mod, tmp_path):
    """Pose bone heads after FBX export + re-import must match Unity-space math (mapped by D)."""
    from aniimo_pal.convert.blender_runner import run_job

    clip = parse_anim_file(FIXTURES / "Foxlet_Wag.anim")
    out = tmp_path / "anim.fbx"
    report = run_job({"input_model": str(_model(tmp_path)), "output_fbx": str(out), "unity_skeleton": SKELETON,
                      "clips": [clip]})
    assert report["basis"]["method"] == "expected-assetripper-basis"
    assert report["basis"]["rms_residual_m"] < 1e-6
    c = report["clips"][0]
    assert c["bones_driven"] == 4 and c["unknown_paths"] == [] and c["max_head_error_m"] < 1e-6
    assert c["events"][0]["function"] == "OnHit"

    bpy = bpy_mod
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    bpy.ops.import_scene.fbx(filepath=str(out))
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    assert arm.animation_data and arm.animation_data.action
    assert {a.name for a in bpy.data.actions} >= {arm.animation_data.action.name}
    scene = bpy.context.scene
    for f, t in enumerate(sample_times(clip)):
        overrides = {}
        for tr in clip["tracks"]:
            overrides.setdefault(tr["path"], {})[tr["channel"]] = evaluate_track(tr, t)
        expected = _unity_world(SKELETON, overrides)
        scene.frame_set(1 + f)
        for path, pos in expected.items():
            pb = arm.pose.bones[path.rsplit("/", 1)[-1]]
            head = np.array(arm.matrix_world @ pb.head)
            assert np.allclose(head, D @ pos, atol=1e-4), (f, path, head, D @ pos)


def test_bone_map_and_missing_bone_warning(bpy_mod, tmp_path):
    from aniimo_pal.convert.blender_runner import run_job

    report = run_job({"input_model": str(_model(tmp_path)), "output_fbx": str(tmp_path / "m.fbx"),
                      "bone_map": {"Hips": "pelvis", "Nope": "x"}})
    names = {b["name"] for b in report["armatures"][0]["bones"]}
    assert "pelvis" in names and "Hips" not in names
    assert report["renamed_bones"] == {"Hips": "pelvis"}
    assert any("Nope" in w for w in report["warnings"])


def test_job_errors(bpy_mod, tmp_path):
    from aniimo_pal.convert.blender_runner import BlenderJobError, run_job

    with pytest.raises(BlenderJobError, match="unity_skeleton"):
        run_job({"input_model": str(_model(tmp_path)), "output_fbx": str(tmp_path / "m.fbx"),
                 "clips": [parse_anim_file(FIXTURES / "Foxlet_Wag.anim")]})
    with pytest.raises(BlenderJobError, match="unsupported"):
        run_job({"input_model": str(tmp_path / "x.obj"), "output_fbx": str(tmp_path / "m.fbx")})


def test_external_blender_failure_is_reported(tmp_path):
    from aniimo_pal.convert.blender_runner import BlenderJobError, run_job

    with pytest.raises(BlenderJobError, match="cannot run Blender"):
        run_job({"input_model": "x", "output_fbx": "y"}, blender=str(tmp_path / "no-blender"))
