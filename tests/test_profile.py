import copy

import pytest

from aniimo_pal.cli import EXIT_CHECK_FAILED, EXIT_OK, main
from aniimo_pal.creature.model import new_creature, validate_creature
from aniimo_pal.creature.profile import effective_profile, profile_warnings
from aniimo_pal.errors import ConfigError
from aniimo_pal.systems import bundled_system, load_system, parse_system

SRC = bundled_system("aniimo")
DST = bundled_system("palworld")


def _creature():
    c = new_creature("aniimo_test", "Test")
    c["variants"]["shiny"] = {"enabled": True, "spawn_probability": 0.002}
    c["variants"]["prismana"] = {"enabled": True, "spawn_probability": 0.0005}
    c["source_profile"] = {
        "system": SRC.ref(),
        "stats": {"hp": {"value": 80, "provenance": "verified", "source": "cab-x:12.m_Hp"},
                  "atk": {"value": 101, "provenance": "verified"}},
        "elements": [{"id": "earth", "provenance": "verified"}, {"id": "wind", "provenance": "verified"}],
        "work": [{"id": "earth_job", "level": 2, "provenance": "verified"}],
        "tiers": {"job_level": {"value": 2, "meaning": None, "provenance": "verified"}},
        "role": {"value": "DPS", "provenance": "inferred"},
    }
    c["translation"] = {
        "target_system": DST.ref(),
        "profile": {"id": "faithful", "rules_version": None},
        "stats": {"hp": {"value": 75, "auto_value": 75, "from": ["hp"], "method": "user", "overridden": True}},
        "elements": [{"id": "ground", "from": ["earth"], "method": "user"}],
        "unmapped_elements": ["wind"],
        "work": [{"id": "mining", "level": 2, "from": ["earth_job"], "method": "user", "overridden": False}],
    }
    return c


def test_bundled_systems_load_and_are_unverified():
    assert SRC.status == DST.status == "unverified"
    assert len(SRC.elements) == 9 and len(DST.elements) == 9
    assert "wind" in SRC.elements and "wind" not in DST.elements
    assert DST.work_categories[:3] == ("kindling", "watering", "planting") and len(DST.work_categories) == 12
    assert SRC.canonical_element("Holy") == "light" and SRC.canonical_element("ROCK") == "earth"
    assert SRC.sources and DST.sources


def test_valid_profile():
    assert validate_creature(_creature(), SRC, DST) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c["source_profile"]["stats"].update(speed={"value": 1, "provenance": "verified"}), "unknown aniimo stat"),
    (lambda c: c["source_profile"]["stats"]["hp"].update(provenance="wiki"), "provenance"),
    (lambda c: c["source_profile"]["elements"].append({"id": "holy", "provenance": "user"}), "canonical id 'light'"),
    (lambda c: c["source_profile"]["elements"].append({"id": "fire", "provenance": "user"}), "at most 2"),
    (lambda c: c["source_profile"]["work"][0].update(level=7), "outside aniimo range"),
    (lambda c: c["translation"]["elements"].append({"id": "wind", "from": ["wind"], "method": "user"}),
     "types are never invented"),
    (lambda c: c["translation"].update(unmapped_elements=[]), "must be mapped or listed"),
    (lambda c: c["translation"]["elements"][0].update(**{"from": ["fire"]}), "not in source_profile"),
    (lambda c: c["translation"]["work"][0].update(id="dancing"), "not a palworld work category"),
    (lambda c: c["translation"]["work"][0].update(level=11), "outside palworld range"),
    (lambda c: c["translation"]["work"][0].pop("method"), "method is required"),
    (lambda c: c["translation"]["stats"].update(mana={"value": 3, "method": "user"}), "unknown palworld stat"),
    (lambda c: c["translation"]["stats"]["hp"].update(value="high"), "must be a number"),
    (lambda c: c["translation"].update(profile={"id": "op"}), "profile.id"),
    (lambda c: c["translation"].update(target_system={"id": "pokemon", "version": "1.0.0"}), "validated against"),
])
def test_profile_errors(mutate, needle):
    c = _creature()
    mutate(c)
    errors = validate_creature(c, SRC, DST)
    assert any(needle in e for e in errors), errors


def test_variants_inherit_and_override():
    c = _creature()
    assert effective_profile(c, "shiny") == effective_profile(c, "normal")
    c["variants"]["prismana"]["profile_overrides"] = {
        "provenance": "verified",
        "source_profile": {"elements": [{"id": "earth", "provenance": "verified"}, {"id": "light", "provenance": "verified"}]},
        "translation": {"elements": [{"id": "ground", "from": ["earth"], "method": "user"}],
                        "unmapped_elements": ["light"]},
    }
    assert validate_creature(c, SRC, DST) == []
    eff = effective_profile(c, "prismana")
    assert [e["id"] for e in eff["source_profile"]["elements"]] == ["earth", "light"]
    assert eff["source_profile"]["stats"] == c["source_profile"]["stats"]  # stats inherited unchanged
    assert [e["id"] for e in effective_profile(c, "normal")["source_profile"]["elements"]] == ["earth", "wind"]


