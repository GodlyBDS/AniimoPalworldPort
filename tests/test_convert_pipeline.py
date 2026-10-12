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
    assert m["assembly"]["bind_pose_source"] == "mesh-bind-poses" and m["assembly"]["warnings"] == []
    assert m["blender"]["basis"]["method"] == "expected-assetripper-basis"
    assert m["clips"][0]["bones_driven"] == 5 and m["clips"][0]["max_head_error_m"] < 1e-6
    assert c["status"] == "converted" and mesh["converted"] == m["fbx"]
    assert json.loads((cdir / "convert/report.json").read_text())["creature"] == "aniimo_foxlet"

    # 3D preview: every variant side by side, every animation on the timeline.
    from aniimo_pal.convert.preview import build_preview

    preview = build_preview(c, cdir)
    assert [v["label"] for v in preview["variants"]] == ["Normal", "Shiny 1"]
    assert all(v["texture"] and Path(v["texture"]).is_file() for v in preview["variants"])
    assert [t["action"] for t in preview["timeline"]] == ["Foxlet_Wag"]
    assert (cdir / "preview" / "preview.blend").is_file()
    assert preview["render"] is None and "Blender executable" in preview["render_error"]  # bpy module: no still


def ub_fixture():
    from pathlib import Path

    return Path(__file__).parent / "fixtures" / "Foxlet_Wag.anim"


def test_convert_requires_assetripper_mesh(skinned_game, tmp_path, capsys):
    cat = build_catalog(build_inventory(scan(skinned_game, ScanConfig()), skinned_game))
    c = creature_from_catalog(cat, "foxlet")
    path = save_creature(c, creature_path(tmp_path / "ws", c["id"]))
    from aniimo_pal.convert.pipeline import convert_creature

    report = convert_creature(c, path.parent)
    assert report["meshes"][0]["status"] == "error" and "export-assetripper" in report["meshes"][0]["error"]
    assert c["status"] == "mapped"


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
    m = report["meshes"][0]
    assert m["blender"]["meshes"][0]["skinned"] and m["blender"]["meshes"][0]["unweighted_vertices"] == 0
    assert m["clips"][0]["max_head_error_m"] < 1e-6
    assert main(["verify", str(ws / "m.json"), str(root)]) == EXIT_OK  # game files untouched
