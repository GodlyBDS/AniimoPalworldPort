"""Object inventory: list every object inside the Unity containers of a manifest.

Input: a scan manifest + the scanned root. Output: an ``aniimo-pal/objects``
document (``objects.json``) with one record per Unity object and its outgoing
references. The catalog and association engine work only from this document,
so they never need to touch game files again.

Only manifest entries that are ``supported`` (which excludes encryption-flagged
bundles) and of kind ``unity_bundle``/``unity_serialized`` are opened. Each
file's hash is re-checked against the manifest first, so the object index is
guaranteed to describe exactly the files the manifest describes.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter, unitypy_version
from aniimo_pal.errors import ToolkitError
from aniimo_pal.log import get_logger

log = get_logger("inventory")

SCHEMA_ID = "aniimo-pal/objects"
SCHEMA_VERSION = "0.1.0"
INVENTORY_KINDS = ("unity_bundle", "unity_serialized")


def _file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def build_inventory(manifest: dict[str, Any], root: str | Path, *, include: Iterable[str] = (),
                    verify_hashes: bool = True,
                    progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    root_path = Path(root).resolve()
    algo = manifest["settings"]["hash_algorithms"][0]
    adapter = UnityPyAdapter()
    patterns = tuple(include)

    candidates, skipped = [], []
    for entry in manifest["files"]:
        if entry.get("kind") not in INVENTORY_KINDS:
            continue
        if patterns and not any(fnmatch.fnmatchcase(entry["path"], p) for p in patterns):
            continue
        if not entry.get("supported"):
            reason = "encryption flag set" if entry.get("details", {}).get("encryption_flag") else "not supported"
            skipped.append({"path": entry["path"], "reason": reason})
            continue
        candidates.append(entry)

    containers, objects = [], []
    for i, entry in enumerate(candidates, 1):
        rel = entry["path"]
        path = root_path / rel
        record: dict[str, Any] = {"path": rel, "sha256": entry["hashes"].get("sha256"),
                                  "hash": {algo: entry["hashes"].get(algo)}, "kind": entry["kind"],
                                  "status": "ok", "error": None, "serialized_files": []}
        try:
            if verify_hashes and _file_hash(path, algo) != entry["hashes"].get(algo):
                raise ToolkitError("file changed since the manifest was written; re-scan first")
            for inv in adapter.inventory_file(path):
                record["serialized_files"].append({"cab": inv.cab, "unity_version": inv.unity_version,
                                                   "externals": inv.externals, "object_count": len(inv.objects)})
                for obj in inv.objects:
                    obj["container"] = rel
                    objects.append(obj)
        except Exception as exc:  # keep going: one bad container must not stop the inventory
            log.warning("inventory failed for %s: %s", rel, exc)
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        containers.append(record)
        if progress:
            progress(i, len(candidates), rel)

    cab_owner: dict[str, str] = {}
    duplicate_cabs = set()
    for c in containers:
        for sf in c["serialized_files"]:
            if sf["cab"] in cab_owner and cab_owner[sf["cab"]] != c["path"]:
                duplicate_cabs.add(sf["cab"])
            cab_owner.setdefault(sf["cab"], c["path"])

    known_ids = {o["id"] for o in objects}
    unresolved = sum(1 for o in objects for r in o["refs"] if r["target"] not in known_ids)
    objects.sort(key=lambda o: (o["cab"], o["path_id"]))
    by_class: dict[str, int] = defaultdict(int)
    for o in objects:
        by_class[o["class"]] += 1

    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": {"name": "UnityPy", "version": unitypy_version()},
        "manifest": {"root_label": manifest["source"]["root_label"], "created_utc": manifest.get("created_utc")},
        "summary": {
            "container_count": len(containers),
            "container_errors": sum(1 for c in containers if c["status"] != "ok"),
            "skipped_count": len(skipped),
            "object_count": len(objects),
            "object_errors": sum(1 for o in objects if o.get("error")),
            "unresolved_refs": unresolved,
            "duplicate_cabs": sorted(duplicate_cabs),
            "by_class": dict(sorted(by_class.items())),
        },
        "containers": containers,
        "skipped": skipped,
        "objects": objects,
    }


class ObjectIndex:
    """Lookup helpers over an ``aniimo-pal/objects`` document."""

    def __init__(self, doc: dict[str, Any]):
        if doc.get("schema") != SCHEMA_ID:
            raise ToolkitError(f"not an {SCHEMA_ID} document")
        self.doc = doc
        self.by_id: dict[str, dict[str, Any]] = {o["id"]: o for o in doc["objects"]}
        self.by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.referrers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for o in doc["objects"]:
            self.by_class[o["class"]].append(o)
            for r in o["refs"]:
                self.referrers[r["target"]].append((o["id"], r["field"]))
        self.container_hash = {c["path"]: c.get("sha256") for c in doc["containers"]}

    def get(self, object_id: str | None) -> dict[str, Any] | None:
        return self.by_id.get(object_id) if object_id else None

    def refs(self, obj: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
        """Resolved targets of references whose field path starts with ``prefix``."""
        out = []
        for r in obj["refs"]:
            if r["field"] == prefix or r["field"].startswith(prefix + "[") or r["field"].startswith(prefix + "."):
                target = self.by_id.get(r["target"])
                if target is not None:
                    out.append(target)
        return out

    def ref(self, obj: dict[str, Any], field: str) -> dict[str, Any] | None:
        found = self.refs(obj, field)
        return found[0] if found else None
