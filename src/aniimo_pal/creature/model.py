"""Creature project model (``creature.json``).

A creature project captures everything decided about porting ONE Aniimo
species, so work can stop and resume, and so finished creatures can be bundled
into multi-creature mod packs later. It lives in a user workspace outside the
repository (``<workspace>/creatures/<id>/creature.json``) because it references
proprietary files.

Assets are referenced by content hash (``sha256``) plus the manifest path, so
an association survives re-scans and game updates that move files; a changed
hash means the asset really changed and must be re-confirmed.

Schema version 0.x: fields may still change before the editor milestone.
0.1.x/0.2.x documents are migrated in memory on load/validate (``migrate_creature``):
their mutually exclusive Normal/Shiny/Prismana variants become the 0.3 identity
model (independent form + appearance rolls, regional forms, wild/egg rarity).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aniimo_pal.creature.combat import new_combat_section, validate_combat, validate_variant_overrides
from aniimo_pal.creature.profile import (
    validate_source_profile,
    validate_translation,
    validate_variant_profile_overrides,
)
from aniimo_pal.creature.variants import (
    BASE_REGION,
    FORMS,
    STANDARD,
    new_variants,
    regional_forms,
    shiny_appearances,
    validate_variants,
)
from aniimo_pal.errors import ToolkitError
from aniimo_pal.manifest.io import write_json_atomic
from aniimo_pal.systems import GameSystem, bundled_system

SCHEMA_ID = "aniimo-pal/creature"
SCHEMA_VERSION = "0.3.0"

ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Workflow order: associations are reviewed ("mapped") before files are extracted.
STATUSES = ("draft", "mapped", "extracted", "converted", "configured", "validated", "exported")
# Per-asset identity scope: which individuals an asset applies to.
ASSET_FORMS = ("shared", *FORMS, "unclassified")
ASSET_APPEARANCE_GENERIC = ("any", STANDARD, "shiny")  # plus configured shiny appearance ids
ASSET_REGION_GENERIC = ("any", BASE_REGION)            # plus configured regional form ids
PROVENANCE = ("verified", "inferred", "user")
ASSET_ROLES = ("mesh", "skeleton", "texture", "material", "animation", "icon", "audio", "other")
ASSOCIATION_STATES = ("proposed", "confirmed", "rejected")
INTEGRATION_MODES = ("replace", "new_pal")


class CreatureError(ToolkitError):
    """Invalid creature project."""


def new_creature(creature_id: str, display_name: str) -> dict[str, Any]:
    if not ID_RE.match(creature_id):
        raise CreatureError("creature id must match ^[a-z][a-z0-9_]{2,47}$ (lowercase, digits, underscore)")
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "id": creature_id,
        "display_name": display_name,
        "status": "draft",
        "source": {"manifest_sha256": None, "game_version": None},
        "assets": [],
        "variants": new_variants(),
        # One project per evolution stage; stages of a line link to each other.
        "evolution": None,
        "integration": {"mode": "replace", "replace_target": None, "notes": ""},
        "skeleton": None,
        "combat": new_combat_section(),
        # Original Aniimo stats/elements/work and their Palworld translation (see creature/profile.py).
        "source_profile": None,
        "translation": None,
    }


def _validate_evolution(evo: Any) -> list[str]:
    if evo is None:
        return []
    if not isinstance(evo, dict):
        return ["evolution must be an object or null"]
    errors = []
    stage = evo.get("stage")
    if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
        errors.append("evolution.stage must be a positive integer")
    for key in ("previous", "next"):
        v = evo.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) and ID_RE.match(x) for x in v)):
            errors.append(f"evolution.{key} must be null or a list of creature ids")
    if evo.get("provenance") not in PROVENANCE:
        errors.append(f"evolution.provenance must be one of {PROVENANCE}")
    return errors


def migrate_creature(data: Any) -> Any:
    """Upgrade a 0.1.x/0.2.x project to 0.3 (returns a new dict; unknown input is returned unchanged).

    0.1/0.2 modelled Normal/Shiny/Prismana as mutually exclusive outcomes of one
    roll. 0.3 rolls form and appearance independently, per wild/egg context. The
    old per-spawn chances are carried over as wild chances, eggs get the same
    Shiny chance and no Prismana, and a history entry asks the user to review.
    """
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_ID:
        return data
    version = str(data.get("schema_version", ""))
    if not (version.startswith("0.1") or version.startswith("0.2")):
        return data
    import copy

    out = copy.deepcopy(data)
    old = out.get("variants") or {}
    new = new_variants()
    shiny, prismana = old.get("shiny") or {}, old.get("prismana") or {}
    new["forms"]["prismana"]["enabled"] = bool(prismana.get("enabled"))
    new["shiny"]["enabled"] = bool(shiny.get("enabled"))
    if shiny.get("enabled"):
        new["shiny"]["appearances"] = shiny_appearances(1)
    pp = float(prismana.get("spawn_probability", 0.0)) if prismana.get("enabled") else 0.0
    ps = float(shiny.get("spawn_probability", 0.0)) if shiny.get("enabled") else 0.0
    new["rarity"]["wild"] = {"prismana_probability": pp, "shiny_probability": ps}
    new["rarity"]["egg"] = {"prismana_probability": 0.0, "shiny_probability": ps}
    for src_name, dst in (("prismana", new["forms"]["prismana"]), ("shiny", new["shiny"])):
        for key in ("profile_overrides", "combat_overrides"):
            if key in (old.get(src_name) or {}):
                dst[key] = old[src_name][key]
    out["variants"] = new
    mapping = {"normal": ("normal", STANDARD), "shiny": ("normal", "shiny_01"), "prismana": ("prismana", STANDARD),
               "shared": ("shared", "any"), "unclassified": ("unclassified", "any")}
    for a in out.get("assets") or []:
        if isinstance(a, dict) and "variant" in a:
            a["form"], a["appearance"] = mapping.get(a.pop("variant"), ("shared", "any"))
            a.setdefault("region", "any")
    for p in out.get("prefabs") or []:
        if isinstance(p, dict) and "variant" in p:
            form, app = mapping.get(p["variant"], ("unclassified", STANDARD))
            p.update(region=BASE_REGION, form=form, appearance=app)
    out.setdefault("evolution", None)
    out["schema_version"] = SCHEMA_VERSION
    out.setdefault("history", []).append({"action": f"migrated-from-{version}",
                                          "note": "rarity model changed to independent form/shiny rolls; review "
                                                  "variants.rarity and Shiny appearances"})
    return out


def validate_creature(data: Any, source_system: GameSystem | None = None,
                      target_system: GameSystem | None = None) -> list[str]:
    """Return a list of problems (empty when valid).

    Stat/element/work vocabularies are checked against the given game-system
    definitions, defaulting to the bundled (unverified) Aniimo and Palworld ones.
    Older 0.x documents are migrated in memory first.
    """
    data = migrate_creature(data)
    if not isinstance(data, dict):
        return ["creature project must be a JSON object"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA_ID:
        errors.append(f"schema must be {SCHEMA_ID!r}")
    if not isinstance(data.get("id"), str) or not ID_RE.match(data["id"]):
        errors.append("id must match ^[a-z][a-z0-9_]{2,47}$")
    if not isinstance(data.get("display_name"), str) or not data["display_name"].strip():
        errors.append("display_name must be a non-empty string")
    if data.get("schema") == SCHEMA_ID and not str(data.get("schema_version", "")).startswith("0."):
        errors.append(f"unsupported schema_version {data.get('schema_version')!r} (expected 0.x)")
    if data.get("status") not in STATUSES:
        errors.append(f"status must be one of {STATUSES}")
    status_rank = STATUSES.index(data["status"]) if data.get("status") in STATUSES else 0

    variants = data.get("variants")
    variant_errors = validate_variants(variants)
    errors.extend(variant_errors)
    if variant_errors:
        variants = None
    appearance_ids = {a["id"] for a in (variants or {}).get("shiny", {}).get("appearances", [])} if variants else set()
    region_ids = {r["id"] for r in regional_forms(variants)} if variants else set()
    errors.extend(_validate_evolution(data.get("evolution")))

    seen: set[tuple[str, str, str]] = set()
    assets = data.get("assets")
    if not isinstance(assets, list):
        errors.append("assets must be a list")
        assets = []
    for i, a in enumerate(assets):
        where = f"assets[{i}]"
        if not isinstance(a, dict):
            errors.append(f"{where} must be an object")
            continue
        ref = a.get("ref") or {}
        if not isinstance(ref.get("path"), str) or not ref["path"]:
            errors.append(f"{where}.ref.path is required")
        sha = ref.get("sha256")
        if sha is not None and not (isinstance(sha, str) and SHA256_RE.match(sha)):
            errors.append(f"{where}.ref.sha256 must be 64 lowercase hex characters")
        if a.get("role") not in ASSET_ROLES:
            errors.append(f"{where}.role must be one of {ASSET_ROLES}")
        state = a.get("association")
        form = a.get("form", "shared")
        appearance = a.get("appearance", "any")
        region = a.get("region", "any")
        if form not in ASSET_FORMS:
            errors.append(f"{where}.form must be one of {ASSET_FORMS}")
        elif form == "prismana" and variants and not variants["forms"]["prismana"]["enabled"] and state != "rejected":
            errors.append(f"{where} is assigned to the disabled Prismana form")
        elif form == "unclassified" and status_rank >= STATUSES.index("mapped") and state != "rejected":
            errors.append(f"{where}: form must be resolved (not 'unclassified') once status is 'mapped'")
        if appearance not in ASSET_APPEARANCE_GENERIC and appearance not in appearance_ids:
            errors.append(f"{where}.appearance must be one of {ASSET_APPEARANCE_GENERIC} or a configured Shiny id")
        elif appearance not in ("any", STANDARD) and variants and not variants["shiny"]["enabled"] \
                and state != "rejected":
            errors.append(f"{where} is assigned to a Shiny appearance but Shiny is disabled")
        if region not in ASSET_REGION_GENERIC and region not in region_ids:
            errors.append(f"{where}.region must be 'any', 'base' or a configured regional form id")
        if a.get("provenance", "user") not in PROVENANCE:
            errors.append(f"{where}.provenance must be one of {PROVENANCE}")
        if state not in ASSOCIATION_STATES:
            errors.append(f"{where}.association must be one of {ASSOCIATION_STATES}")
        elif state == "confirmed" and sha is None:
            errors.append(f"{where}: confirmed associations must pin ref.sha256")
        elif state == "proposed" and status_rank >= STATUSES.index("mapped"):
            errors.append(f"{where}: proposed associations must be confirmed or rejected once status is 'mapped'")
        key = (str(ref.get("path")), str(ref.get("object", "")), f"{region}|{form}|{appearance}")
        if key in seen:
            errors.append(f"{where} duplicates an earlier asset reference")
        seen.add(key)

    anim_objects = {str((a.get("ref") or {}).get("object")) for a in assets
                    if isinstance(a, dict) and a.get("role") == "animation" and a.get("association") != "rejected"}
    other_objects = {str((a.get("ref") or {}).get("object")) for a in assets
                     if isinstance(a, dict) and a.get("role") != "animation" and a.get("association") != "rejected"}
    errors.extend(validate_combat(data.get("combat"), anim_objects, other_objects))
    if variants:
        errors.extend(validate_variant_overrides(variants))

    src = source_system or bundled_system("aniimo")
    dst = target_system or bundled_system("palworld")
    errors.extend(validate_source_profile(data.get("source_profile"), src))
    errors.extend(validate_translation(data.get("translation"), data.get("source_profile"), dst))
    if variants:  # only meaningful once the variants block itself is valid
        errors.extend(validate_variant_profile_overrides(data, src, dst))

    integ = data.get("integration") or {}
    if integ.get("mode") not in INTEGRATION_MODES:
        errors.append(f"integration.mode must be one of {INTEGRATION_MODES}")
    elif integ["mode"] == "replace" and data.get("status") in ("configured", "validated", "exported") \
            and not integ.get("replace_target"):
        errors.append("integration.replace_target is required once a replace-mode creature is configured")
    return errors


def creature_path(workspace: str | Path, creature_id: str) -> Path:
    return Path(workspace) / "creatures" / creature_id / "creature.json"


def load_creature(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CreatureError(f"creature project not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise CreatureError(f"{p} is not valid JSON: {exc}") from exc
    return migrate_creature(data)


def save_creature(data: dict[str, Any], path: str | Path) -> Path:
    errors = validate_creature(data)
    if errors:
        raise CreatureError("refusing to save invalid creature project: " + "; ".join(errors))
    return write_json_atomic(data, path)