@pytest.mark.parametrize("override,needle", [
    ({"provenance": "inferred"}, "needs provenance"),
    ({"provenance": "user", "spawn_probability": 0.5}, "unexpected keys"),
    ({"provenance": "user", "translation": {"stats": {"hp": {"value": 1, "method": "user", "spawn_probability": 1}}}},
     "do not belong"),
    ({"provenance": "user", "translation": {"work": [{"id": "mining", "level": 99, "method": "user"}]}},
     "outside palworld range"),
])
def test_variant_override_errors(override, needle):
    c = _creature()
    c["variants"]["shiny"]["profile_overrides"] = override
    errors = validate_creature(c, SRC, DST)
    assert any(needle in e for e in errors), errors


def test_normal_cannot_override():
    c = _creature()
    c["variants"]["normal"]["profile_overrides"] = {"provenance": "user"}
    assert any("base profile" in e for e in validate_creature(c, SRC, DST))


def test_warnings():
    c = _creature()
    c["variants"]["prismana"]["profile_overrides"] = {
        "provenance": "user", "translation": {"work": [{"id": "mining", "level": DST.work_level_max,
                                                       "from": ["earth_job"], "method": "user"}]}}
    warnings = profile_warnings(c, SRC, DST)
    assert any("unverified" in w for w in warnings)
    assert any("job_level" in w and "meaning" in w for w in warnings)
    assert any("prismana" in w and "maximum" in w for w in warnings)
    c2 = _creature()
    for s in c2["source_profile"]["stats"].values():
        s["provenance"] = "inferred"
    assert any("not backed by any verified" in w for w in profile_warnings(c2, SRC, DST))


def test_profile_is_independent_of_rarity_and_combat():
    c = _creature()
    before = copy.deepcopy(c["source_profile"]), copy.deepcopy(c["translation"])
    c["variants"]["shiny"]["spawn_probability"] = 0.01
    c["combat"]["profiles"] = ["partner_skill"]
    assert validate_creature(c, SRC, DST) == []
    assert (c["source_profile"], c["translation"]) == before


def test_new_creature_has_empty_profile_sections():
    c = new_creature("aniimo_test", "Test")
    assert c["source_profile"] is None and c["translation"] is None
    assert validate_creature(c) == []


@pytest.mark.parametrize("text,msg", [
    ('[system]\nid = "X"\nversion = "1.0.0"\n', "lowercase"),
    ('[system]\nid = "x"\nversion = "1"\n', "1.2.3"),
    ('[system]\nid = "x"\nversion = "1.0.0"\nstatus = "maybe"\n', "status"),
    ('[system]\nid = "x"\nversion = "1.0.0"\n[elements]\nfire = { name = "Fire", aliases = ["fire"] }\n', "ambiguous"),
    ('[system]\nid = "x"\nversion = "1.0.0"\n[work]\nlevel_min = 5\nlevel_max = 1\n', "level_min"),
    ('[system]\nid = "x"\nversion = "1.0.0"\n[work]\ncategories = ["a", "a"]\n', "duplicates"),
])
def test_system_definition_validation(tmp_path, text, msg):
    p = tmp_path / "s.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=msg):
        load_system(p)


def test_verified_custom_system_used_by_cli(tmp_path, capsys):
    custom = tmp_path / "palworld-verified.toml"
    custom.write_text('[system]\nid = "palworld"\nversion = "1.0.0"\nstatus = "verified"\nmax_elements = 2\n'
                      '[elements]\nground = { name = "Ground" }\n[stats]\nhp = { name = "HP" }\n'
                      '[work]\ncategories = ["mining"]\nlevel_min = 1\nlevel_max = 4\n')
    import json
    path = tmp_path / "c.json"
    c = _creature()
    path.write_text(json.dumps(c))
    assert main(["creature", "validate", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "warning: source system definition aniimo 0.1.0 is unverified" in out
    assert main(["creature", "validate", str(path), "--target-system", str(custom)]) == EXIT_OK
    c["translation"]["work"][0]["level"] = 5
    path.write_text(json.dumps(c))
    assert main(["creature", "validate", str(path), "--target-system", str(custom)]) == EXIT_CHECK_FAILED
    assert main(["systems", "aniimo", "--json"]) == EXIT_OK
    assert parse_system({"system": {"id": "x", "version": "0.0.1"}}).work_categories == ()
