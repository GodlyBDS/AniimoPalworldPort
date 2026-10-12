"""Creature workflow helpers: create from a catalog entry, review and resolve associations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aniimo_pal.creature.model import ASSET_VARIANTS, STATUSES, CreatureError, new_creature

CATALOG_SCHEMA_ID = "aniimo-pal/catalog"


def find_species(catalog: dict[str, Any], key: str) -> dict[str, Any]:
    if catalog.get("schema") != CATALOG_SCHEMA_ID:
        raise CreatureError("not an aniimo-pal/catalog document")
    for sp in catalog["species"]:
        if sp["key"] == key:
            return sp
    raise CreatureError(f"species {key!r} not found; run `aniimo-pal catalog list` to see keys")


def _asset(entry: dict[str, Any], role: str, variant: str, auto_confirm_verified: bool,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    provenance = entry.get("provenance", "inferred")
    confirm = auto_confirm_verified and provenance == "verified" and variant != "unclassified" and entry.get("sha256")
    asset = {
        "ref": {"path": entry["container"], "sha256": entry.get("sha256"), "object": entry["object"]},
        "class": entry.get("class"),
        "name": entry.get("name"),
        "role": role,
        "variant": variant,
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
                          display_name: str | None = None, shiny_probability: float = 0.002,
                          prismana_probability: float = 0.0005,
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
    c["prefabs"] = [{k: p[k] for k in ("object", "name", "asset_path", "container", "variant",
                                       "variant_provenance", "variant_evidence")} for p in sp["prefabs"]]

    for entry in sp["assets"]:
        c["assets"].append(_asset(entry, entry["role"], entry["variant"], auto_confirm_verified))
    for anim in sp["animations"]:
        extra = {"role_guess": anim.get("role_guess"), "role_provenance": anim.get("role_provenance"),
                 "linked_via": anim.get("linked_via"), "binding_overlap": anim.get("binding_overlap"),
                 "event_count": anim.get("event_count")}
        entry = dict(anim, evidence=[f"linked-via:{anim.get('linked_via')}"])
        c["assets"].append(_asset(entry, "animation", "shared", auto_confirm_verified, extra))

    present = {p["variant"] for p in sp["prefabs"]}
    if "shiny" in present:
        c["variants"]["shiny"] = {"enabled": True, "spawn_probability": shiny_probability}
    if "prismana" in present:
        c["variants"]["prismana"] = {"enabled": True, "spawn_probability": prismana_probability}
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
            if a.get("association") == "proposed" or (a.get("variant") == "unclassified"
                                                      and a.get("association") != "rejected")]


def maybe_mark_mapped(c: dict[str, Any]) -> bool:
    """Advance draft -> mapped once every association is decided."""
    if c.get("status") == "draft" and not pending(c) and any(a["association"] == "confirmed" for a in c["assets"]):
        c["status"] = "mapped"
        return True
    return False


def set_associations(c: dict[str, Any], *, confirm: list[int] = (), reject: list[int] = (),
                     variants: dict[int, str] | None = None, confirm_all: bool = False) -> dict[str, Any]:
    n = len(c["assets"])
    for i in [*confirm, *reject, *(variants or {})]:
        if not 0 <= i < n:
            raise CreatureError(f"asset index {i} out of range (0..{n - 1})")
    if set(confirm) & set(reject):
        raise CreatureError("an asset cannot be confirmed and rejected at once")
    for i, v in (variants or {}).items():
        if v not in ASSET_VARIANTS or v == "unclassified":
            raise CreatureError(f"variant must be one of {[x for x in ASSET_VARIANTS if x != 'unclassified']}")
        c["assets"][i]["variant"] = v
        c["assets"][i]["provenance"] = "user"
    targets = set(confirm)
    if confirm_all:
        targets |= {i for i, a in enumerate(c["assets"]) if a["association"] == "proposed"}
    for i in sorted(targets):
        a = c["assets"][i]
        if not a["ref"].get("sha256"):
            raise CreatureError(f"asset {i} has no sha256; cannot confirm")
        if a.get("variant") == "unclassified":
            raise CreatureError(f"asset {i} has variant 'unclassified'; assign a variant before confirming")
        a["association"] = "confirmed"
    for i in reject:
        c["assets"][i]["association"] = "rejected"
    c.setdefault("history", []).append({"utc": _now(), "action": "associations-updated",
                                        "confirmed": sorted(targets), "rejected": sorted(reject),
                                        "variants": {str(k): v for k, v in (variants or {}).items()}})
    maybe_mark_mapped(c)
    return c


def status_at_least(c: dict[str, Any], status: str) -> bool:
    return STATUSES.index(c["status"]) >= STATUSES.index(status)
