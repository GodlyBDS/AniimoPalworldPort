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


def _catalog_for(tmp_path, builder, rules=None, strategy="targeted"):
    (tmp_path / "x.bundle").write_bytes(ub.bundle(builder.build(), "CAB-x"))
    return build_catalog(build_inventory(scan(tmp_path, ScanConfig()), tmp_path, strategy=strategy), rules)


def test_species_and_variants(pipeline):
    cat = pipeline["catalog"]
    assert [s["key"] for s in cat["species"]] == ["foxlet", "mossbun"]
    fox = _species(cat, "foxlet")
    assert {p["name"]: p["variant"] for p in fox["prefabs"]} == {
        "Foxlet": "normal", "Foxlet_Shiny": "normal+shiny_01", "Foxlet_Prismana": "prismana"}
    shiny = next(p for p in fox["prefabs"] if p["name"] == "Foxlet_Shiny")
    assert (shiny["region"], shiny["form"], shiny["appearance"]) == ("base", "normal", "shiny_01")
    assert fox["grouping_evidence"] == ["shared-mesh"]
    assert fox["suggested_id"] == "aniimo_foxlet" and fox["display_name"] == "Foxlet"
    assert fox["warnings"] == ["1 normal Shiny prefab(s) numbered by name order (inferred); "
                               "verify appearance identities against the source data"]


def test_asset_roles_and_variant_attribution(pipeline):
    fox = _species(pipeline["catalog"], "foxlet")
    by_name = {a["name"]: a for a in fox["assets"]}
    assert by_name["Foxlet_Body"]["role"] == "mesh"
    assert (by_name["Foxlet_Body"]["form"], by_name["Foxlet_Body"]["appearance"]) == ("shared", "any")
    assert by_name["FoxletAvatar"]["role"] == "skeleton"
    assert (by_name["Foxlet_Shiny_Albedo"]["form"], by_name["Foxlet_Shiny_Albedo"]["appearance"]) == ("normal", "shiny_01")
    assert by_name["Foxlet_Shiny_Albedo"]["slot"] == "_MainTex"
    assert (by_name["Foxlet_Prismana_Mat"]["form"], by_name["Foxlet_Prismana_Mat"]["appearance"]) == ("prismana", "standard")
    assert (by_name["Foxlet_Normal"]["form"], by_name["Foxlet_Normal"]["appearance"]) == ("normal", "standard")
    assert all(a["region"] == "any" for a in fox["assets"])
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
    assert {p["variant"] for p in sp[0]["prefabs"]} == {"normal", "normal+shiny_01"}


