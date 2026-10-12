"""Reference rarity semantics: independent form/Shiny rolls, wild vs egg, regional forms."""

import random

import pytest

from aniimo_pal.creature.model import new_creature, validate_creature
from aniimo_pal.creature.variants import (
    one_in,
    outcome_probabilities,
    rarity_warnings,
    region_for_biome,
    roll_individual,
    select_individual,
    shiny_appearances,
    validate_variants,
)


def _variants(shiny=0.01, prismana=0.001, egg_shiny=0.01, egg_prismana=0.0, n_apps=12):
    c = new_creature("aniimo_test", "Test")
    v = c["variants"]
    v["forms"]["prismana"]["enabled"] = True
    v["shiny"] = {"enabled": True, "appearances": shiny_appearances(n_apps)}
    v["rarity"]["wild"] = {"prismana_probability": prismana, "shiny_probability": shiny}
    v["rarity"]["egg"] = {"prismana_probability": egg_prismana, "shiny_probability": egg_shiny}
    return c


def test_brief_example_prismana_shiny_is_product():
    v = _variants(shiny=0.01, prismana=0.001)["variants"]
    p = outcome_probabilities(v, "wild")
    assert p["prismana_shiny"] == pytest.approx(0.00001)
    assert one_in(p["prismana_shiny"]) == "1 in 100,000"
    assert p["normal"] + p["normal_shiny"] + p["prismana"] + p["prismana_shiny"] == pytest.approx(1.0)
    # Prismana Shiny is rarer than Shiny and than Prismana; Prismana is rarer than Shiny.
    assert p["prismana_shiny"] < p["prismana"] < p["normal_shiny"]
    assert sum(p["per_appearance"].values()) == pytest.approx(0.01)
    assert all(v == pytest.approx(0.01 / 12) for v in p["per_appearance"].values())


def test_rolls_are_independent_and_yield_one_individual():
    v = _variants(shiny=0.25, prismana=0.1)["variants"]
    assert roll_individual(v, draws=(0.05, 0.10, 0.0)) == {"region": "base", "form": "prismana", "appearance": "shiny_01"}
    assert roll_individual(v, draws=(0.05, 0.90, 0.0)) == {"region": "base", "form": "prismana", "appearance": "standard"}
    assert roll_individual(v, draws=(0.50, 0.10, 0.999)) == {"region": "base", "form": "normal", "appearance": "shiny_12"}
    assert roll_individual(v, draws=(0.50, 0.90, 0.5)) == {"region": "base", "form": "normal", "appearance": "standard"}
    with pytest.raises(ValueError):
        roll_individual(v, draws=(1.0, 0.0, 0.0))


def test_appearances_equal_by_default_and_weights_respected():
    v = _variants()["variants"]
    picks = {roll_individual(v, draws=(0.9, 0.0, (i + 0.5) / 12))["appearance"] for i in range(12)}
    assert picks == {f"shiny_{i:02d}" for i in range(1, 13)}
    v["shiny"]["appearances"][0]["weight"] = 11.0  # 11 of 22 weight
    assert roll_individual(v, draws=(0.9, 0.0, 0.49))["appearance"] == "shiny_01"
    assert any("unequal" in w for w in rarity_warnings(v))


def test_simulated_frequencies_match_exact_probabilities():
    v = _variants(shiny=0.05, prismana=0.02)["variants"]
    exact = outcome_probabilities(v, "wild")
    rng = random.Random(7)
    n = 200_000
    counts = {"normal": 0, "normal_shiny": 0, "prismana": 0, "prismana_shiny": 0}
    for _ in range(n):
        ind = roll_individual(v, "wild", rng=rng)
        counts[ind["form"] + ("_shiny" if ind["appearance"] != "standard" else "")] += 1
    assert sum(counts.values()) == n  # exactly one individual per event
    for key, c in counts.items():
        p = exact[key]
        assert abs(c / n - p) < 5 * (p * (1 - p) / n) ** 0.5 + 1e-9, (key, c)


def test_egg_context_is_independent():
    c = _variants(shiny=0.01, prismana=0.001, egg_shiny=0.004, egg_prismana=0.0)
    v = c["variants"]
    egg = outcome_probabilities(v, "egg")
    assert egg["any_shiny"] == 0.004 and egg["any_prismana"] == 0.0 and egg["prismana_shiny"] == 0.0
    assert validate_creature(c) == []


@pytest.mark.parametrize("change,needle", [
    (lambda v: v["rarity"]["wild"].update(prismana_probability=0.02), "must be rarer than Shiny"),
    (lambda v: v["rarity"]["egg"].update(prismana_probability=0.005), "must not exceed the wild"),
    (lambda v: v["rarity"]["wild"].update(shiny_probability=1.0), "guarantee"),
    (lambda v: v["rarity"].update(prismana_shiny_probability=0.5), "computed"),
    (lambda v: v["rarity"]["wild"].update(shiny_probability=-0.1), "[0, 1]"),
    (lambda v: v["shiny"].update(appearances=[]), "no appearances"),
    (lambda v: v["shiny"]["appearances"].append({"id": "shiny_01"}), "duplicate"),
    (lambda v: v["shiny"]["appearances"][0].update(weight=0), "positive"),
    (lambda v: (v["forms"]["prismana"].update(enabled=False)), "must be 0 while that variant is disabled"),
    (lambda v: v["rarity"].update(pity=True), "pity"),
    (lambda v: v["regions"]["forms"].append({"id": "snow", "biomes": ["snow"]}), "provenance"),
    (lambda v: v["regions"]["forms"].extend([{"id": "snow", "biomes": ["tundra"], "provenance": "user"},
                                             {"id": "frost", "biomes": ["tundra"], "provenance": "user"}]),
     "already maps"),
    (lambda v: v["regions"]["forms"].append({"id": "base", "provenance": "user"}), "reserved"),
    (lambda v: v["regions"]["forms"].append({"id": "snow", "provenance": "user", "appearances": ["shiny_99"]}),
     "configured Shiny appearance ids"),
    (lambda v: v["regions"]["forms"].append({"id": "snow", "provenance": "user", "appearances": []}),
     "allows no Shiny appearance"),
    (lambda v: v["regions"].update(inheritance={"mode": "random"}), "inheritance"),
])
def test_rarity_validation_errors(change, needle):
    v = _variants()["variants"]
    change(v)
    errors = validate_variants(v)
    assert any(needle in e for e in errors), errors


