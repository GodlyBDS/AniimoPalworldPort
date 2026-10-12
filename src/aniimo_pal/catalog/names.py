"""User-supplied names for game IDs: creature names and per-creature form names.

The game files only contain internal names and numbers (``P_Parmon_10184``,
``NG_P_Parmon_1018403``). This file maps them to the names the player sees. It
is written by the user (or the Studio's *Set names* dialog) and stored in the
workspace, never shipped with the toolkit: every name in it has provenance
``user``.

```toml
[names]
"10184" = "Tromber"

[forms."10184"]
"01" = "Beach Form"
"03" = "Highland Form"
```
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ConfigError


def load_names(path: str | Path | None) -> dict[str, Any]:
    empty: dict[str, Any] = {"names": {}, "forms": {}}
    if path is None or not Path(path).is_file():
        return empty
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    names = {str(k): str(v).strip() for k, v in (data.get("names") or {}).items() if str(v).strip()}
    forms = {str(k): {str(i): str(n).strip() for i, n in (v or {}).items() if str(n).strip()}
             for k, v in (data.get("forms") or {}).items() if isinstance(v, dict)}
    return {"names": names, "forms": {k: v for k, v in forms.items() if v}}


def save_names(path: str | Path, game_id: str, name: str | None = None,
               forms: dict[str, str] | None = None) -> dict[str, Any]:
    """Set (or clear, with an empty string) the name of ``game_id`` and its form names; returns the new data."""
    data = load_names(path)
    if name is not None:
        if name.strip():
            data["names"][game_id] = name.strip()
        else:
            data["names"].pop(game_id, None)
    if forms is not None:
        cleaned = {i: n.strip() for i, n in forms.items() if n.strip()}
        if cleaned:
            data["forms"][game_id] = cleaned
        else:
            data["forms"].pop(game_id, None)
    lines = ["# Names you gave to game IDs (shown by the Studio; provenance: user).", "", "[names]"]
    lines += [f"{json.dumps(k)} = {json.dumps(v, ensure_ascii=True)}" for k, v in sorted(data["names"].items())]
    for gid, table in sorted(data["forms"].items()):
        lines += ["", f"[forms.{json.dumps(gid)}]"]
        lines += [f"{json.dumps(i)} = {json.dumps(n, ensure_ascii=True)}" for i, n in sorted(table.items())]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(p)
    return data


def apply_names(catalog: dict[str, Any], names: dict[str, Any]) -> int:
    """Use the user's names in a catalog (display names and form names). Returns the number of species named."""
    named = 0
    for sp in catalog.get("species", []):
        gid = sp.get("game_id")
        sp.setdefault("internal_name", sp["display_name"])
        if gid and gid in names["names"]:
            sp["display_name"] = names["names"][gid]
            sp["display_name_provenance"] = "user"
            named += 1
        else:
            sp["display_name"] = sp["internal_name"]
            sp["display_name_provenance"] = "inferred:internal-name"
        form_names = names["forms"].get(gid or "", {})
        for p in sp.get("prefabs", []):
            idx = p.get("game_form_index")
            p["game_form_name"] = form_names.get(idx) if idx else None
    return named
