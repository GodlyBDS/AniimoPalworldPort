"""Creature workflow helpers: create from a catalog entry, review and resolve associations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aniimo_pal.creature.model import ASSET_APPEARANCE_GENERIC, ASSET_FORMS, STATUSES, CreatureError, new_creature

CATALOG_SCHEMA_ID = "aniimo-pal/catalog"


def find_species(catalog: dict[str, Any], key: str) -> dict[str, Any]:
    if catalog.get("schema") != CATALOG_SCHEMA_ID:
        raise CreatureError("not an aniimo-pal/catalog document")
    for sp in catalog["species"]:
        if sp["key"] == key:
            return sp
    raise CreatureError(f"species {key!r} not found; run `aniimo-pal catalog list` to see keys")


# Placeholder rarity defaults (NOT balancing decisions; see docs/architecture.md §6).
DEFAULT_RARITY = {"wild": {"shiny_probability": 0.002, "prismana_probability": 0.0002},
                  "egg": {"shiny_probability": 0.002, "prismana_probability": 0.0}}


def _asset(entry: dict[str, Any], role: str, scope: tuple[str, str, str], auto_confirm_verified: bool,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    provenance = entry.get("provenance", "inferred")
    region, form, appearance = scope
    confirm = auto_confirm_verified and provenance == "verified" and form != "unclassified" and entry.get("sha256")
    asset = {
        "ref": {"path": entry["container"], "sha256": entry.get("sha256"), "object": entry["object"]},
        "class": entry.get("class"),
        "name": entry.get("name"),
        "role": role,
        "region": region,
        "form": form,
        "appearance": appearance,
        "association": "confirmed" if confirm else "proposed",
        "provenance": provenance,
        "evidence": list(entry.get("evidence") or []),
    }
    if entry.get("slot"):
        asset["slot"] = entry["slot"]
    if extra:
        asset.update(extra)
    return asset


def creature_from_catalog(catalog: dict[str, Any], key: str, *, creature_id: str | None = None,
                          display_name: str | None = None, rarity: dict[str, dict[str, float]] | None = None,
                          auto_confirm_verified: bool = True) -> dict[str, Any]:
    """Build a creature project from one catalog species.

    Associations backed by explicit game-data references (provenance
    ``verified``) are confirmed automatically unless ``auto_confirm_verified``
    is False; inferred ones stay ``proposed`` for the user to review.
    """
    sp = find_species(catalog, key)
    c = new_creature(creature_id or sp["suggested_id"], display_name or sp["display_name"])
    c["source"].update(catalog={"created_utc": catalog.get("created_utc"), "species_key": sp["key"],
                                "root_label": (catalog.get("source") or {}).get("root_label")})
    c["prefabs"] = [{k: p.get(k) for k in ("object", "name", "asset_path", "container", "region", "form",
                                           "appearance", "variant", "variant_provenance", "variant_evidence")}
                    for p in sp["prefabs"]]

    for entry in sp["assets"]:
        scope = (entry.get("region", "any"), entry.get("form", "shared"), entry.get("appearance", "any"))
        c["assets"].append(_asset(entry, entry["role"], scope, auto_confirm_verified))
    for anim in sp["animations"]:
        extra = {"role_guess": anim.get("role_guess"), "role_provenance": anim.get("role_provenance"),
                 "linked_via": anim.get("linked_via"), "binding_overlap": anim.get("binding_overlap"),
                 "event_count": anim.get("event_count")}
        entry = dict(anim, evidence=[f"linked-via:{anim.get('linked_via')}"])
        c["assets"].append(_asset(entry, "animation", ("any", "shared", "any"), auto_confirm_verified, extra))

    # Enable only what the source data actually contains (no invented forms or appearances).
    rarity = rarity or DEFAULT_RARITY
    variants = c["variants"]
    has_prismana = any(p.get("form") == "prismana" for p in sp["prefabs"])
    shiny_ids = sorted({p["appearance"] for p in sp["prefabs"]
                        if str(p.get("appearance", "")).startswith("shiny_") and p.get("form") != "unclassified"})
    variants["forms"]["prismana"]["enabled"] = has_prismana
    if shiny_ids:
        variants["shiny"] = {"enabled": True,
                             "appearances": [{"id": i, "name": f"Shiny {int(i.split('_')[1])}", "weight": 1.0,
                                              "provenance": "inferred"} for i in shiny_ids]}
    for ctx in ("wild", "egg"):
        variants["rarity"][ctx] = {
            "prismana_probability": float(rarity[ctx]["prismana_probability"]) if has_prismana else 0.0,
            "shiny_probability": float(rarity[ctx]["shiny_probability"]) if shiny_ids else 0.0,
        }
    c["skeleton"] = sp.get("skeleton")
    c["notes"] = list(sp.get("warnings") or [])
    c["history"] = [{"utc": _now(), "action": "created-from-catalog", "species": sp["key"]}]
    maybe_mark_mapped(c)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pending(c: dict[str, Any]) -> list[int]:
    """Indices of assets that still need a decision."""
    return [i for i, a in enumerate(c["assets"])
            if a.get("association") == "proposed" or (a.get("form") == "unclassified"
                                                      and a.get("association") != "rejected")]


def maybe_mark_mapped(c: dict[str, Any]) -> bool:
    """Advance draft -> mapped once every association is decided."""
    if c.get("status") == "draft" and not pending(c) and any(a["association"] == "confirmed" for a in c["assets"]):
        c["status"] = "mapped"
        return True
    return False


def set_associations(c: dict[str, Any], *, confirm: list[int] = (), reject: list[int] = (),
                     forms: dict[int, str] | None = None, appearances: dict[int, str] | None = None,
                     confirm_all: bool = False) -> dict[str, Any]:
    n = len(c["assets"])
    for i in [*confirm, *reject, *(forms or {}), *(appearances or {})]:
        if not 0 <= i < n:
            raise CreatureError(f"asset index {i} out of range (0..{n - 1})")
    if set(confirm) & set(reject):
        raise CreatureError("an asset cannot be confirmed and rejected at once")
    allowed_forms = [f for f in ASSET_FORMS if f != "unclassified"]
    for i, f in (forms or {}).items():
        if f not in allowed_forms:
            raise CreatureError(f"form must be one of {allowed_forms}")
        c["assets"][i]["form"] = f
        c["assets"][i]["provenance"] = "user"
    known_apps = set(ASSET_APPEARANCE_GENERIC) | {a["id"] for a in c["variants"]["shiny"].get("appearances", [])}
    for i, a in (appearances or {}).items():
        if a not in known_apps:
            raise CreatureError(f"appearance must be one of {sorted(known_apps)}")
        c["assets"][i]["appearance"] = a
        c["assets"][i]["provenance"] = "user"
    targets = set(confirm)
    if confirm_all:
        targets |= {i for i, a in enumerate(c["assets"]) if a["association"] == "proposed"}
    for i in sorted(targets):
        a = c["assets"][i]
        if not a["ref"].get("sha256"):
            raise CreatureError(f"asset {i} has no sha256; cannot confirm")
        if a.get("form") == "unclassified":
            raise CreatureError(f"asset {i} has form 'unclassified'; assign a form before confirming")
        a["association"] = "confirmed"
    for i in reject:
        c["assets"][i]["association"] = "rejected"
    c.setdefault("history", []).append({"utc": _now(), "action": "associations-updated",
                                        "confirmed": sorted(targets), "rejected": sorted(reject),
                                        "forms": {str(k): v for k, v in (forms or {}).items()},
                                        "appearances": {str(k): v for k, v in (appearances or {}).items()}})
    maybe_mark_mapped(c)
    return c


def status_at_least(c: dict[str, Any], status: str) -> bool:
    return STATUSES.index(c["status"]) >= STATUSES.index(status)
