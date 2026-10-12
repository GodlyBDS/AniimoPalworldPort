"""Versioned game-system definitions (vocabularies), e.g. Aniimo and Palworld.

A system definition lists the ids a creature project may use: elements (with
aliases), stats, work categories and the work-level range. It contains **no
conversion formulas**. Translation rules are a later milestone and need
verified data from both games first (docs/architecture.md §10).

Definitions are TOML files with a ``[system]`` header carrying ``id``,
``version`` and ``status`` (``unverified`` | ``verified``). Bundled defaults live
in ``aniimo_pal/data/systems/``. A user can point at an updated copy once values
are confirmed from game data. Creature projects record the system id+version
they were validated against, so rules can change without invalidating
earlier imports.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ConfigError

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
STATUSES = ("unverified", "verified")


@dataclass(frozen=True)
class GameSystem:
    id: str
    version: str
    status: str
    game_version: str
    max_elements: int
    elements: dict[str, dict[str, Any]]
    stats: dict[str, dict[str, Any]]
    work_categories: tuple[str, ...]
    work_level_min: int
    work_level_max: int
    sources: tuple[str, ...] = field(default=())
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def canonical_element(self, name: str) -> str | None:
        key = name.strip().lower()
        if key in self.elements:
            return key
        return self.aliases.get(f"element:{key}")

    def ref(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version}


def _table(data: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: [{key}] must be a table")
    return value


def parse_system(data: dict[str, Any], where: str = "system definition") -> GameSystem:
    head = _table(data, "system", where)
    sid, version, status = head.get("id"), head.get("version"), head.get("status", "unverified")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ConfigError(f"{where}: system.id must be a lowercase identifier")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ConfigError(f"{where}: system.version must look like 1.2.3")
    if status not in STATUSES:
        raise ConfigError(f"{where}: system.status must be one of {STATUSES}")
    max_elements = head.get("max_elements", 1)
    if isinstance(max_elements, bool) or not isinstance(max_elements, int) or max_elements < 1:
        raise ConfigError(f"{where}: system.max_elements must be a positive integer")

    aliases: dict[str, str] = {}
    vocab: dict[str, dict[str, dict[str, Any]]] = {}
    for section in ("elements", "stats"):
        entries = _table(data, section, where)
        for key, spec in entries.items():
            if not ID_RE.match(key) or not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
                raise ConfigError(f"{where}: {section}.{key} needs a lowercase id and a name")
            for alias in spec.get("aliases", []):
                if not isinstance(alias, str) or not ID_RE.match(alias):
                    raise ConfigError(f"{where}: {section}.{key} has an invalid alias {alias!r}")
                prefixed = f"{section[:-1] if section == 'elements' else 'stat'}:{alias}"
                if alias in entries or prefixed in aliases:
                    raise ConfigError(f"{where}: alias {alias!r} in [{section}] is ambiguous")
                aliases[prefixed] = key
        vocab[section] = entries

    work = _table(data, "work", where)
    categories = work.get("categories", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) and ID_RE.match(c) for c in categories):
        raise ConfigError(f"{where}: work.categories must be a list of lowercase ids")
    if len(set(categories)) != len(categories):
        raise ConfigError(f"{where}: work.categories contains duplicates")
    lo, hi = work.get("level_min", 1), work.get("level_max", 1)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (lo, hi)) or not 0 <= lo <= hi:
        raise ConfigError(f"{where}: work.level_min/level_max must be integers with 0 <= min <= max")
    sources = head.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ConfigError(f"{where}: system.sources must be a list of strings")
    return GameSystem(sid, version, status, str(head.get("game_version", "unknown")), max_elements,
                      vocab["elements"], vocab["stats"], tuple(categories), lo, hi, tuple(sources), aliases)


def load_system(path: str | Path) -> GameSystem:
    try:
        with open(path, "rb") as fh:
            return parse_system(tomllib.load(fh), str(path))
    except FileNotFoundError as exc:
        raise ConfigError(f"system definition not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def bundled_system(system_id: str) -> GameSystem:
    """Load a definition shipped with the package (``aniimo`` or ``palworld``)."""
    res = resources.files("aniimo_pal").joinpath(f"data/systems/{system_id}.toml")
    if not res.is_file():
        raise ConfigError(f"no bundled system definition {system_id!r}")
    return parse_system(tomllib.loads(res.read_text("utf-8")), f"bundled {system_id}.toml")
