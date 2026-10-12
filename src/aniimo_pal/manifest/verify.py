"""Re-hash files on disk and compare against a manifest (read-only)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from aniimo_pal.config import ScanConfig
from aniimo_pal.scanner.scanner import walk

CHUNK = 1024 * 1024


def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(manifest: dict[str, Any], root: str | Path,
                    check_extra: bool = True) -> dict[str, Any]:
    """Return ok / missing / changed / unreadable / extra path lists."""
    root_path = Path(root).resolve()
    algo = manifest["settings"]["hash_algorithms"][0]
    ok, missing, changed, unreadable = [], [], [], []
    for entry in manifest["files"]:
        if entry.get("error"):
            continue
        path = root_path / entry["path"]
        if not path.is_file():
            missing.append(entry["path"])
            continue
        try:
            digest = _hash_file(path, algo)
        except OSError as exc:
            unreadable.append({"path": entry["path"], "error": str(exc)})
            continue
        (ok if digest == entry["hashes"].get(algo) else changed).append(entry["path"])

    extra: list[str] = []
    if check_extra:
        s = manifest.get("settings", {})
        cfg = ScanConfig(
            follow_symlinks=s.get("follow_symlinks", False),
            include_hidden=s.get("include_hidden", False),
            exclude=tuple(s.get("exclude", ())),
        )
        known = {e["path"] for e in manifest["files"]}
        extra = [rel for rel, _ in walk(root_path, cfg).files if rel not in known]

    return {
        "hash_algorithm": algo,
        "ok": ok,
        "missing": missing,
        "changed": changed,
        "unreadable": unreadable,
        "extra": extra,
        "passed": not (missing or changed or unreadable or extra),
    }
