import pytest

pytest.importorskip("UnityPy")

import unity_builder as ub  # noqa: E402
from conftest import MOSSBUN_BONES  # noqa: E402

from aniimo_pal.catalog import CatalogRules, build_catalog, load_rules  # noqa: E402
from aniimo_pal.catalog.builder import path_hash  # noqa: E402
from aniimo_pal.catalog.rules import match_keyword, tokens  # noqa: E402
from aniimo_pal.config import ScanConfig  # noqa: E402
from aniimo_pal.errors import ConfigError  # noqa: E402
from aniimo_pal.inventory import build_inventory  # noqa: E402
from aniimo_pal.scanner import scan  # noqa: E402


def _species(catalog, key):
    return next(s for s in catalog["species"] if s["key"] == key)


def _catalog_for(tmp_path, builder, rules=None):
    (tmp_path / "x.bundle").write_bytes(ub.bundle(builder.build(), "CAB-x"))
    return build_catalog(build_inventory(scan(tmp_path, ScanConfig()), tmp_path), rules)


def test_species_and_variants(pipeline):
    cat = pipeline["catalog"]
    assert [s["key"] for s in cat["species"]] == ["foxlet", "mossbun"]
    fox = _species(cat, "foxlet")
    assert {p["name"]: p["variant"] for p in fox["prefabs"]} == {
        "Foxlet": "normal", "Foxlet_Shiny": "shiny", "Foxlet_Prismana": "prismana"}
    assert fox["grouping_evidence"] == ["shared-mesh"]
    assert fox["suggested_id"] == "aniimo_foxlet" and fox["display_name"] == "Foxlet"
    assert fox["warnings"] == []


def test_asset_roles_and_variant_attribution(pipeline):
    fox = _species(pipeline["catalog"], "foxlet")
    by_name = {a["name"]: a for a in fox["assets"]}
    assert by_name["Foxlet_Body"]["role"] == "mesh" and by_name["Foxlet_Body"]["variant"] == "shared"
    assert by_name["FoxletAvatar"]["role"] == "skeleton"
    assert by_name["Foxlet_Shiny_Albedo"]["variant"] == "shiny"
    assert by_name["Foxlet_Shiny_Albedo"]["slot"] == "_MainTex"
    assert by_name["Foxlet_Prismana_Mat"]["variant"] == "prismana"
    assert by_name["Foxlet_Normal"]["variant"] == "normal"
    assert all(a["provenance"] == "verified" for a in fox["assets"])
    assert all(a["sha256"] and a["container"] == "StreamingAssets/foxlet.bundle" for a in fox["assets"])


def test_skeleton(pipeline):
    sk = _species(pipeline["catalog"], "foxlet")["skeleton"]
    bones = {b["path"]: b for b in sk["bones"]}
    assert set(ub.DEFAULT_BONES) <= set(bones)
    assert bones["Root/Hips/Spine/Head"]["parent"] == "Root/Hips/Spine"
    assert bones["Root/Hips"]["skinned"] is True
    assert bones["Foxlet_Body"]["skinned"] is False
    assert bones["Root/Hips"]["local"]["position"]["y"] == pytest.approx(0.2)


def test_controller_clips_are_verified(pipeline):
    anims = {a["name"]: a for a in _species(pipeline["catalog"], "foxlet")["animations"]}
    assert set(anims) == {"Foxlet_Idle", "Foxlet_Walk", "Foxlet_Attack"}
    attack = anims["Foxlet_Attack"]
    assert attack["linked_via"] == "controller" and attack["provenance"] == "verified"
    assert attack["binding_overlap"] == [5, 5]
    assert attack["role_guess"] == "attack" and attack["role_provenance"] == "inferred:name-keyword"
    assert attack["event_count"] == 2


def test_orphan_clips_linked_by_binding_hashes_are_inferred(pipeline):
    moss = _species(pipeline["catalog"], "mossbun")
    anims = {a["name"]: a for a in moss["animations"]}
    assert set(anims) == {"Mossbun_Idle", "Mossbun_Hop", "Mossbun_Ult"}
    assert all(a["linked_via"] == "binding-match" and a["provenance"] == "inferred" for a in anims.values())
    assert anims["Mossbun_Ult"]["role_guess"] == "ultimate"
    # Foxlet's clips must not be attached to Mossbun (different skeleton).
    assert not any(n.startswith("Foxlet") for n in anims)
    assert pipeline["catalog"]["summary"]["unassigned_clip_count"] == 0


