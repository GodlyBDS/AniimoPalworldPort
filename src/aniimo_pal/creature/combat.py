"""Reserved combat section of ``creature.json`` (structure + validation only).

The Combat Translation System is a later milestone (see docs/architecture.md
§9). This module fixes the *data shape* now so discovery and editing tools
can fill it in without migrations:

* Every fact carries ``provenance``: ``verified`` (read from game data, e.g.
  an animation event), ``inferred`` (rules/heuristics, e.g. a clip name
  containing "ult") or ``user`` (set in the editor). Missing information stays
  absent; it is never invented.
* Abilities are sequences of **phases** (one or more animation clips), so
  multi-stage attacks and Ultimates are first-class.
* The Ultimate maps to the creature's **Partner Skill** by default (project
  design decision), keeping its original clips, effects and event timing.
* Variants inherit the base combat configuration; per-variant overrides must
  be ``verified`` or ``user`` provenance.

Nothing here claims Palworld supports a given mapping; ``palworld_mapping``
records intent and review status only.
"""

from __future__ import annotations

from typing import Any

PROVENANCE = ("verified", "inferred", "user")
ABILITY_KINDS = ("basic", "special", "ultimate", "movement", "passive", "transformation", "support")
INTEGRATION_PROFILES = ("ai_companion", "partner_skill", "mounted", "player_ability")
MAPPING_TARGETS = ("active_skill", "partner_skill", "attack_sequence", "movement", "passive", "unmapped")
MAPPING_STATUS = ("unmapped", "proposed", "confirmed")
ULTIMATE_TARGETS = ("partner_skill", "active_skill", "none")
EFFECT_KINDS = ("vfx", "sfx", "projectiles")


def new_combat_section() -> dict[str, Any]:
    return {
        "profiles": ["ai_companion"],
        "abilities": [],
        "ultimate": {"ability": None, "target": "partner_skill"},
    }


def _check_param(where: str, value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict) or "value" not in value:
        errors.append(f"{where} must be an object with 'value' and 'provenance'")
    elif value.get("provenance") not in PROVENANCE:
        errors.append(f"{where}.provenance must be one of {PROVENANCE}")


def validate_combat(combat: Any, animation_objects: set[str], other_objects: set[str]) -> list[str]:
    """Structural validation. ``animation_objects``: object ids of the creature's animation assets."""
    if combat is None:
        return []
    if not isinstance(combat, dict):
        return ["combat must be an object"]
    errors: list[str] = []
    profiles = combat.get("profiles", [])
    if not isinstance(profiles, list) or any(p not in INTEGRATION_PROFILES for p in profiles):
        errors.append(f"combat.profiles must be a list drawn from {INTEGRATION_PROFILES}")

    abilities = combat.get("abilities", [])
    if not isinstance(abilities, list):
        return errors + ["combat.abilities must be a list"]
    ids: dict[str, dict[str, Any]] = {}
    for i, ab in enumerate(abilities):
        where = f"combat.abilities[{i}]"
        if not isinstance(ab, dict):
            errors.append(f"{where} must be an object")
            continue
        aid = ab.get("id")
        if not isinstance(aid, str) or not aid:
            errors.append(f"{where}.id is required")
        elif aid in ids:
            errors.append(f"{where}.id {aid!r} is duplicated")
        else:
            ids[aid] = ab
        if ab.get("kind") not in ABILITY_KINDS:
            errors.append(f"{where}.kind must be one of {ABILITY_KINDS}")
        if ab.get("provenance") not in PROVENANCE:
            errors.append(f"{where}.provenance must be one of {PROVENANCE}")
        phases = ab.get("phases", [])
        if not isinstance(phases, list):
            errors.append(f"{where}.phases must be a list")
            phases = []
        for j, ph in enumerate(phases):
            clip = ph.get("clip") if isinstance(ph, dict) else None
            if clip not in animation_objects:
                errors.append(f"{where}.phases[{j}].clip must reference one of the creature's animation assets")
        for kind, refs in (ab.get("effects") or {}).items():
            if kind not in EFFECT_KINDS:
                errors.append(f"{where}.effects.{kind}: unknown effect kind")
            elif not isinstance(refs, list) or any(r not in other_objects | animation_objects for r in refs):
                errors.append(f"{where}.effects.{kind} must list asset object ids of this creature")
        for key, value in (ab.get("parameters") or {}).items():
            _check_param(f"{where}.parameters.{key}", value, errors)
        mapping = ab.get("palworld_mapping") or {"target": "unmapped", "status": "unmapped"}
        if mapping.get("target") not in MAPPING_TARGETS:
            errors.append(f"{where}.palworld_mapping.target must be one of {MAPPING_TARGETS}")
        if mapping.get("status", "unmapped") not in MAPPING_STATUS:
            errors.append(f"{where}.palworld_mapping.status must be one of {MAPPING_STATUS}")

    ult = combat.get("ultimate") or {}
    if ult.get("target", "partner_skill") not in ULTIMATE_TARGETS:
        errors.append(f"combat.ultimate.target must be one of {ULTIMATE_TARGETS}")
    ult_id = ult.get("ability")
    if ult_id is not None:
        if ult_id not in ids:
            errors.append("combat.ultimate.ability must reference an ability id")
        elif ids[ult_id].get("kind") != "ultimate":
            errors.append("combat.ultimate.ability must reference an ability of kind 'ultimate'")
    return errors


def validate_variant_overrides(variants: dict[str, Any]) -> list[str]:
    errors = []
    for name, v in variants.items():
        ov = v.get("combat_overrides") if isinstance(v, dict) else None
        if ov is None:
            continue
        if name == "normal":
            errors.append("variants.normal cannot have combat_overrides (it is the base configuration)")
        elif not isinstance(ov, dict) or ov.get("provenance") not in ("verified", "user"):
            errors.append(f"variants.{name}.combat_overrides needs provenance 'verified' or 'user'")
    return errors
