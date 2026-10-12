"""Creature-level conversion: catalog skeleton + AssetRipper outputs -> rigged FBX with clips."""

import json
import math
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("UnityPy")
pytest.importorskip("numpy")

import unity_builder as ub  # noqa: E402

from aniimo_pal.catalog import build_catalog  # noqa: E402
from aniimo_pal.cli import EXIT_OK, main  # noqa: E402
from aniimo_pal.config import ScanConfig  # noqa: E402
from aniimo_pal.creature.model import creature_path, load_creature, save_creature  # noqa: E402
from aniimo_pal.creature.workflow import creature_from_catalog  # noqa: E402
from aniimo_pal.inventory import build_inventory  # noqa: E402
from aniimo_pal.scanner import scan  # noqa: E402


def _q(axis, deg):
    a = math.radians(deg) / 2
    n = math.sqrt(sum(c * c for c in axis))
    return tuple([c / n * math.sin(a) for c in axis] + [math.cos(a)])


BONES = {
    "Root": ((0, 0.5, 0), (0, 0, 0, 1)),
    "Root/Hips": ((0, 0.2, 0.1), _q([0, 1, 0], 30)),
    "Root/Hips/Spine": ((0, 0.25, 0), (0, 0, 0, 1)),
    "Root/Hips/Spine/Head": ((0, 0.2, 0.15), _q([1, 0, 0], 15)),
    "Root/Hips/Tail": ((0.1, 0, -0.3), _q([1, 0, 0], -20)),
}


@pytest.fixture
def skinned_game(tmp_path):
    b = ub.SerializedBuilder()
    ids = ub.add_creature(b, "Foxlet", bones=list(BONES), clips=[], bone_locals=BONES,
                          variants={"shiny": (240, 220, 60)})
    clip = ub.add_dense_clip(b, "Foxlet_Wag", [
        ("Root/Hips/Tail", ub.ATTR_ROTATION, [_q([0, 0, 1], 0), _q([0, 0, 1], 30), _q([0, 0, 1], 60)]),
        ("Root", ub.ATTR_POSITION, [(0, 0.5, 0), (0, 0.6, 0), (0, 0.7, 0)])], events=[(1 / 30, "OnHit")])
    ctrl = next(o for o in b._objects if o.class_id == 91)
    ctrl.data["m_AnimationClips"] = [ub.pptr(clip)]
    ub.add_assetbundle(b, "creatures/foxlet", {"assets/creatures/foxlet/foxlet.prefab": ids["normal"].root_go,
                                               "assets/creatures/foxlet/foxlet_shiny.prefab": ids["shiny"].root_go})
    root = tmp_path / "game"
    root.mkdir()
    (root / "foxlet.bundle").write_bytes(ub.bundle(b.build(), "CAB-foxlet"))
    return root


def test_catalog_records_skin_joint_order(skinned_game):
    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    skins = cat["species"][0]["skeleton"]["skins"]
    assert len(skins) == 1 and skins[0]["bones"] == list(BONES) and skins[0]["mesh_name"] == "Foxlet_Body"