def test_unrelated_clip_not_matched(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_creature(b, "Foxlet", bones=ub.DEFAULT_BONES, clips=["Idle"])
    ub.add_clip(b, "Bird_Fly", ["Wing", "Wing/Tip", "Beak", "Tail2"])
    cat = _catalog_for(tmp_path, b)
    assert cat["summary"]["unassigned_clip_count"] == 1


def test_binding_threshold_rules(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_creature(b, "Mossbun", bones=MOSSBUN_BONES, clips=[], with_controller=False)
    ub.add_clip(b, "Mossbun_Partial", MOSSBUN_BONES[:3] + ["Other/A", "Other/B"])  # 3 of 5 match = 0.6
    assert _catalog_for(tmp_path, b)["species"][0]["animations"] == []
    (tmp_path / "x.bundle").unlink()
    loose = CatalogRules(min_binding_ratio=0.5)
    assert [a["name"] for a in _catalog_for(tmp_path, b, loose)["species"][0]["animations"]] == ["Mossbun_Partial"]


def test_extra_normal_prefab_is_unclassified(tmp_path):
    b = ub.SerializedBuilder()
    ids = ub.add_creature(b, "Foxlet", bones=ub.DEFAULT_BONES, clips=["Idle"])
    ub.add_creature(b, "FoxletBattle", bones=ub.DEFAULT_BONES, clips=[], mesh=ids["normal"].mesh)
    sp = _catalog_for(tmp_path, b)["species"][0]
    assert {p["name"]: p["variant"] for p in sp["prefabs"]} == {"Foxlet": "normal", "FoxletBattle": "unclassified"}
    assert any("unclassified" in w for w in sp["warnings"])


def test_duplicated_meshes_grouped_by_skeleton_and_name(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_creature(b, "Foxlet", bones=ub.DEFAULT_BONES, clips=["Idle"])
    ub.add_creature(b, "Foxlet_Shiny", bones=ub.DEFAULT_BONES, clips=[])  # own mesh copy
    sp = _catalog_for(tmp_path, b)["species"]
    assert len(sp) == 1
    assert sp[0]["grouping_evidence"] == ["same-skeleton+name-stem"]
    assert {p["variant"] for p in sp[0]["prefabs"]} == {"normal", "shiny"}


def test_static_props_ignored_by_default(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_static_prop(b, "Rock")
    assert _catalog_for(tmp_path, b)["species"] == []
    (tmp_path / "x.bundle").unlink()
    cat = _catalog_for(tmp_path, b, CatalogRules(include_static_meshes=True))
    assert [s["key"] for s in cat["species"]] == ["rock"]


def test_tokens_and_keywords():
    assert tokens("FoxletShiny_LOD0") == ["foxlet", "shiny", "lod0"]
    assert match_keyword("Mossbun_Ult_01", {"ultimate": ["ult"]}) == ("ultimate", "ult")
    assert match_keyword("Result", {"ultimate": ["ult"]}) is None  # token match, not substring
    assert path_hash("Root/Hips") == ub.crc("Root/Hips")


def test_rules_file_matches_defaults():
    from pathlib import Path

    assert load_rules(Path(__file__).resolve().parents[1] / "config" / "catalog_rules.toml") == CatalogRules()


@pytest.mark.parametrize("text,msg", [
    ("[catalog]\nbogus = 1\n", "unknown"),
    ("[catalog]\nmin_binding_ratio = 2\n", "(0, 1]"),
    ("[catalog]\nmin_binding_matches = 0\n", "positive"),
    ("[catalog.variant_keywords]\nshadow = [\"dark\"]\n", "only define"),
    ("[catalog]\nstop_tokens = [1]\n", "list of strings"),
])
def test_rules_validation(tmp_path, text, msg):
    p = tmp_path / "r.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(").replace("]", r"\]")):
        load_rules(p)
