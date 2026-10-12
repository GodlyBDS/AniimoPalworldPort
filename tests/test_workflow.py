import json

import pytest

pytest.importorskip("UnityPy")

from PIL import Image  # noqa: E402  (UnityPy dependency)

from aniimo_pal.cli import EXIT_CHECK_FAILED, EXIT_INCOMPLETE, EXIT_OK, EXIT_USAGE, main  # noqa: E402
from aniimo_pal.creature.model import CreatureError, creature_path, load_creature, save_creature, validate_creature  # noqa: E402
from aniimo_pal.creature.workflow import creature_from_catalog, pending, set_associations  # noqa: E402
from aniimo_pal.errors import ToolkitError, UnsafeOutputError  # noqa: E402
from aniimo_pal.extract import extract_creature  # noqa: E402


def _foxlet(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "foxlet")
    path = save_creature(c, creature_path(pipeline["workspace"], c["id"]))
    return c, path


def test_from_catalog_autoconfirms_verified(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "foxlet")
    assert validate_creature(c) == []
    assert c["id"] == "aniimo_foxlet" and c["status"] == "mapped"
    assert all(a["association"] == "confirmed" for a in c["assets"])
    v = c["variants"]
    assert v["forms"]["prismana"]["enabled"] and v["shiny"]["enabled"]
    assert [a["id"] for a in v["shiny"]["appearances"]] == ["shiny"]  # only what the data contains
    assert v["rarity"]["wild"] == {"prismana_probability": 0.0002, "shiny_probability": 0.002}
    assert v["rarity"]["egg"] == {"prismana_probability": 0.0, "shiny_probability": 0.002}
    assert {a["role"] for a in c["assets"]} == {"mesh", "material", "texture", "skeleton", "animation", "other"}
    assert c["skeleton"]["bones"] and c["combat"]["ultimate"]["target"] == "partner_skill"


def test_propose_all(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "foxlet", auto_confirm_verified=False)
    assert c["status"] == "draft" and len(pending(c)) == len(c["assets"])


def test_inferred_stay_proposed_until_reviewed(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "mossbun")
    todo = pending(c)
    assert c["status"] == "draft"
    assert {c["assets"][i]["name"] for i in todo} == {"Mossbun_Idle", "Mossbun_Hop", "Mossbun_Ult"}
    assert c["variants"]["shiny"]["enabled"] is False  # no shiny prefab found -> not invented
    assert c["variants"]["forms"]["prismana"]["enabled"] is False
    set_associations(c, confirm=todo[:2], reject=todo[2:])
    assert c["status"] == "mapped" and validate_creature(c) == []


def test_associate_errors(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "mossbun")
    with pytest.raises(CreatureError, match="out of range"):
        set_associations(c, confirm=[99])
    with pytest.raises(CreatureError, match="at once"):
        set_associations(c, confirm=[5], reject=[5])
    with pytest.raises(CreatureError, match="form must be"):
        set_associations(c, forms={0: "golden"})
    with pytest.raises(CreatureError, match="appearance must be"):
        set_associations(c, appearances={0: "shiny_09"})


def test_extract_outputs(pipeline):
    c, path = _foxlet(pipeline)
    report = extract_creature(c, path.parent, pipeline["root"])
    s = report["summary"]
    assert (s["ok"], s["errors"], s["skipped"]) == (15, 0, 1)  # controller has no UnityPy export
    # The synthetic clips bind paths but carry no curves: exported as metadata, counted as not decoded.
    assert s["animations_decoded"] == 0 and s["animations_not_decoded"] == 3 and s["by_role"]["material"] == 3
    assert c["status"] == "extracted"
    out = path.parent / "extract" / "unitypy"
    img = Image.open(out / "textures" / "normal" / "shiny" / "Foxlet_Shiny_Albedo.png")
    assert img.size == (4, 4) and img.getpixel((0, 0))[:3] == (240, 220, 60)
    assert "v " in (out / "meshes" / "Foxlet_Body.obj").read_text()
    mat = json.loads((out / "materials" / "prismana" / "Foxlet_Prismana_Mat.json").read_text())
    assert mat["textures"]["_MainTex"]["file"] == "textures/prismana/Foxlet_Prismana_Albedo.png"
    clip = json.loads((out / "animations" / "Foxlet_Attack.json").read_text())
    assert [e["function"] for e in clip["events"]] == ["OnHit", "PlayVfx"] and clip["provenance"] == "verified"
    skeleton = json.loads((out / "skeleton.json").read_text())
    assert any(b["path"] == "Root/Hips/Tail" for b in skeleton["bones"])
    tex = next(a for a in c["assets"] if a["name"] == "Foxlet_Albedo")
    assert tex["extracted"] == "extract/unitypy/textures/normal/Foxlet_Albedo.png"


def test_extract_refuses_changed_source(pipeline):
    c, path = _foxlet(pipeline)
    src = pipeline["root"] / "StreamingAssets" / "foxlet.bundle"
    src.write_bytes(src.read_bytes() + b"\x00")
    report = extract_creature(c, path.parent, pipeline["root"])
    assert report["summary"]["ok"] == 1  # only skeleton.json (from the project file)
    assert all("re-scan" in r["error"] for r in report["outputs"] if r["status"] == "error")
    assert c["status"] == "mapped"


