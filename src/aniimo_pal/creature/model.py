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
0.1.x documents are read as-is (all 0.2 additions are optional).
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
from aniimo_pal.creature.variants import variant_probabilities
from aniimo_pal.errors import ToolkitError
from aniimo_pal.manifest.io import write_json_atomic
from aniimo_pal.systems import GameSystem, bundled_system

SCHEMA_ID = "aniimo-pal/creature"
SCHEMA_VERSION = "0.2.0"

ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Workflow order: associations are reviewed ("mapped") before files are extracted.
STATUSES = ("draft", "mapped", "extracted", "converted", "configured", "validated", "exported")
VARIANTS = ("normal", "shiny", "prismana")
ASSET_VARIANTS = ("shared", *VARIANTS, "unclassified")
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
        "variants": {
            "normal": {"enabled": True},
            "shiny": {"enabled": False, "spawn_probability": 0.0},
            "prismana": {"enabled": False, "spawn_probability": 0.0},
        },
        "integration": {"mode": "replace", "replace_target": None, "notes": ""},
        "skeleton": None,
        "combat": new_combat_section(),
        # Original Aniimo stats/elements/work and their Palworld translation (see creature/profile.py).
        "source_profile": None,
        "translation": None,
    }


def validate_creature(data: Any, source_system: GameSystem | None = None,
                      target_system: GameSystem | None = None) -> list[str]:
    """Return a list of problems (empty when valid).

    Stat/element/work vocabularies are checked against the given game-system
    definitions, defaulting to the bundled (unverified) Aniimo and Palworld ones.
    """
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
    if not isinstance(variants, dict) or set(variants) != set(VARIANTS):
        errors.append(f"variants must contain exactly {VARIANTS}")
        variants = {}
    else:
        if variants["normal"].get("enabled") is not True:
            errors.append("variants.normal must be enabled (rare variants replace the normal outcome)")
        for name in ("shiny", "prismana"):
            v = variants[name]
            p = v.get("spawn_probability", 0.0)
            if not isinstance(v.get("enabled"), bool):
                errors.append(f"variants.{name}.enabled must be a boolean")
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
                errors.append(f"variants.{name}.spawn_probability must be a number in [0, 1]")
            elif v.get("enabled") and p == 0:
                errors.append(f"variants.{name} is enabled but spawn_probability is 0")
            elif v.get("enabled") is False and p != 0:
                errors.append(f"variants.{name} is disabled but has a non-zero spawn_probability")
        if not errors:
            try:
                variant_probabilities(variants)
            except ValueError as exc:
                errors.append(f"variants: {exc}")

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
        variant = a.get("variant", "shared")
        state = a.get("association")
        if variant not in ASSET_VARIANTS:
            errors.append(f"{where}.variant must be one of {ASSET_VARIANTS}")
        elif variant in ("shiny", "prismana") and variants and not variants[variant].get("enabled") \
                and state != "rejected":
            errors.append(f"{where} is assigned to disabled variant {variant!r}")
        elif variant == "unclassified" and status_rank >= STATUSES.index("mapped") and state != "rejected":
            errors.append(f"{where}: variant must be resolved (not 'unclassified') once status is 'mapped'")
        if a.get("provenance", "user") not in PROVENANCE:
            errors.append(f"{where}.provenance must be one of {PROVENANCE}")
        if state not in ASSOCIATION_STATES:
            errors.append(f"{where}.association must be one of {ASSOCIATION_STATES}")
        elif state == "confirmed" and sha is None:
            errors.append(f"{where}: confirmed associations must pin ref.sha256")
        elif state == "proposed" and status_rank >= STATUSES.index("mapped"):
            errors.append(f"{where}: proposed associations must be confirmed or rejected once status is 'mapped'")
        key = (str(ref.get("path")), str(ref.get("object", "")), str(variant))
        if key in seen:
            errors.append(f"{where} duplicates an earlier asset reference")
        seen.add(key)

    anim_objects = {str((a.get("ref") or {}).get("object")) for a in assets
                    if isinstance(a, dict) and a.get("role") == "animation" and a.get("association") != "rejected"}
    other_objects = {str((a.get("ref") or {}).get("object")) for a in assets
                     if isinstance(a, dict) and a.get("role") != "animation" and a.get("association") != "rejected"}
    errors.extend(validate_combat(data.get("combat"), anim_objects, other_objects))
    if isinstance(variants, dict) and variants:
        errors.extend(validate_variant_overrides(variants))

    src = source_system or bundled_system("aniimo")
    dst = target_system or bundled_system("palworld")
    errors.extend(validate_source_profile(data.get("source_profile"), src))
    errors.extend(validate_translation(data.get("translation"), data.get("source_profile"), dst))
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
    return data


def save_creature(data: dict[str, Any], path: str | Path) -> Path:
    errors = validate_creature(data)
    if errors:
        raise CreatureError("refusing to save invalid creature project: " + "; ".join(errors))
    return write_json_atomic(data, path)
