import json
import random

import pytest

from aniimo_pal.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, main
from aniimo_pal.creature import load_creature, new_creature, roll_variant, save_creature, validate_creature
from aniimo_pal.creature.model import CreatureError, creature_path
from aniimo_pal.creature.variants import variant_probabilities

SHA = "a" * 64


def _rare(shiny=0.01, prismana=0.001):
    c = new_creature("aniimo_test", "Test")
    c["variants"]["shiny"] = {"enabled": True, "spawn_probability": shiny}
    c["variants"]["prismana"] = {"enabled": True, "spawn_probability": prismana}
    return c


def test_new_creature_is_valid():
    assert validate_creature(new_creature("aniimo_test", "Test")) == []


def test_bad_id_rejected():
    with pytest.raises(CreatureError):
        new_creature("Bad Id", "x")


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c["variants"]["normal"].update(enabled=False), "normal must be enabled"),
    (lambda c: c["variants"]["shiny"].update(enabled=True, spawn_probability=0), "spawn_probability is 0"),
    (lambda c: c["variants"]["shiny"].update(spawn_probability=0.1), "disabled but has a non-zero"),
    (lambda c: c["variants"]["shiny"].update(enabled=True, spawn_probability=1.5), "in [0, 1]"),
    (lambda c: c["variants"].pop("prismana"), "exactly"),
    (lambda c: c.update(status="done"), "status"),
    (lambda c: c["integration"].update(mode="merge"), "integration.mode"),
    (lambda c: c.update(status="configured"), "replace_target"),
    (lambda c: c["assets"].append({"ref": {"path": "a.bundle"}, "role": "mesh", "association": "confirmed"}),
     "must pin ref.sha256"),
    (lambda c: c["assets"].append({"ref": {"path": "a", "sha256": SHA}, "role": "hat", "association": "proposed"}),
     "role"),
    (lambda c: c["assets"].append({"ref": {"path": "a", "sha256": SHA}, "role": "texture",
                                   "variant": "shiny", "association": "proposed"}), "disabled variant"),
    (lambda c: c["assets"].extend([{"ref": {"path": "a", "sha256": SHA}, "role": "mesh", "association": "proposed"}] * 2),
     "duplicates"),
])
def test_validation_errors(mutate, needle):
    c = new_creature("aniimo_test", "Test")
    mutate(c)
    errors = validate_creature(c)
    assert any(needle in e for e in errors), errors


def test_combined_rare_probability_cannot_exceed_one():
    errors = validate_creature(_rare(shiny=0.7, prismana=0.6))
    assert any("exceeds 1" in e for e in errors)


def test_roll_partitions_single_draw():
    c = _rare(shiny=0.25, prismana=0.1)["variants"]
    assert roll_variant(c, u=0.0) == "prismana"
    assert roll_variant(c, u=0.0999) == "prismana"
    assert roll_variant(c, u=0.1) == "shiny"
    assert roll_variant(c, u=0.3499) == "shiny"
    assert roll_variant(c, u=0.35) == "normal"
    assert roll_variant(c, u=0.999999) == "normal"
    with pytest.raises(ValueError):
        roll_variant(c, u=1.0)


def test_disabled_variants_never_roll():
    c = new_creature("aniimo_test", "Test")["variants"]
    assert {roll_variant(c, u=u / 100) for u in range(100)} == {"normal"}


def test_roll_frequencies_match_configuration():
    variants = _rare(shiny=0.02, prismana=0.005)["variants"]
    rng = random.Random(42)
    n = 200_000
    counts = {"normal": 0, "shiny": 0, "prismana": 0}
    for _ in range(n):
        counts[roll_variant(variants, rng=rng)] += 1  # exactly one outcome per event
    assert sum(counts.values()) == n
    probs = variant_probabilities(variants)
    for name, p in probs.items():
        sigma = (p * (1 - p) / n) ** 0.5
        assert abs(counts[name] / n - p) < 5 * sigma + 1e-9, (name, counts[name])


def test_save_load_roundtrip_and_refuse_invalid(tmp_path):
    c = _rare()
    path = save_creature(c, creature_path(tmp_path, c["id"]))
    assert load_creature(path) == c
    c["variants"]["normal"]["enabled"] = False
    with pytest.raises(CreatureError):
        save_creature(c, path)
    assert load_creature(path)["variants"]["normal"]["enabled"] is True


def test_cli_creature_flow(tmp_path, capsys):
    ws = tmp_path / "ws"
    assert main(["creature", "init", str(ws), "--id", "aniimo_test", "--name", "Test"]) == EXIT_OK
    path = creature_path(ws, "aniimo_test")
    assert main(["creature", "init", str(ws), "--id", "aniimo_test", "--name", "Test"]) == EXIT_USAGE
    assert main(["creature", "validate", str(path)]) == EXIT_OK

    data = json.loads(path.read_text())
    data["variants"]["shiny"] = {"enabled": True, "spawn_probability": 0.01}
    path.write_text(json.dumps(data))
    capsys.readouterr()
    assert main(["creature", "roll", str(path), "--n", "10000", "--seed", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "shiny" in out and "configured 0.010000" in out

    data["variants"]["normal"]["enabled"] = False
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
    assert validate_creature(c) == []
    c["variants"]["normal"]["combat_overrides"] = {"provenance": "user"}
    assert any("base configuration" in e for e in validate_creature(c))


def test_schema_v010_documents_still_validate():
    c = new_creature("aniimo_test", "Test")
    c["schema_version"] = "0.1.0"
    del c["combat"], c["skeleton"]
    assert validate_creature(c) == []
    c["schema_version"] = "1.0.0"
    assert any("schema_version" in e for e in validate_creature(c))