def test_policy_warnings():
    v = _variants(shiny=0.05, prismana=0.04, egg_shiny=0.01, egg_prismana=0.001, n_apps=3)["variants"]
    w = " | ".join(rarity_warnings(v))
    assert "genuinely rare" in w and "not significantly rarer" in w
    assert "primarily a wild encounter" in w and "12 expected" in w
    assert rarity_warnings(_variants()["variants"]) == []
    v["rarity"].update(allow_guaranteed=True, pity={"enabled": True})
    w = " | ".join(rarity_warnings(v))
    assert "allow_guaranteed" in w and "pity" in w


def test_guaranteed_needs_explicit_opt_in():
    v = _variants()["variants"]
    v["rarity"]["wild"]["shiny_probability"] = 1.0
    v["rarity"]["allow_guaranteed"] = True
    assert validate_variants(v) == []


def _regional():
    c = _variants()
    c["variants"]["regions"]["forms"] = [
        {"id": "snow", "name": "Snow", "biomes": ["frost_island", "snowfield"], "provenance": "verified",
         "appearances": ["shiny_01", "shiny_07"]},
        {"id": "desert", "name": "Desert", "biomes": ["dune"], "provenance": "user", "allow_prismana": False},
    ]
    c["evolution"] = {"line": "test", "stage": 2, "provenance": "user"}
    return c


def test_region_comes_from_biome_not_roll():
    v = _regional()["variants"]
    assert region_for_biome(v, "snowfield") == "snow"
    assert region_for_biome(v, "dune") == "desert"
    assert region_for_biome(v, "grassland") == "base"
    assert region_for_biome(v, None) == "base"


def test_full_identity_snow_prismana_shiny_7():
    c = _regional()
    assert validate_creature(c) == []
    ind = select_individual(c, biome="snowfield", draws=(0.0, 0.0, 0.99))
    assert ind == {"species": "aniimo_test", "stage": 2, "region": "snow", "form": "prismana",
                   "appearance": "shiny_07"}


def test_regional_restrictions_change_exact_odds():
    v = _regional()["variants"]
    snow = outcome_probabilities(v, "wild", "snow")
    assert set(snow["per_appearance"]) == {"shiny_01", "shiny_07"}
    assert snow["per_appearance"]["shiny_07"] == pytest.approx(0.01 / 2)
    desert = outcome_probabilities(v, "wild", "desert")
    assert desert["any_prismana"] == 0.0 and desert["prismana_shiny"] == 0.0
    assert {roll_individual(v, region="desert", draws=(0.0, 0.5, 0.0))["form"]} == {"normal"}


def test_skins_are_recorded_and_offered_only_by_explicit_policy(tmp_path, capsys):
    import json

    from aniimo_pal.cli import EXIT_OK, main
    from aniimo_pal.creature.model import creature_path, save_creature
    from aniimo_pal.creature.variants import enabled_skins, rarity_warnings

    c = new_creature("aniimo_irisalis", "Irisalis")
    assert c["variants"]["skins"] == {"policy": None, "entries": []}
    path = save_creature(c, creature_path(tmp_path, c["id"]))
    for sid, name in (("starwing", "Starwing"), ("lunar_gala", "Lunar Gala")):
        assert main(["creature", "skins", str(path), "--add", sid, name, "--source", "gacha",
                     "--mesh", "M_Parmon_10213_Wing02"]) == EXIT_OK
    data = json.loads(path.read_text())
    skins = data["variants"]["skins"]
    assert [s["provenance"] for s in skins["entries"]] == ["user", "user"] and skins["policy"] is None
    assert enabled_skins(data["variants"]) == []  # undecided: nothing is exported
    assert any("decide which ones the Palworld mod offers" in w for w in rarity_warnings(data["variants"]))
    assert main(["creature", "skins", str(path), "--policy", "selected", "--enable", "starwing"]) == EXIT_OK
    data = json.loads(path.read_text())
    assert [s["id"] for s in enabled_skins(data["variants"])] == ["starwing"]
    assert "starwing" in capsys.readouterr().out
    # base_only with an enabled skin is contradictory and refused
    assert main(["creature", "skins", str(path), "--policy", "base_only"]) == 2
    assert main(["creature", "skins", str(path), "--disable", "starwing"]) == EXIT_OK
    assert main(["creature", "skins", str(path), "--policy", "base_only"]) == EXIT_OK
    assert main(["creature", "skins", str(path), "--policy", "all"]) == EXIT_OK
    assert len(enabled_skins(json.loads(path.read_text())["variants"])) == 2


def test_skin_validation_errors():
    c = new_creature("aniimo_test", "Test")
    c["variants"]["skins"] = {"policy": "some", "entries": [{"id": "Bad Id", "name": ""},
                                                            {"id": "ok", "name": "Ok", "provenance": "guess"}]}
    errors = " | ".join(validate_creature(c))
    assert "policy must be null" in errors and "lowercase identifier" in errors and "provenance" in errors
    del c["variants"]["skins"]  # projects made before skins existed stay valid
    assert validate_creature(c) == []