def test_convert_with_simulated_assetripper_outputs(skinned_game, tmp_path):
    pytest.importorskip("bpy")
    import gltf_builder as gb

    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.convert.skin_assembly import skeleton_locals
    from aniimo_pal.extract import extract_creature

    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    c = creature_from_catalog(cat, "foxlet")
    cdir = creature_path(tmp_path / "ws", c["id"]).parent
    save_creature(c, cdir / "creature.json")
    extract_creature(c, cdir, skinned_game)  # writes bind poses next to the OBJ
    mesh = next(a for a in c["assets"] if a["role"] == "mesh")
    assert (cdir / (mesh["extracted"][:-4] + ".skin.json")).is_file()

    # Simulate the two AssetRipper exports (the real ones are covered by the ASSETRIPPER_PATH test).
    locals_ = skeleton_locals(c["skeleton"])
    glb = cdir / "extract/assetripper/primary/Assets/Mesh/Foxlet_Body.glb"
    glb.parent.mkdir(parents=True)
    gb.assetripper_like_mesh_glb(glb, locals_, list(BONES))
    mesh["assetripper"] = {"primary": [str(glb.relative_to(cdir))]}
    anim = cdir / "extract/assetripper/project/Foxlet_Wag.anim"
    anim.parent.mkdir(parents=True)
    shutil.copy(ub_fixture(), anim)
    clip_asset = next(a for a in c["assets"] if a["role"] == "animation")
    clip_asset["assetripper"] = {"project": [str(anim.relative_to(cdir))]}

    report = convert_creature(c, cdir)
    assert report["summary"] == {"meshes_ok": 1, "errors": 0, "clips": 1, "clip_errors": 0}
    m = report["meshes"][0]
    assert (cdir / m["fbx"]).stat().st_size > 1000
    model = report["model"]
    assert model["status"] == "ok" and model["fbx"] == m["fbx"] and model["parts"] == 1
    assert model["assembly"]["parts"][0]["bind_pose_source"] == "mesh-bind-poses"
    assert model["assembly"]["warnings"] == []
    assert model["blender"]["basis"]["method"] == "expected-assetripper-basis"
    assert m["clips"][0]["bones_driven"] == 5 and m["clips"][0]["max_head_error_m"] < 1e-6
    assert c["status"] == "converted" and mesh["converted"] == m["fbx"]
    assert json.loads((cdir / "convert/report.json").read_text())["creature"] == "aniimo_foxlet"

    # 3D preview: every variant side by side, every animation on the timeline.
    from aniimo_pal.convert.preview import build_preview

    preview = build_preview(c, cdir)
    assert [v["label"] for v in preview["variants"]] == ["Normal", "Shiny (look not identified)"]
    assert all(v["texture"] and Path(v["texture"]).is_file() for v in preview["variants"])
    assert [t["action"] for t in preview["timeline"]] == ["Foxlet_Wag"]
    # The wagging tail and moving root are framed: animated bounds contain the rest bounds.
    (rlo, rhi), (alo, ahi) = preview["rest_bounds"], preview["animated_bounds"]
    assert all(a <= r for a, r in zip(alo, rlo, strict=True)) and all(a >= r for a, r in zip(ahi, rhi, strict=True))
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "aniimo_pal" / "convert" / "blender_scripts"))
    from preview_scene import label_lines

    assert label_lines("Shiny (look not identified) [NG_P_Parmon_10351_Shiny] - NO TEXTURE") == [
        "Shiny (look not identified)", "[NG_P_Parmon_10351_Shiny]", "NO TEXTURE"]
    assert (cdir / "preview" / "preview.blend").is_file()
    assert preview["render"] is None and "Blender executable" in preview["render_error"]  # bpy module: no still


def ub_fixture():
    from pathlib import Path

    return Path(__file__).parent / "fixtures" / "Foxlet_Wag.anim"


def test_convert_requires_extracted_mesh(skinned_game, tmp_path, capsys):
    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    c = creature_from_catalog(cat, "foxlet")
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    from aniimo_pal.convert.pipeline import convert_creature

    report = convert_creature(c, path.parent)
    assert report["meshes"][0]["status"] == "error" and "creature extract" in report["meshes"][0]["error"]
    assert c["status"] == "mapped"


def test_extract_writes_skinned_glb_and_convert_needs_no_assetripper_for_meshes(skinned_game, tmp_path):
    pytest.importorskip("bpy")
    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.convert.skin_assembly import read_glb
    from aniimo_pal.extract import extract_creature

    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    c = creature_from_catalog(cat, "foxlet")
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    extract_creature(c, path.parent, skinned_game, include_proposed=True)
    mesh = next(a for a in c["assets"] if a["role"] == "mesh")
    glb = path.parent / (mesh["extracted"][:-4] + ".glb")
    doc, _ = read_glb(glb)
    assert {"POSITION", "JOINTS_0", "WEIGHTS_0"} <= set(doc["meshes"][0]["primitives"][0]["attributes"])
    report = convert_creature(c, path.parent, include_proposed=True)
    assert report["meshes"][0]["status"] == "ok", report["meshes"][0]
    assert (path.parent / report["meshes"][0]["fbx"]).is_file()


