"""Variant model and reference rarity semantics (independent form + shiny rolls).

This module is the *specification and test oracle* for the in-game spawn and
hatch logic, which must ultimately run inside Palworld (Blueprint/UE4SS);
Python never runs there.

Identity of an individual = (species, evolution stage, region, form, appearance),
five independent properties, never separate mutually exclusive definitions:

* species / evolution stage: the creature project (one project per stage;
  ``evolution`` links stages).
* region: ``base`` or a regional form id (e.g. ``snow``), chosen from the
  spawn biome, NOT rolled.
* form: ``normal`` or ``prismana`` (a rare form; may change element/skills).
* appearance: ``standard`` or one of the species' Shiny appearances
  (Aniimo has 12 by the project brief; equal weights by default).

Spawn selection order (one individual per event):
1. Palworld picks the species (its own spawn rules).
2. ``region_for_biome`` picks the regional form from the biome.
3. Prismana roll.  4. Independent Shiny roll.  5. Weighted appearance pick.
6. The caller resolves assets for the identity and spawns exactly one creature.
A regional form can restrict which forms/appearances exist for it
(``allow_prismana``, ``appearances``); those restrictions come from the source
data and are never filled with invented assets.

Rules (project requirements):

* One spawn/hatch event produces exactly ONE individual. Rare outcomes change
  that individual; they never add copies.
* Two independent rolls per individual: a **form roll** (Prismana with
  probability ``p_prismana``) and a **shiny roll** (Shiny with probability
  ``p_shiny``). If Shiny, a third draw picks the appearance by weight.
* Hence P(Prismana Shiny) = p_prismana × p_shiny. It is always computed, never
  configured, so it is automatically rarer than either outcome alone.
* Probabilities are configured separately for ``wild`` encounters and ``egg``
  hatches. Prismana is primarily a wild encounter.
* No pity system and no guaranteed rare outcome unless the user explicitly
  opts in (``allow_guaranteed`` / ``pity``); the reference roller implements
  neither.
* The resulting (form, appearance) must persist through capture, storage,
  save/load and evolution. That is an in-game requirement (architecture §6).
"""

from __future__ import annotations

import math
import random
from typing import Any

FORMS = ("normal", "prismana")
BASE_REGION = "base"
CONTEXTS = ("wild", "egg")
STANDARD = "standard"
DEFAULT_POLICY = {
    # Prismana must be at least this many times rarer than Shiny (warning below it).
    "min_shiny_to_prismana_ratio": 2.0,
    # Shiny above this per-roll chance is not "genuinely rare" (warning).
    "max_shiny_probability": 0.01,
    # Number of Shiny appearances expected per species (project brief: 12).
    "expected_shiny_appearances": 12,
}


def new_variants() -> dict[str, Any]:
    return {
        # Regional forms: verified source data only; biomes are target-game biome ids.
        "regions": {"forms": [], "inheritance": None},
        "forms": {"normal": {"enabled": True}, "prismana": {"enabled": False}},
        "shiny": {"enabled": False, "appearances": []},
        "rarity": {
            "wild": {"prismana_probability": 0.0, "shiny_probability": 0.0},
            "egg": {"prismana_probability": 0.0, "shiny_probability": 0.0},
            "allow_guaranteed": False,
            "pity": None,
            "policy": dict(DEFAULT_POLICY),
        },
    }


def shiny_appearances(n: int) -> list[dict[str, Any]]:
    return [{"id": f"shiny_{i:02d}", "name": f"Shiny {i}", "weight": 1.0} for i in range(1, n + 1)]


def variant_key(form: str, appearance: str) -> str:
    """Display key, e.g. 'normal', 'prismana', 'normal+shiny_03', 'prismana+shiny_03'."""
    return form if appearance == STANDARD else f"{form}+{appearance}"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def regional_forms(variants: dict[str, Any]) -> list[dict[str, Any]]:
    return list(((variants.get("regions") or {}).get("forms")) or [])


