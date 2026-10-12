"""Stats, element types, tiers and work suitability in ``creature.json``.

Two strictly separate sections:

* ``source_profile``: the ORIGINAL Aniimo data (stats, elements, work/job
  levels, tiers, stage, role). Never rewritten by translation. Every value
  carries ``provenance`` (verified | inferred | user) and optionally ``source``
  (where it was read, e.g. an object id + field).
* ``translation``: the Palworld values derived from it (stats, elements,
  work suitability), each recording ``from`` (source ids), ``method`` (rule id
  or ``user``), ``auto_value`` (what the rules produced) and ``overridden``.
  Unmappable source elements are listed explicitly in ``unmapped_elements``.

Both sections record the game-system definition (id + version) they use, and
``translation.profile`` names the balancing preset (faithful |
vanilla_balanced | custom) and its rules version. Rarity (``variants.*.spawn_probability``)
and combat (``combat``) live elsewhere and must never appear here.

Variants inherit the base profile. ``variants.<shiny|prismana>.profile_overrides``
may override parts of it, with provenance ``verified`` or ``user`` only.

This module validates structure and vocabulary. It contains no conversion
formulas (see docs/architecture.md §10).
"""

from __future__ import annotations

import copy
from typing import Any

from aniimo_pal.systems import GameSystem

PROVENANCE = ("verified", "inferred", "user")
PROFILE_PRESETS = ("faithful", "vanilla_balanced", "custom")
FORBIDDEN_IN_OVERRIDES = {"spawn_probability", "enabled", "combat", "combat_overrides"}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_valued(where: str, entry: Any, errors: list[str], *, numeric: bool = True) -> None:
    if not isinstance(entry, dict) or "value" not in entry:
        errors.append(f"{where} must be an object with 'value' and 'provenance'")
        return
    if numeric and entry["value"] is not None and not _num(entry["value"]):
        errors.append(f"{where}.value must be a number or null")
    if entry.get("provenance") not in PROVENANCE:
        errors.append(f"{where}.provenance must be one of {PROVENANCE}")


def _check_system_ref(where: str, ref: Any, system: GameSystem | None, errors: list[str]) -> None:
    if ref is None:
        return
    if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not isinstance(ref.get("version"), str):
        errors.append(f"{where} must be {{'id': ..., 'version': ...}}")
    elif system is not None and ref["id"] != system.id:
        errors.append(f"{where}.id is {ref['id']!r} but validated against {system.id!r}")


def validate_source_profile(sp: Any, system: GameSystem | None, where: str = "source_profile") -> list[str]:
    if sp is None:
        return []
    if not isinstance(sp, dict):
        return [f"{where} must be an object"]
    errors: list[str] = []
    _check_system_ref(f"{where}.system", sp.get("system"), system, errors)
    for key, entry in (sp.get("stats") or {}).items():
        _check_valued(f"{where}.stats.{key}", entry, errors)
        if system is not None and key not in system.stats:
            errors.append(f"{where}.stats.{key}: unknown {system.id} stat (known: {sorted(system.stats)})")
    elements = sp.get("elements") or []
    if not isinstance(elements, list):
        errors.append(f"{where}.elements must be a list")
        elements = []
    seen = set()
    for i, el in enumerate(elements):
        w = f"{where}.elements[{i}]"
        if not isinstance(el, dict) or not isinstance(el.get("id"), str):
            errors.append(f"{w} must be an object with an 'id'")
            continue
        if el.get("provenance") not in PROVENANCE:
            errors.append(f"{w}.provenance must be one of {PROVENANCE}")
        if system is not None and el["id"] not in system.elements:
            canon = system.canonical_element(el["id"])
            hint = f"; use canonical id {canon!r}" if canon else ""
            errors.append(f"{w}: {el['id']!r} is not a {system.id} element{hint}")
        if el["id"] in seen:
            errors.append(f"{w}: duplicate element {el['id']!r}")
        seen.add(el["id"])
    if system is not None and len(elements) > system.max_elements:
        errors.append(f"{where}.elements: {system.id} creatures have at most {system.max_elements} elements")
    _validate_work(f"{where}.work", sp.get("work"), system, errors, require_known=False)
    for key, tier in (sp.get("tiers") or {}).items():
        _check_valued(f"{where}.tiers.{key}", tier, errors, numeric=False)
    for key in ("stage", "role"):
        if sp.get(key) is not None:
            _check_valued(f"{where}.{key}", sp[key], errors, numeric=False)
    return errors


