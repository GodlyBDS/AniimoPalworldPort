import json

import pytest

from aniimo_pal.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, main
from aniimo_pal.creature import load_creature, migrate_creature, new_creature, save_creature, validate_creature
from aniimo_pal.creature.model import CreatureError, creature_path
from aniimo_pal.creature.variants import shiny_appearances

SHA = "a" * 64


def _rare(shiny=0.01, prismana=0.001, egg_shiny=0.01, egg_prismana=0.0, n_apps=12):
    c = new_creature("aniimo_test", "Test")
    v = c["variants"]
    v["forms"]["prismana"]["enabled"] = prismana > 0
    v["shiny"] = {"enabled": True, "appearances": shiny_appearances(n_apps)}
    v["rarity"]["wild"] = {"prismana_probability": prismana, "shiny_probability": shiny}
    v["rarity"]["egg"] = {"prismana_probability": egg_prismana, "shiny_probability": egg_shiny}
    return c


def test_new_creature_is_valid():
    c = new_creature("aniimo_test", "Test")
    assert validate_creature(c) == []
    assert c["schema_version"] == "0.3.0" and c["evolution"] is None
    assert c["variants"]["regions"] == {"forms": [], "inheritance": None}


def test_bad_id_rejected():
    with pytest.raises(CreatureError):
        new_creature("Bad Id", "x")


def _asset(**kw):
    return dict({"ref": {"path": "a", "sha256": SHA}, "role": "texture", "association": "proposed"}, **kw)


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c["variants"]["forms"]["normal"].update(enabled=False), "normal must be enabled"),
    (lambda c: c["variants"]["forms"].pop("prismana"), "exactly"),
    (lambda c: c.update(status="done"), "status"),
    (lambda c: c["integration"].update(mode="merge"), "integration.mode"),
    (lambda c: c.update(status="configured"), "replace_target"),
    (lambda c: c["assets"].append({"ref": {"path": "a.bundle"}, "role": "mesh", "association": "confirmed"}),
     "must pin ref.sha256"),
    (lambda c: c["assets"].append(_asset(role="hat")), "role"),
    (lambda c: c["assets"].append(_asset(form="prismana")), "disabled Prismana"),
    (lambda c: c["assets"].append(_asset(appearance="shiny_01")), "Shiny"),
    (lambda c: c["assets"].append(_asset(form="golden")), "form must be"),
    (lambda c: c["assets"].append(_asset(region="snow")), "regional form id"),
    (lambda c: c["assets"].extend([_asset(role="mesh")] * 2), "duplicates"),
    (lambda c: c.update(evolution={"stage": 0, "provenance": "user"}), "evolution.stage"),
    (lambda c: c.update(evolution={"stage": 1, "next": ["Bad"], "provenance": "user"}), "evolution.next"),
])
def test_validation_errors(mutate, needle):
    c = new_creature("aniimo_test", "Test")
    mutate(c)
    errors = validate_creature(c)
    assert any(needle in e for e in errors), errors


def test_evolution_link_valid():
    c = new_creature("aniimo_helmwhelp", "Helmwhelp")
    c["evolution"] = {"line": "jawling", "stage": 2, "previous": ["aniimo_jawling"], "next": ["aniimo_infergon"],
                      "provenance": "inferred"}
    assert validate_creature(c) == []


def test_save_load_roundtrip_and_refuse_invalid(tmp_path):
    c = _rare()
    path = save_creature(c, creature_path(tmp_path, c["id"]))
    assert load_creature(path) == c
    c["variants"]["forms"]["normal"]["enabled"] = False
    with pytest.raises(CreatureError):
        save_creature(c, path)
    assert load_creature(path)["variants"]["forms"]["normal"]["enabled"] is True


