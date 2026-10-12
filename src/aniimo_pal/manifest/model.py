"""Manifest constants and summary computation.

The manifest is plain JSON (see ``schemas/manifest.schema.json`` and
docs/manifest.md). Entries are dictionaries rather than classes so that the
on-disk format *is* the in-memory format and stays easy to consume from other
tools (Blender scripts, Unreal Python, spreadsheets).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

SCHEMA_ID = "aniimo-pal/manifest"
SCHEMA_VERSION = "1.0.0"


def summarize(entries: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind = Counter(e["kind"] for e in entries if e.get("kind"))
    by_category = Counter(e["category"] for e in entries if e.get("category"))
    bytes_by_category: Counter[str] = Counter()
    for e in entries:
        if e.get("category"):
            bytes_by_category[e["category"]] += e.get("size") or 0
    unity_versions = Counter(
        e["details"]["unity_version"]
        for e in entries
        if e.get("details", {}).get("unity_version")
    )
    return {
        "file_count": len(entries),
        "total_bytes": sum(e.get("size") or 0 for e in entries),
        "skipped_count": len(skipped),
        "error_count": sum(1 for e in entries if e.get("error")),
        "warning_count": sum(len(e.get("warnings") or []) for e in entries),
        "supported_count": sum(1 for e in entries if e.get("supported")),
        "encrypted_count": sum(1 for e in entries if e.get("details", {}).get("encryption_flag") is True),
        "by_kind": dict(sorted(by_kind.items())),
        "by_category": dict(sorted(by_category.items())),
        "bytes_by_category": dict(sorted(bytes_by_category.items())),
        "unity_versions": dict(sorted(unity_versions.items())),
    }