def _validate_work(where: str, work: Any, system: GameSystem | None, errors: list[str], *, require_known: bool) -> None:
    if work is None:
        return
    if not isinstance(work, list):
        errors.append(f"{where} must be a list")
        return
    seen = set()
    for i, w in enumerate(work):
        loc = f"{where}[{i}]"
        if not isinstance(w, dict) or not isinstance(w.get("id"), str):
            errors.append(f"{loc} must be an object with an 'id'")
            continue
        if w["id"] in seen:
            errors.append(f"{loc}: duplicate work category {w['id']!r}")
        seen.add(w["id"])
        level = w.get("level")
        if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
            errors.append(f"{loc}.level must be an integer or null")
        elif system is not None and level is not None and not system.work_level_min <= level <= system.work_level_max:
            errors.append(f"{loc}.level {level} outside {system.id} range "
                          f"{system.work_level_min}-{system.work_level_max}")
        if system is not None and system.work_categories and w["id"] not in system.work_categories:
            errors.append(f"{loc}: {w['id']!r} is not a {system.id} work category")
        elif require_known and system is not None and not system.work_categories:
            errors.append(f"{loc}: {system.id} defines no work categories")


def validate_translation(tr: Any, source: dict[str, Any] | None, system: GameSystem | None,
                         where: str = "translation") -> list[str]:
    if tr is None:
        return []
    if not isinstance(tr, dict):
        return [f"{where} must be an object"]
    errors: list[str] = []
    _check_system_ref(f"{where}.target_system", tr.get("target_system"), system, errors)
    profile = tr.get("profile")
    if profile is not None:
        if not isinstance(profile, dict) or profile.get("id") not in PROFILE_PRESETS:
            errors.append(f"{where}.profile.id must be one of {PROFILE_PRESETS}")
    source = source or {}
    src_stats = set((source.get("stats") or {}).keys())
    src_elements = {e.get("id") for e in source.get("elements") or [] if isinstance(e, dict)}
    src_work = {w.get("id") for w in source.get("work") or [] if isinstance(w, dict)}

    for key, entry in (tr.get("stats") or {}).items():
        w = f"{where}.stats.{key}"
        if not isinstance(entry, dict) or not _num(entry.get("value")):
            errors.append(f"{w}.value must be a number")
            continue
        if system is not None and key not in system.stats:
            errors.append(f"{w}: unknown {system.id} stat (known: {sorted(system.stats)})")
        if not entry.get("method"):
            errors.append(f"{w}.method is required (rule id or 'user')")
        unknown = set(entry.get("from") or []) - src_stats
        if unknown:
            errors.append(f"{w}.from references source stats not in source_profile: {sorted(unknown)}")

    elements = tr.get("elements")
    if elements is not None:
        if not isinstance(elements, list):
            errors.append(f"{where}.elements must be a list")
            elements = []
        mapped_from: set[str] = set()
        targets = set()
        for i, el in enumerate(elements):
            w = f"{where}.elements[{i}]"
            if not isinstance(el, dict) or not isinstance(el.get("id"), str):
                errors.append(f"{w} must be an object with an 'id'")
                continue
            if system is not None and el["id"] not in system.elements:
                errors.append(f"{w}: {el['id']!r} is not a {system.id} element (types are never invented)")
            if el["id"] in targets:
                errors.append(f"{w}: duplicate target element {el['id']!r}")
            targets.add(el["id"])
            if not el.get("method"):
                errors.append(f"{w}.method is required (rule id or 'user')")
            frm = set(el.get("from") or [])
            if frm - src_elements:
                errors.append(f"{w}.from references source elements not in source_profile: {sorted(frm - src_elements)}")
            mapped_from |= frm
        if system is not None and len(elements) > system.max_elements:
            errors.append(f"{where}.elements: {system.id} supports at most {system.max_elements} elements")
        unmapped = set(tr.get("unmapped_elements") or [])
        if unmapped - src_elements:
            errors.append(f"{where}.unmapped_elements lists unknown source elements: {sorted(unmapped - src_elements)}")
        missing = src_elements - mapped_from - unmapped
        if missing:
            errors.append(f"{where}: source elements {sorted(missing)} must be mapped or listed in unmapped_elements")

    _validate_work(f"{where}.work", tr.get("work"), system, errors, require_known=True)
    for i, w in enumerate(tr.get("work") or []):
        if isinstance(w, dict):
            if not w.get("method"):
                errors.append(f"{where}.work[{i}].method is required (rule id or 'user')")
            unknown = set(w.get("from") or []) - src_work
            if unknown:
                errors.append(f"{where}.work[{i}].from references source work not in source_profile: {sorted(unknown)}")
    return errors


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            out[k] = deep_merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(override)