def test_cli_creature_flow(tmp_path, capsys):
    ws = tmp_path / "ws"
    assert main(["creature", "init", str(ws), "--id", "aniimo_test", "--name", "Test"]) == EXIT_OK
    path = creature_path(ws, "aniimo_test")
    assert main(["creature", "init", str(ws), "--id", "aniimo_test", "--name", "Test"]) == EXIT_USAGE
    assert main(["creature", "validate", str(path)]) == EXIT_OK

    path.write_text(json.dumps(_rare(shiny=0.01, prismana=0.001)))
    capsys.readouterr()
    assert main(["creature", "roll", str(path), "--n", "20000", "--seed", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Prismana Shiny  exact 0.00001000" in out and "1 in 100,000" in out
    assert "[wild]" in out and "[egg]" in out

    data = json.loads(path.read_text())
    data["variants"]["forms"]["normal"]["enabled"] = False
    path.write_text(json.dumps(data))
    assert main(["creature", "validate", str(path)]) == EXIT_CHECK_FAILED


# --- reserved combat section ---------------------------------------------------

def _with_clips():
    c = new_creature("aniimo_test", "Test")
    for i, name in enumerate(["Ult_Start", "Ult_Loop", "Ult_End", "Bite"]):
        c["assets"].append({"ref": {"path": "c.bundle", "sha256": SHA, "object": f"cab:{i}"}, "role": "animation",
                            "name": name, "association": "confirmed"})
    c["assets"].append({"ref": {"path": "c.bundle", "sha256": SHA, "object": "cab:fx"}, "role": "other",
                        "name": "UltBurst", "association": "confirmed"})
    return c


def test_default_combat_section_maps_ultimate_to_partner_skill():
    c = new_creature("aniimo_test", "Test")
    assert c["combat"] == {"profiles": ["ai_companion"], "abilities": [],
                           "ultimate": {"ability": None, "target": "partner_skill"}}
    assert validate_creature(c) == []


def test_multi_phase_ultimate_is_valid():
    c = _with_clips()
    c["combat"]["abilities"] = [
        {"id": "ult", "kind": "ultimate", "provenance": "inferred", "name": "Thorn Storm",
         "phases": [{"clip": "cab:0", "label": "windup"}, {"clip": "cab:1", "label": "active"},
                    {"clip": "cab:2", "label": "recovery"}],
         "effects": {"vfx": ["cab:fx"]},
         "parameters": {"cooldown_s": {"value": 30, "provenance": "user"}},
         "palworld_mapping": {"target": "partner_skill", "status": "proposed"}},
        {"id": "bite", "kind": "basic", "provenance": "inferred", "phases": [{"clip": "cab:3"}]},
    ]
    c["combat"]["ultimate"]["ability"] = "ult"
    assert validate_creature(c) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "laser", "provenance": "user"}), "kind"),
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "basic", "provenance": "guess"}), "provenance"),
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "basic", "provenance": "user",
                                        "phases": [{"clip": "cab:missing"}]}), "animation assets"),
    (lambda cb: cb["abilities"].extend([{"id": "x", "kind": "basic", "provenance": "user"}] * 2), "duplicated"),
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "basic", "provenance": "user",
                                        "parameters": {"damage": 50}}), "provenance"),
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "basic", "provenance": "user",
                                        "effects": {"vfx": ["cab:unknown"]}}), "asset object ids"),
    (lambda cb: cb["abilities"].append({"id": "x", "kind": "basic", "provenance": "user",
                                        "palworld_mapping": {"target": "summon"}}), "palworld_mapping.target"),
    (lambda cb: cb["ultimate"].update(ability="nope"), "reference an ability"),
    (lambda cb: (cb["abilities"].append({"id": "b", "kind": "basic", "provenance": "user"}),
                 cb["ultimate"].update(ability="b")), "kind 'ultimate'"),
    (lambda cb: cb["ultimate"].update(target="random_attack"), "ultimate.target"),
    (lambda cb: cb.update(profiles=["flying_mount"]), "profiles"),
])
def test_combat_validation_errors(mutate, needle):
    c = _with_clips()
    mutate(c["combat"])
    errors = validate_creature(c)
    assert any(needle in e for e in errors), errors


def test_variant_combat_overrides_need_verified_or_user():
    c = _rare()
    c["variants"]["shiny"]["combat_overrides"] = {"provenance": "inferred"}
    assert any("combat_overrides" in e for e in validate_creature(c))
    c["variants"]["shiny"]["combat_overrides"] = {"provenance": "user"}
    c["variants"]["forms"]["prismana"]["combat_overrides"] = {"provenance": "verified"}
    assert validate_creature(c) == []
    c["variants"]["forms"]["normal"]["combat_overrides"] = {"provenance": "user"}
    assert any("base configuration" in e for e in validate_creature(c))


def _v02_document():
    return {
        "schema": "aniimo-pal/creature", "schema_version": "0.2.0", "id": "aniimo_old", "display_name": "Old",
        "status": "draft", "source": {}, "integration": {"mode": "replace", "replace_target": None},
        "variants": {"normal": {"enabled": True},
                     "shiny": {"enabled": True, "spawn_probability": 0.002,
                               "profile_overrides": {"provenance": "user"}},
                     "prismana": {"enabled": True, "spawn_probability": 0.0005}},
        "assets": [_asset(variant="shiny"), _asset(variant="prismana", role="material"),
                   _asset(variant="shared", role="mesh")],
        "prefabs": [{"object": "c:1", "name": "Old_Shiny", "variant": "shiny"}],
    }


def test_migration_from_v02():
    old = _v02_document()
    new = migrate_creature(old)
    assert old["schema_version"] == "0.2.0"  # input untouched
    assert new["schema_version"] == "0.3.0"
    v = new["variants"]
    assert v["forms"]["prismana"]["enabled"] and v["shiny"]["enabled"]
    assert [a["id"] for a in v["shiny"]["appearances"]] == ["shiny_01"]
    assert v["rarity"]["wild"] == {"prismana_probability": 0.0005, "shiny_probability": 0.002}
    assert v["rarity"]["egg"] == {"prismana_probability": 0.0, "shiny_probability": 0.002}
    assert v["shiny"]["profile_overrides"] == {"provenance": "user"}
    assert [(a["form"], a["appearance"]) for a in new["assets"]] == [
        ("normal", "shiny_01"), ("prismana", "standard"), ("shared", "any")]
    assert new["prefabs"][0]["appearance"] == "shiny_01"
    assert "migrated-from-0.2.0" in new["history"][-1]["action"]
    assert validate_creature(old) == []  # old documents validate via in-memory migration


def test_schema_v010_documents_still_validate(tmp_path):
    c = {"schema": "aniimo-pal/creature", "schema_version": "0.1.0", "id": "aniimo_test", "display_name": "T",
         "status": "draft", "source": {}, "assets": [], "integration": {"mode": "replace"},
         "variants": {"normal": {"enabled": True}, "shiny": {"enabled": False, "spawn_probability": 0.0},
                      "prismana": {"enabled": False, "spawn_probability": 0.0}}}
    assert validate_creature(c) == []
    p = tmp_path / "c.json"
    p.write_text(json.dumps(c))
    assert load_creature(p)["schema_version"] == "0.3.0"
    c["schema_version"] = "1.0.0"
    assert any("schema_version" in e for e in validate_creature(c))
