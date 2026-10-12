"""Compare two manifests (e.g. before/after a game update)."""

from __future__ import annotations

from typing import Any

from aniimo_pal.errors import ManifestError


def _common_algorithm(old: dict[str, Any], new: dict[str, Any]) -> str:
    a = old["settings"]["hash_algorithms"]
    b = set(new["settings"]["hash_algorithms"])
    for algo in a:
        if algo in b:
            return algo
    raise ManifestError("manifests share no hash algorithm; cannot compare contents")


def diff_manifests(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return added / removed / modified / moved / kind_changed file lists.

    A removed+added pair with identical content hash is reported as ``moved``.
    """
    algo = _common_algorithm(old, new)
    old_files = {f["path"]: f for f in old["files"]}
    new_files = {f["path"]: f for f in new["files"]}

    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    modified, kind_changed = [], []
    for path in sorted(set(old_files) & set(new_files)):
        o, n = old_files[path], new_files[path]
        if o.get("hashes", {}).get(algo) != n.get("hashes", {}).get(algo) or o.get("size") != n.get("size"):
            modified.append({"path": path, "old_size": o.get("size"), "new_size": n.get("size")})
        if o.get("kind") != n.get("kind"):
            kind_changed.append({"path": path, "old_kind": o.get("kind"), "new_kind": n.get("kind")})

    removed_by_hash: dict[str, list[str]] = {}
    for p in removed:
        digest = old_files[p].get("hashes", {}).get(algo)
        if digest:
            removed_by_hash.setdefault(digest, []).append(p)
    moved = []
    for p in list(added):
        digest = new_files[p].get("hashes", {}).get(algo)
        candidates = removed_by_hash.get(digest or "")
        if candidates:
            src = candidates.pop(0)
            moved.append({"from": src, "to": p})
            added.remove(p)
            removed.remove(src)

    return {
        "hash_algorithm": algo,
        "added": added,
        "removed": removed,
        "modified": modified,
        "moved": moved,
        "kind_changed": kind_changed,
        "unchanged_count": len(set(old_files) & set(new_files)) - len(modified),
        "identical": not (added or removed or modified or moved),
    }