def region_spec(variants: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region == BASE_REGION:
        return None
    for r in regional_forms(variants):
        if r.get("id") == region:
            return r
    raise ValueError(f"unknown regional form {region!r}")


def region_for_biome(variants: dict[str, Any], biome: str | None) -> str:
    """Step 2 of spawn selection: the regional form for a biome (deterministic, not rolled)."""
    if biome:
        for r in regional_forms(variants):
            if biome in (r.get("biomes") or []):
                return r["id"]
    return BASE_REGION


def rates(variants: dict[str, Any], context: str, region: str = BASE_REGION) -> tuple[float, float]:
    """(p_prismana, p_shiny) for a context and region; disabled features count as 0."""
    if context not in CONTEXTS:
        raise ValueError(f"context must be one of {CONTEXTS}")
    ctx = variants["rarity"][context]
    spec = region_spec(variants, region)
    prismana_ok = variants["forms"]["prismana"].get("enabled") and (spec is None or spec.get("allow_prismana", True))
    pp = float(ctx.get("prismana_probability", 0.0)) if prismana_ok else 0.0
    ps = float(ctx.get("shiny_probability", 0.0)) if variants["shiny"].get("enabled") and \
        appearance_weights(variants, region) else 0.0
    return pp, ps


def appearance_weights(variants: dict[str, Any], region: str = BASE_REGION) -> dict[str, float]:
    apps = variants["shiny"].get("appearances") or []
    weights = {a["id"]: float(a.get("weight", 1.0)) for a in apps}
    spec = region_spec(variants, region)
    if spec is not None and spec.get("appearances") is not None:
        weights = {k: w for k, w in weights.items() if k in spec["appearances"]}
    return weights


def outcome_probabilities(variants: dict[str, Any], context: str, region: str = BASE_REGION) -> dict[str, Any]:
    """Exact probabilities of each outcome class for one individual (they sum to 1)."""
    pp, ps = rates(variants, context, region)
    weights = appearance_weights(variants, region)
    total_w = sum(weights.values())
    per_app = {k: ps * w / total_w for k, w in weights.items()} if total_w else {}
    return {
        "normal": (1 - pp) * (1 - ps),
        "normal_shiny": (1 - pp) * ps,
        "prismana": pp * (1 - ps),
        "prismana_shiny": pp * ps,
        "any_shiny": ps,
        "any_prismana": pp,
        "per_appearance": per_app,  # P(this exact shiny appearance), either form
    }


def one_in(p: float) -> str:
    return "never" if p <= 0 else f"1 in {math.floor(1 / p + 0.5):,}"


def roll_individual(variants: dict[str, Any], context: str = "wild", *, region: str = BASE_REGION,
                    rng: random.Random | None = None,
                    draws: tuple[float, float, float] | None = None) -> dict[str, str]:
    """Roll ONE individual's form and appearance (steps 3-5) in a given region.

    ``draws`` (u_form, u_shiny, u_pick), each in [0, 1), makes the roll
    deterministic for tests.
    """
    pp, ps = rates(variants, context, region)
    if draws is None:
        r = rng or random
        draws = (r.random(), r.random(), r.random())
    if any(not 0.0 <= u < 1.0 for u in draws):
        raise ValueError("draws must be in [0, 1)")
    u_form, u_shiny, u_pick = draws
    form = "prismana" if u_form < pp else "normal"
    appearance = STANDARD
    if u_shiny < ps:
        weights = appearance_weights(variants, region)
        total = sum(weights.values())
        edge = 0.0
        for app_id, w in weights.items():
            edge += w / total
            if u_pick < edge:
                appearance = app_id
                break
        else:  # floating-point edge: last appearance
            appearance = next(reversed(weights))
    return {"region": region, "form": form, "appearance": appearance}


def select_individual(creature: dict[str, Any], *, biome: str | None = None, context: str = "wild",
                      rng: random.Random | None = None,
                      draws: tuple[float, float, float] | None = None) -> dict[str, Any]:
    """Steps 2-5 for one spawn/hatch of this species: full identity of exactly one individual."""
    variants = creature["variants"]
    region = region_for_biome(variants, biome)
    identity = roll_individual(variants, context, region=region, rng=rng, draws=draws)
    evo = creature.get("evolution") or {}
    return {"species": creature["id"], "stage": evo.get("stage"), **identity}


def validate_variants(variants: Any) -> list[str]:
    """Structural and policy-hard errors for the ``variants`` block."""
    if not isinstance(variants, dict):
        return ["variants must be an object"]
    errors: list[str] = []
    forms = variants.get("forms")
    if not isinstance(forms, dict) or set(forms) != set(FORMS):
        return [f"variants.forms must contain exactly {FORMS}"]
    if forms["normal"].get("enabled") is not True:
        errors.append("variants.forms.normal must be enabled (it is the base species)")
    if not isinstance(forms["prismana"].get("enabled"), bool):
        errors.append("variants.forms.prismana.enabled must be a boolean")
    shiny = variants.get("shiny")
    if not isinstance(shiny, dict) or not isinstance(shiny.get("enabled"), bool):
        return errors + ["variants.shiny must be an object with a boolean 'enabled'"]
    apps = shiny.get("appearances", [])
    if not isinstance(apps, list):
        return errors + ["variants.shiny.appearances must be a list"]
    ids = set()
    for i, a in enumerate(apps):
        where = f"variants.shiny.appearances[{i}]"
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not a["id"].startswith("shiny"):
            errors.append(f"{where}.id must be a string starting with 'shiny'")
            continue
        if a["id"] in ids:
            errors.append(f"{where}: duplicate appearance id {a['id']!r}")
        ids.add(a["id"])
        w = a.get("weight", 1.0)
        if not _num(w) or w <= 0:
            errors.append(f"{where}.weight must be a positive number")
    if shiny["enabled"] and not apps:
        errors.append("variants.shiny is enabled but has no appearances")

    errors.extend(_validate_regions(variants.get("regions"), ids))

    rarity = variants.get("rarity")
    if not isinstance(rarity, dict):
        return errors + ["variants.rarity must be an object"]
    if "prismana_shiny_probability" in rarity or any(
            isinstance(rarity.get(c), dict) and "prismana_shiny_probability" in rarity[c] for c in CONTEXTS):
        errors.append("Prismana Shiny probability is computed (p_prismana × p_shiny) and cannot be configured")
    allow_guaranteed = rarity.get("allow_guaranteed", False)
    if not isinstance(allow_guaranteed, bool):
        errors.append("variants.rarity.allow_guaranteed must be a boolean")
    pity = rarity.get("pity")
    if pity is not None and (not isinstance(pity, dict) or not isinstance(pity.get("enabled"), bool)):
        errors.append("variants.rarity.pity must be null or an object with a boolean 'enabled'")
    for ctx in CONTEXTS:
        block = rarity.get(ctx)
        if not isinstance(block, dict):
            errors.append(f"variants.rarity.{ctx} must be an object")
            continue
        for key, enabled in (("prismana_probability", forms["prismana"].get("enabled")),
                             ("shiny_probability", shiny["enabled"])):
            p = block.get(key, 0.0)
            where = f"variants.rarity.{ctx}.{key}"
            if not _num(p) or not 0.0 <= p <= 1.0:
                errors.append(f"{where} must be a number in [0, 1]")
                continue
            if p >= 1.0 and not allow_guaranteed:
                errors.append(f"{where} = 1 would guarantee a rare outcome; set allow_guaranteed to opt in")
            if not enabled and p != 0:
                errors.append(f"{where} must be 0 while that variant is disabled")
    if errors:
        return errors

    for r in regional_forms(variants):
        if r.get("appearances") is not None and shiny["enabled"] and not appearance_weights(variants, r["id"]):
            errors.append(f"variants.regions: regional form {r['id']!r} allows no Shiny appearance; "
                          "set appearances to null to inherit, or list at least one")
    wild_pp, wild_ps = rates(variants, "wild")
    egg_pp, _ = rates(variants, "egg")
    if forms["prismana"]["enabled"] and wild_pp == 0 and egg_pp == 0:
        errors.append("variants.forms.prismana is enabled but its probability is 0 in every context")
    if shiny["enabled"] and wild_ps == 0 and rates(variants, "egg")[1] == 0:
        errors.append("variants.shiny is enabled but its probability is 0 in every context")
    for ctx in CONTEXTS:
        pp, ps = rates(variants, ctx)
        if pp > 0 and ps > 0 and pp >= ps:
            errors.append(f"variants.rarity.{ctx}: Prismana ({pp}) must be rarer than Shiny ({ps})")
    if egg_pp > wild_pp:
        errors.append("variants.rarity.egg.prismana_probability must not exceed the wild probability "
                      "(Prismana is primarily a wild encounter)")
    return errors


def rarity_warnings(variants: dict[str, Any]) -> list[str]:
    """Non-fatal findings against the (per-creature, overridable) rarity policy."""
    if validate_variants(variants):
        return []
    policy = dict(DEFAULT_POLICY, **(variants["rarity"].get("policy") or {}))
    warnings: list[str] = []
    for ctx in CONTEXTS:
        pp, ps = rates(variants, ctx)
        if ps > policy["max_shiny_probability"]:
            warnings.append(f"{ctx}: Shiny chance {ps} ({one_in(ps)}) is above the 'genuinely rare' policy "
                            f"limit {policy['max_shiny_probability']}")
        if pp > 0 and ps > 0 and ps / pp < policy["min_shiny_to_prismana_ratio"]:
            warnings.append(f"{ctx}: Prismana ({one_in(pp)}) is not significantly rarer than Shiny ({one_in(ps)}); "
                            f"policy wants at least {policy['min_shiny_to_prismana_ratio']}x")
    if rates(variants, "egg")[0] > 0:
        warnings.append("egg hatches can produce Prismana; the design keeps Prismana primarily a wild encounter")
    shiny = variants["shiny"]
    if shiny["enabled"]:
        n = len(shiny["appearances"])
        if n != policy["expected_shiny_appearances"]:
            warnings.append(f"{n} Shiny appearance(s) configured; {policy['expected_shiny_appearances']} expected "
                            "per species. Verify against the source data")
        if len({float(a.get('weight', 1.0)) for a in shiny["appearances"]}) > 1:
            warnings.append("Shiny appearance weights are unequal (default is equal probability)")
    if variants["rarity"].get("allow_guaranteed"):
        warnings.append("allow_guaranteed is on: guaranteed rare outcomes are possible")
    pity = variants["rarity"].get("pity")
    if pity and pity.get("enabled"):
        warnings.append("a pity system is explicitly enabled; the reference roller does not implement pity, "
                        "so its in-game behaviour must be specified and tested separately")
    return warnings


REGION_KEYS = {"id", "name", "biomes", "provenance", "allow_prismana", "appearances", "profile_overrides",
               "combat_overrides", "notes"}
INHERITANCE_MODES = ("parent", "biome", "configurable")


def _validate_regions(regions: Any, appearance_ids: set[str]) -> list[str]:
    if regions is None:
        return []
    if not isinstance(regions, dict):
        return ["variants.regions must be an object"]
    errors: list[str] = []
    forms = regions.get("forms", [])
    if not isinstance(forms, list):
        return ["variants.regions.forms must be a list"]
    seen_ids: set[str] = set()
    biome_owner: dict[str, str] = {}
    for i, r in enumerate(forms):
        where = f"variants.regions.forms[{i}]"
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]:
            errors.append(f"{where} must be an object with an 'id'")
            continue
        rid = r["id"]
        if rid == BASE_REGION or rid in seen_ids:
            errors.append(f"{where}: id {rid!r} is reserved or duplicated")
        seen_ids.add(rid)
        unknown = set(r) - REGION_KEYS
        if unknown:
            errors.append(f"{where}: unexpected keys {sorted(unknown)}")
        # Regional forms must come from source data or a deliberate user decision, never invented.
        if r.get("provenance") not in ("verified", "user"):
            errors.append(f"{where}.provenance must be 'verified' or 'user'")
        biomes = r.get("biomes", [])
        if not isinstance(biomes, list) or not all(isinstance(b, str) and b for b in biomes):
            errors.append(f"{where}.biomes must be a list of biome ids")
            biomes = []
        for b in biomes:
            if b in biome_owner and biome_owner[b] != rid:
                errors.append(f"{where}: biome {b!r} already maps to regional form {biome_owner[b]!r}")
            biome_owner[b] = rid
        if "allow_prismana" in r and not isinstance(r["allow_prismana"], bool):
            errors.append(f"{where}.allow_prismana must be a boolean")
        apps = r.get("appearances")
        if apps is not None and (not isinstance(apps, list) or set(apps) - appearance_ids):
            errors.append(f"{where}.appearances must be null or a list of configured Shiny appearance ids")
    inh = regions.get("inheritance")
    if inh is not None and (not isinstance(inh, dict) or inh.get("mode") not in INHERITANCE_MODES):
        errors.append(f"variants.regions.inheritance must be null or have mode in {INHERITANCE_MODES}")
    return errors