def test_extract_refuses_draft_and_output_in_game_root(pipeline):
    c = creature_from_catalog(pipeline["catalog"], "mossbun")
    with pytest.raises(ToolkitError, match="draft"):
        extract_creature(c, pipeline["workspace"] / "x", pipeline["root"])
    c2 = creature_from_catalog(pipeline["catalog"], "foxlet")
    with pytest.raises(UnsafeOutputError):
        extract_creature(c2, pipeline["root"] / "inside", pipeline["root"])


def test_cli_end_to_end(creature_game, tmp_path, capsys):
    root, _ = creature_game
    ws = tmp_path / "ws"
    m, objs, cat = ws / "manifest.json", ws / "objects.json", ws / "catalog.json"
    assert main(["scan", str(root), "-o", str(m), "-q"]) == EXIT_OK
    assert main(["inventory", str(m), str(root), "-o", str(objs)]) == EXIT_OK
    side = json.loads((ws / "objects.summary.json").read_text())
    assert side["summary"] == json.loads(objs.read_text())["summary"] and "objects" not in side
    assert main(["inventory", str(m), str(root), "-o", str(root / "o.json")]) == EXIT_USAGE
    assert main(["catalog", "build", str(objs), "-o", str(cat)]) == EXIT_OK
    capsys.readouterr()
    assert main(["catalog", "list", str(cat), "--json"]) == EXIT_OK
    assert [s["key"] for s in json.loads(capsys.readouterr().out)] == ["foxlet", "mossbun"]
    assert main(["catalog", "show", str(cat), "mossbun"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["key"] == "mossbun"

    assert main(["creature", "init", str(ws), "--from-catalog", str(cat), "--species", "mossbun"]) == EXIT_OK
    cfile = ws / "creatures" / "aniimo_mossbun" / "creature.json"
    assert main(["creature", "extract", str(cfile), "--root", str(root)]) == EXIT_USAGE  # still draft
    capsys.readouterr()
    assert main(["creature", "review", str(cfile), "--pending"]) == EXIT_OK
    assert "Mossbun_Ult" in capsys.readouterr().out
    assert main(["creature", "associate", str(cfile), "--confirm-all"]) == EXIT_OK
    assert load_creature(cfile)["status"] == "mapped"
    # Exit 3: everything exported, but the (curve-less) synthetic clips could not be decoded.
    assert main(["creature", "extract", str(cfile), "--root", str(root)]) == EXIT_INCOMPLETE
    assert (cfile.parent / "extract" / "unitypy" / "report.json").is_file()
    assert main(["creature", "validate", str(cfile)]) == EXIT_OK
    assert main(["creature", "init", str(ws), "--from-catalog", str(cat), "--species", "nope"]) == EXIT_USAGE
    assert main(["verify", str(m), str(root)]) == EXIT_OK  # game files untouched


def test_cli_extract_reports_errors(pipeline, capsys):
    c, path = _foxlet(pipeline)
    (pipeline["root"] / "StreamingAssets" / "foxlet.bundle").unlink()
    assert main(["creature", "extract", str(path), "--root", str(pipeline["root"])]) == EXIT_CHECK_FAILED


def test_assetripper_name_keys_strip_only_build_suffixes():
    from aniimo_pal.extract import _name_keys

    assert _name_keys("M_Parmon_10184_Body_-1771000540") == ["m_parmon_10184_body_1771000540", "m_parmon_10184_body"]
    assert _name_keys("MI_Parmon_10184_Body_01a_9632378D") == ["mi_parmon_10184_body_01a_9632378d",
                                                              "mi_parmon_10184_body_01a"]
    assert _name_keys("NG_P_Parmon_1018403") == ["ng_p_parmon_1018403"]  # 7-digit form IDs stay
    assert _name_keys("Ani_Parmon_10184_Idle") == ["ani_parmon_10184_idle"]
    assert _name_keys("Foxlet_Body_12345678") == ["foxlet_body_12345678"]  # 8 digits without hex letters stay


def test_unassigned_sample_prefers_files_of_this_creature():
    from aniimo_pal.extract import _related_unassigned

    files = ["x/Assets/Shader/Hidden_Blit.json", "x/Assets/res/10184/M_Parmon_10184.glb", "x/Assets/a/Body.png",
             "x/Assets/res/10184/M_Parmon_10184.glb.meta", "x/done.png"]
    assert _related_unassigned(files, {"x/done.png"}, ["M_Parmon_10184_Body_-1771000540"]) == [
        "x/Assets/res/10184/M_Parmon_10184.glb"]
    assert _related_unassigned(files, set(), ["Nothing_Here"])[:1] == ["x/Assets/Shader/Hidden_Blit.json"]


def test_remove_tree_handles_read_only_files_and_deep_paths(tmp_path):
    import os
    import stat

    from aniimo_pal.extract import remove_tree

    deep = tmp_path / "out"
    leaf = deep
    for i in range(30):  # > 260 characters in total
        leaf = leaf / f"Res_Export_Character_{i:02d}"
    leaf.mkdir(parents=True)
    f = leaf / "x.asset"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    assert len(str(f)) > 260
    remove_tree(deep)
    assert not deep.exists()
