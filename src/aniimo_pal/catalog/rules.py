"""Reusable association rules (keywords and thresholds) for the catalog.

Rules are data, not code: a rules TOML file can be kept in the workspace
(``<workspace>/rules/catalog.toml``) and refined as more creatures are ported.
``config/catalog_rules.toml`` documents the defaults (a test keeps them in sync).

Keyword matching is token-based and case-insensitive: names are split on
``_ - . space /`` and camelCase boundaries, so ``FoxletShiny`` and
``foxlet_shiny`` both contain the token ``shiny``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ConfigError

_DEFAULT_VARIANT_KEYWORDS = {
    "shiny": ["shiny", "rare", "alt", "color2", "colour2"],
    "prismana": ["prismana", "prism", "rainbow"],
}
# Clip-role guesses from names are INFERRED, never treated as verified data.
_DEFAULT_CLIP_ROLE_KEYWORDS = {
    "idle": ["idle", "stand", "wait", "breath"],
    "walk": ["walk"],
    "run": ["run", "sprint", "dash"],
    "jump": ["jump", "fall", "land"],
    "attack": ["attack", "atk", "melee", "bite", "claw", "slash", "combo"],
    "skill": ["skill", "spell", "cast", "special"],
    "ultimate": ["ult", "ultimate"],
    "hit_react": ["hit", "hurt", "damage", "dmg"],
    "death": ["die", "death", "dead"],
    "sleep": ["sleep", "rest"],
    "transform": ["transform", "morph"],
}
_DEFAULT_STOP_TOKENS = ["prefab", "body", "mesh", "model", "lod0", "skin", "mdl", "char", "npc"]


@dataclass(frozen=True)
class CatalogRules:
    variant_keywords: dict[str, list[str]] = field(default_factory=lambda: dict(_DEFAULT_VARIANT_KEYWORDS))
    clip_role_keywords: dict[str, list[str]] = field(default_factory=lambda: dict(_DEFAULT_CLIP_ROLE_KEYWORDS))
    stop_tokens: list[str] = field(default_factory=lambda: list(_DEFAULT_STOP_TOKENS))
    include_static_meshes: bool = False
    min_binding_matches: int = 3
    min_binding_ratio: float = 0.8
    max_binding_match_species: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_rules(path: str | Path | None) -> CatalogRules:
    rules = CatalogRules()
    if path is None:
        return rules
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"rules file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    section = data.get("catalog", data)
    known = set(asdict(rules))
    unknown = set(section) - known
    if unknown:
        raise ConfigError(f"unknown catalog rule keys: {sorted(unknown)}")
    values = asdict(rules)
    for key, value in section.items():
        default = values[key]
        if isinstance(default, dict):
            if not isinstance(value, dict) or not all(
                    isinstance(v, list) and all(isinstance(s, str) for s in v) for v in value.values()):
                raise ConfigError(f"catalog.{key} must be a table of string lists")
            value = {k: [s.lower() for s in v] for k, v in value.items()}
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
                raise ConfigError(f"catalog.{key} must be a list of strings")
            value = [s.lower() for s in value]
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"catalog.{key} must be a boolean")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"catalog.{key} must be a positive integer")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ConfigError(f"catalog.{key} must be a number in (0, 1]")
            value = float(value)
        values[key] = value
    if set(values["variant_keywords"]) - {"shiny", "prismana"}:
        raise ConfigError("catalog.variant_keywords may only define 'shiny' and 'prismana'")
    return CatalogRules(**values)


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[_\-.\s/]+")


def tokens(name: str | None) -> list[str]:
    if not name:
        return []
    return [t.lower() for t in _SPLIT.split(_CAMEL.sub("_", name)) if t]


def match_keyword(name: str | None, table: dict[str, list[str]]) -> tuple[str, str] | None:
    """Return (key, keyword) for the first table entry whose keyword is a token of ``name``."""
    toks = tokens(name)
    for key, words in table.items():
        for word in words:
            if word in toks:
                return key, word
    return None