def effective_profile(creature: dict[str, Any], variant: str) -> dict[str, Any]:
    """Source profile + translation for one variant (base, then that variant's overrides)."""
    base = {"source_profile": creature.get("source_profile") or {}, "translation": creature.get("translation") or {}}
    if variant == "normal":
        return copy.deepcopy(base)
    ov = ((creature.get("variants") or {}).get(variant) or {}).get("profile_overrides") or {}
    merged = copy.deepcopy(base)
    for section in ("source_profile", "translation"):
        if section in ov:
            merged[section] = deep_merge(merged[section], ov[section])
    return merged


def validate_variant_profile_overrides(creature: dict[str, Any], src: GameSystem | None,
                                       dst: GameSystem | None) -> list[str]:
    errors: list[str] = []
    for name, v in (creature.get("variants") or {}).items():
        ov = v.get("profile_overrides") if isinstance(v, dict) else None
        if ov is None:
            continue
        where = f"variants.{name}.profile_overrides"
        if name == "normal":
            errors.append(f"{where}: the normal variant is the base profile and cannot override it")
            continue
        if not isinstance(ov, dict) or ov.get("provenance") not in ("verified", "user"):
            errors.append(f"{where} needs provenance 'verified' or 'user'")
            continue
        bad = set(ov) - {"provenance", "source_profile", "translation", "notes"}
        if bad:
            errors.append(f"{where}: unexpected keys {sorted(bad)} (rarity and combat are configured elsewhere)")

        def walk(value: Any) -> set[str]:
            found = set()
            if isinstance(value, dict):
                for k, sub in value.items():
                    if k in FORBIDDEN_IN_OVERRIDES:
                        found.add(k)
                    found |= walk(sub)
            elif isinstance(value, list):
                for sub in value:
                    found |= walk(sub)
            return found

        leaked = walk({k: ov.get(k) for k in ("source_profile", "translation")})
        if leaked:
            errors.append(f"{where}: {sorted(leaked)} do not belong in a stat profile")
        eff = effective_profile(creature, name)
        errors += [f"{where} -> {e}" for e in validate_source_profile(eff["source_profile"], src)]
        errors += [f"{where} -> {e}" for e in validate_translation(eff["translation"], eff["source_profile"], dst)]
    return errors


def profile_warnings(creature: dict[str, Any], src: GameSystem | None, dst: GameSystem | None) -> list[str]:
    """Non-fatal findings: unverified data and suspicious configurations."""
    warnings: list[str] = []
    sp = creature.get("source_profile") or {}
    tr = creature.get("translation") or {}
    if (sp or tr) and src is not None and not src.verified:
        warnings.append(f"source system definition {src.id} {src.version} is unverified (community sources)")
    if tr and dst is not None and not dst.verified:
        warnings.append(f"target system definition {dst.id} {dst.version} is unverified; confirm ranges before export")
    stats = sp.get("stats") or {}
    if tr.get("stats") and not any((s or {}).get("provenance") == "verified" for s in stats.values()):
        warnings.append("translated stats are not backed by any verified source stat")
    for key, tier in (sp.get("tiers") or {}).items():
        if isinstance(tier, dict) and not tier.get("meaning"):
            warnings.append(f"source tier {key!r} has no documented meaning; do not translate it until verified")
    for key, entry in (tr.get("stats") or {}).items():
        if isinstance(entry, dict) and not entry.get("from") and entry.get("method") != "user":
            warnings.append(f"translated stat {key!r} has no source stats in 'from'")
    if dst is not None:
        for name in ("shiny", "prismana"):
            eff = effective_profile(creature, name)
            ov = ((creature.get("variants") or {}).get(name) or {}).get("profile_overrides")
            work = (eff["translation"] or {}).get("work") or []
            if ov and work and all(w.get("level") == dst.work_level_max for w in work if isinstance(w, dict)):
                warnings.append(f"{name}: every work suitability is at the maximum level; rare variants "
                                "must not be maxed out by default")
    return warnings