def test_static_props_ignored_by_default(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_static_prop(b, "Rock")
    assert _catalog_for(tmp_path, b)["species"] == []
    (tmp_path / "x.bundle").unlink()
    # Static props have no skinned mesh, so only a full inventory reads them.
    cat = _catalog_for(tmp_path, b, CatalogRules(include_static_meshes=True), strategy="full")
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


def test_prismana_shiny_detected_as_both(tmp_path):
    b = ub.SerializedBuilder()
    ids = ub.add_creature(b, "Foxlet", bones=ub.DEFAULT_BONES, clips=["Idle"], variants={"prismana": (1, 2, 3)})
    ub.add_creature(b, "Foxlet_Prismana_Shiny_B", bones=ub.DEFAULT_BONES, clips=[], mesh=ids["normal"].mesh)
    ub.add_creature(b, "Foxlet_Prismana_Shiny_A", bones=ub.DEFAULT_BONES, clips=[], mesh=ids["normal"].mesh)
    sp = _catalog_for(tmp_path, b)["species"][0]
    got = {p["name"]: (p["form"], p["appearance"]) for p in sp["prefabs"]}
    assert got == {"Foxlet": ("normal", "standard"), "Foxlet_Prismana": ("prismana", "standard"),
                   "Foxlet_Prismana_Shiny_A": ("prismana", "shiny_01"),
                   "Foxlet_Prismana_Shiny_B": ("prismana", "shiny_02")}
    assert any("numbered by name order" in w for w in sp["warnings"])


def test_diagnostics_explain_missing_meshes(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_creature(b, "Glimmer", bones=ub.DEFAULT_BONES, clips=["Idle"])
    (tmp_path / "x.bundle").write_bytes(ub.bundle(b.build(), "CAB-x"))
    doc = build_inventory(scan(tmp_path, ScanConfig()), tmp_path)
    d = build_catalog(doc)["summary"]["diagnostics"]
    assert d["renderers"] == d["renderers_with_mesh"] == d["renderers_in_hierarchy"] == 1
    doc["objects"] = [o for o in doc["objects"] if o["class"] != "Mesh"]  # e.g. mesh bundle not read
    cat = build_catalog(doc)
    d = cat["summary"]["diagnostics"]
    assert cat["species"] == [] and d["renderers_with_mesh"] == 0
    assert d["missing_mesh_files"] == [{"cab": "cab-x", "count": 1, "container": "x.bundle"}]


def test_clip_fitting_many_skeletons_is_ambiguous_not_attached_everywhere(tmp_path):
    b = ub.SerializedBuilder()
    for name in ("Alpha", "Bravo", "Charlie", "Delta"):  # four species on one shared rig
        ub.add_creature(b, name, bones=ub.DEFAULT_BONES, clips=[], with_controller=False)
    ub.add_clip(b, "Shared_Run", ub.DEFAULT_BONES)
    cat = _catalog_for(tmp_path, b)
    assert len(cat["species"]) == 4 and cat["summary"]["ambiguous_clip_count"] == 1
    assert all(s["animations"] == [] for s in cat["species"])
    (tmp_path / "x.bundle").unlink()
    cat = _catalog_for(tmp_path, b, CatalogRules(max_binding_match_species=4))
    assert cat["summary"]["ambiguous_clip_count"] == 0
    assert all([a["name"] for a in s["animations"]] == ["Shared_Run"] for s in cat["species"])


def _parmon_game(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_creature(b, "P_Parmon_10025", bones=ub.DEFAULT_BONES, clips=["Idle"])
    ub.add_creature(b, "Eff_Parmon_Skill_Ex", bones=ub.DEFAULT_BONES, clips=[], with_controller=False)
    (tmp_path / "a.uab").write_bytes(ub.bundle(b.build(), "CAB-a"))
    b2 = ub.SerializedBuilder()
    ub.add_creature(b2, "P_Parmon_10503", bones=ub.DEFAULT_BONES, clips=["Idle"])
    (tmp_path / "aexp_tcpm_10503_sleepybear_tga_0_64955d0aa83ce0a5305247b81e407573.uab").write_bytes(
        ub.bundle(b2.build(), "CAB-b"))
    return build_inventory(scan(tmp_path, ScanConfig()), tmp_path)


def test_id_numbers_keep_species_apart_and_codename_is_guessed(tmp_path):
    cat = build_catalog(_parmon_game(tmp_path))
    by_key = {s["key"]: s for s in cat["species"]}
    assert set(by_key) == {"p_parmon_10025", "p_parmon_10503", "eff_parmon_skill_ex"}
    code = by_key["p_parmon_10503"]["codename"]
    assert code["value"] == "sleepybear" and code["provenance"] == "inferred" and code["id"] == "10503"
    assert by_key["p_parmon_10025"]["codename"] is None and by_key["eff_parmon_skill_ex"]["codename"] is None
    groups = {g["prefix"]: g for g in cat["summary"]["name_groups"]}
    assert groups["p_parmon"]["prefabs"] == 2 and groups["p_parmon"]["with_id"] == 2
    assert groups["eff_parmon"]["with_id"] == 0 and all(g["selected"] for g in groups.values())


def test_name_prefix_filter_keeps_only_chosen_groups(tmp_path):
    doc = _parmon_game(tmp_path)
    cat = build_catalog(doc, CatalogRules(include_name_prefixes=["p_parmon"]))
    assert sorted(s["key"] for s in cat["species"]) == ["p_parmon_10025", "p_parmon_10503"]
    assert cat["summary"]["prefabs_filtered_out"] == 1
    assert {g["prefix"]: g["selected"] for g in cat["summary"]["name_groups"]} == {"p_parmon": True,
                                                                                  "eff_parmon": False}
    cat = build_catalog(doc, CatalogRules(exclude_name_prefixes=["p_parmon_10025"]))
    assert sorted(s["key"] for s in cat["species"]) == ["eff_parmon_skill_ex", "p_parmon_10503"]


def test_rules_file_prefixes_are_normalised(tmp_path):
    f = tmp_path / "rules.toml"
    f.write_text('[catalog]\ninclude_name_prefixes = ["P_Parmon", "NG-P-Parmon"]\n')
    assert load_rules(f).include_name_prefixes == ["p_parmon", "ng_p_parmon"]


def test_update_rules_file_round_trips(tmp_path):
    from aniimo_pal.catalog.rules import update_rules_file

    f = tmp_path / "r.toml"
    f.write_text('[catalog]\nmin_binding_ratio = 0.9\n[catalog.variant_keywords]\nshiny = ["glow"]\n')
    rules = update_rules_file(f, include_name_prefixes=["P_Parmon", "m_monster"])
    assert rules.include_name_prefixes == ["p_parmon", "m_monster"]
    assert rules.min_binding_ratio == 0.9 and rules.variant_keywords["shiny"] == ["glow"]
    assert load_rules(f) == rules
    assert update_rules_file(tmp_path / "new.toml", include_name_prefixes=[]).include_name_prefixes == []