@pytest.mark.skipif(not os.environ.get("ASSETRIPPER_PATH"), reason="set ASSETRIPPER_PATH to run against real AssetRipper")
def test_full_cli_pipeline_with_real_assetripper(skinned_game, tmp_path, capsys):
    """scan -> inventory -> catalog -> creature -> extract -> AssetRipper (primary+project) -> convert."""
    pytest.importorskip("bpy")
    ws, root = tmp_path / "ws", skinned_game
    ar = os.environ["ASSETRIPPER_PATH"]
    assert main(["scan", str(root), "-o", str(ws / "m.json"), "-q"]) == EXIT_OK
    assert main(["inventory", str(ws / "m.json"), str(root), "-o", str(ws / "o.json")]) == EXIT_OK
    assert main(["catalog", "build", str(ws / "o.json"), "-o", str(ws / "c.json")]) == EXIT_OK
    assert main(["creature", "init", str(ws), "--from-catalog", str(ws / "c.json"), "--species", "foxlet"]) == EXIT_OK
    cfile = ws / "creatures" / "aniimo_foxlet" / "creature.json"
    assert main(["creature", "extract", str(cfile), "--root", str(root)]) == EXIT_OK
    for mode in ("primary", "project"):
        assert main(["creature", "export-assetripper", str(cfile), "--root", str(root), "--mode", mode,
                     "--assetripper", ar]) == EXIT_OK
    capsys.readouterr()
    assert main(["creature", "convert", str(cfile)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "converted meshes 1, clips 1, errors 0" in out
    c = load_creature(cfile)
    assert c["status"] == "converted"
    report = json.loads((cfile.parent / "convert/report.json").read_text())
    m = report["model"]
    assert m["blender"]["meshes"][0]["skinned"] and m["blender"]["meshes"][0]["unweighted_vertices"] == 0
    assert m["clips"][0]["max_head_error_m"] < 1e-6 and report["meshes"][0]["fbx"] == m["fbx"]
    assert main(["verify", str(ws / "m.json"), str(root)]) == EXIT_OK  # game files untouched


@pytest.mark.skipif(not os.environ.get("ASSETRIPPER_PATH"), reason="set ASSETRIPPER_PATH to run against real AssetRipper")
def test_own_mesh_glb_matches_assetripper_triangle_by_triangle(skinned_game, tmp_path):
    import numpy as np
    import UnityPy

    from aniimo_pal.adapters.assetripper import AssetRipperSession
    from aniimo_pal.convert.skin_assembly import read_glb
    from aniimo_pal.convert.unity_mesh_glb import unity_mesh_to_glb

    with AssetRipperSession(os.environ["ASSETRIPPER_PATH"], tmp_path / "logs") as s:
        s.load_files([skinned_game / "foxlet.bundle"])
        s.export(tmp_path / "primary", "primary")
    ar_glb = next((tmp_path / "primary").rglob("Foxlet_Body.glb"))
    mesh = next(o.read() for o in UnityPy.load(str(skinned_game / "foxlet.bundle")).objects if o.type.name == "Mesh")
    unity_mesh_to_glb(mesh, tmp_path / "ours.glb")

    def corners(path):
        doc, binary = read_glb(path)
        prim = doc["meshes"][0]["primitives"][0]
        dtypes = {5126: np.float32, 5123: np.uint16, 5125: np.uint32, 5121: np.uint8}
        sizes = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

        def acc(i):
            a = doc["accessors"][i]
            v = doc["bufferViews"][a["bufferView"]]
            dt, n = np.dtype(dtypes[a["componentType"]]), sizes[a["type"]]
            start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
            stride = v.get("byteStride") or dt.itemsize * n
            raw = np.frombuffer(binary[start:start + stride * a["count"]], dtype=np.uint8).reshape(a["count"], stride)
            return np.frombuffer(raw[:, :dt.itemsize * n].tobytes(), dtype=dt).reshape(a["count"], n)

        attrs = {k: acc(i).astype(float) for k, i in prim["attributes"].items()
                 if k in ("POSITION", "JOINTS_0", "WEIGHTS_0")}
        out = []
        for tri in acc(prim["indices"]).reshape(-1, 3).astype(int):
            c = [tuple(np.round(np.concatenate([attrs[k][i] for k in sorted(attrs)]), 5)) for i in tri]
            k = min(range(3), key=lambda j: c[j])
            out.append(tuple(c[k:] + c[:k]))  # rotating keeps the winding
        return sorted(out)

    assert corners(tmp_path / "ours.glb") == corners(ar_glb)


def _add_rigid_eye(b, head_leaf="Head"):
    """An eye mesh without skin data on its own SkinnedMeshRenderer under the head bone (as in real Aniimo)."""
    eye_mesh = ub.add_triangle_mesh(b, "Foxlet_Eye", [])
    head_go = next(o.path_id for o in b._objects if o.class_id == 1 and o.data["m_Name"] == head_leaf)
    head_tr = next(o for o in b._objects if o.class_id == 4 and o.data["m_GameObject"]["m_PathID"] == head_go)
    go = b.add(1, m_Name="Eye", m_IsActive=True)
    tr = b.add(4, m_GameObject=ub.pptr(go), m_Father=ub.pptr(head_tr.path_id), m_LocalRotation={"w": 1.0},
               m_LocalPosition={"x": 0.0, "y": 0.05, "z": 0.05}, m_LocalScale={"x": 1.0, "y": 1.0, "z": 1.0})
    head_tr.data["m_Children"] = list(head_tr.data.get("m_Children") or []) + [ub.pptr(tr)]
    eye_tex = ub.add_texture(b, "Foxlet_Eye_Albedo", (20, 20, 200))
    eye_mat = ub.add_material(b, "Foxlet_Eye_Mat", {"_MainTex": eye_tex})
    smr = b.add(137, m_GameObject=ub.pptr(go), m_Enabled=True, m_Mesh=ub.pptr(eye_mesh), m_Bones=[],
                m_RootBone=ub.pptr(head_tr.path_id), m_Materials=[ub.pptr(eye_mat)])
    b.get(go)["m_Component"] = [{"component": ub.pptr(tr)}, {"component": ub.pptr(smr)}]


def test_body_and_rigid_eyes_become_one_model(tmp_path):
    pytest.importorskip("bpy")
    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.convert.skin_assembly import read_glb
    from aniimo_pal.extract import extract_creature

    b = ub.SerializedBuilder()
    ub.add_creature(b, "Foxlet", bones=list(BONES), clips=[], bone_locals=BONES)
    _add_rigid_eye(b)
    root = tmp_path / "game"
    root.mkdir()
    (root / "foxlet.bundle").write_bytes(ub.bundle(b.build(), "CAB-foxlet"))
    cat = build_catalog(build_inventory(scan(root, ScanConfig()), root))
    skins = cat["species"][0]["skeleton"]["skins"]
    assert sorted(s["mesh_name"] for s in skins) == ["Foxlet_Body", "Foxlet_Eye"]
    c = creature_from_catalog(cat, "foxlet")
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    extract_creature(c, path.parent, root, include_proposed=True)
    report = convert_creature(c, path.parent, include_proposed=True)
    by_mesh = {m["mesh"]: m for m in report["meshes"]}
    assert by_mesh["Foxlet_Eye"]["status"] == "ok" and by_mesh["Foxlet_Eye"]["attached_to"] == "Root/Hips/Spine/Head/Eye"
    model = report["model"]
    assert model["status"] == "ok" and model["parts"] == 2
    assert len(model["blender"]["armatures"]) == 1 and len(model["blender"]["meshes"]) == 2
    doc, _ = read_glb(path.parent / "convert" / c["id"] / f"{c['id']}.rigged.glb")
    assert len(doc["skins"]) == 2 and len([n for n in doc["nodes"] if "mesh" in n]) == 2

    from aniimo_pal.convert.preview import preview_job

    parts = preview_job(c, path.parent)["variants"][0]["part_textures"]
    assert parts["Foxlet_Eye"].endswith("Foxlet_Eye_Albedo.png") and parts["Foxlet_Body"].endswith("Foxlet_Albedo.png")
    from aniimo_pal.convert.preview import build_preview

    preview = build_preview(c, path.parent)
    assert (path.parent / "preview" / "preview.blend").is_file() and [v["label"] for v in preview["variants"]] == ["Normal"]


def test_part_hidden_by_default_is_left_out_of_the_model(tmp_path):
    pytest.importorskip("bpy")
    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.extract import extract_creature

    b = ub.SerializedBuilder()
    ub.add_creature(b, "Foxlet", bones=list(BONES), clips=[], bone_locals=BONES)
    _add_rigid_eye(b)
    eye_go = next(o for o in b._objects if o.class_id == 1 and o.data["m_Name"] == "Eye")
    eye_go.data["m_IsActive"] = False  # e.g. a "Rampage" part the game switches on later
    root = tmp_path / "game"
    root.mkdir()
    (root / "foxlet.bundle").write_bytes(ub.bundle(b.build(), "CAB-foxlet"))
    cat = build_catalog(build_inventory(scan(root, ScanConfig()), root))
    c = creature_from_catalog(cat, "foxlet")
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    extract_creature(c, path.parent, root, include_proposed=True)
    report = convert_creature(c, path.parent, include_proposed=True)
    by_mesh = {m["mesh"]: m for m in report["meshes"]}
    assert by_mesh["Foxlet_Eye"]["status"] == "skipped" and "hidden by default" in by_mesh["Foxlet_Eye"]["error"]
    assert report["model"]["status"] == "ok" and report["model"]["parts"] == 1


def test_bones_from_hashes_and_alternative_look_skipped(tmp_path):
    from aniimo_pal.catalog.builder import path_hash
    from aniimo_pal.convert.pipeline import bones_from_hashes, convert_creature

    paths = ["Root", "Root/Hips", "Root/Hips/Spine"]
    assert bones_from_hashes([path_hash("Root/Hips/Spine"), path_hash("Root")], paths) == ["Root/Hips/Spine", "Root"]
    assert bones_from_hashes([path_hash("Spine"), 1], paths) == ["Root/Hips/Spine", None]

    creature = {"id": "x", "assets": [], "skeleton": {"bones": [{"path": p} for p in paths], "skins": [
        {"mesh": "a", "mesh_name": "M_Fur01_Default_1", "bones": paths, "state": "default"},
        {"mesh": "b", "mesh_name": "M_Fur01_Rampage_1", "bones": paths, "state": "rampage"}]}}
    report = convert_creature(creature, tmp_path)
    by_mesh = {m["mesh"]: m for m in report["meshes"]}
    assert by_mesh["M_Fur01_Rampage_1"]["status"] == "skipped" and "rampage" in by_mesh["M_Fur01_Rampage_1"]["error"]
    assert by_mesh["M_Fur01_Default_1"]["error"] == "mesh asset not confirmed in this creature project"


def test_compiled_clip_decoded_by_extract_drives_the_fbx_without_assetripper(skinned_game, tmp_path):
    pytest.importorskip("bpy")
    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.extract import extract_creature

    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    c = creature_from_catalog(cat, cat["species"][0]["key"])
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    extract_creature(c, path.parent, skinned_game, include_proposed=True)
    wag = next(a for a in c["assets"] if a["role"] == "animation" and a["name"] == "Foxlet_Wag")
    meta = json.loads((path.parent / wag["extracted"]).read_text())
    assert meta["curves_exported"] and meta["decoder"]["curves"]["dense"] == 7
    report = convert_creature(c, path.parent, include_proposed=True)
    assert report["clip_errors"] == [] and report["summary"]["clips"] == 1
    clip = report["model"]["clips"][0]
    assert clip["name"] == "Foxlet_Wag" and clip["unknown_paths"] == [] and clip["max_head_error_m"] < 1e-3
    comp = report["completeness"]
    assert comp["geometry"]["status"] == "complete" and comp["animations"]["status"] == "complete"
    assert comp["materials"]["status"] == "complete" and comp["ready_for_next_step"]

    # Without its material the same model is reported as untextured, not as a finished port.
    for a in c["assets"]:
        if a["role"] == "material":
            a["association"] = "rejected"
    report = convert_creature(c, path.parent, include_proposed=True)
    comp = report["completeness"]
    assert comp["materials"]["status"] == "missing" and not comp["ready_for_next_step"]
    assert comp["materials"]["untextured_parts"][0]["part"] == "Foxlet_Body"
