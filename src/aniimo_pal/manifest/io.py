"""Manifest reading, writing and validation."""

from __future__ import annotations

import json
import os
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ManifestError, UnsafeOutputError
from aniimo_pal.manifest.model import SCHEMA_ID, summarize


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_output_location(out_path: Path, read_only_roots: list[Path]) -> None:
    """Refuse to write inside any tree the toolkit promised to treat as read-only."""
    for root in read_only_roots:
        if is_within(out_path, root):
            raise UnsafeOutputError(
                f"refusing to write {out_path} inside read-only input tree {root.resolve()}; "
                "choose an output path outside the scanned directory"
            )


def write_manifest(manifest: dict[str, Any], out_path: str | Path, *, indent: int = 2,
                   read_only_roots: list[Path] | None = None) -> Path:
    """Atomically write ``manifest``, refusing locations inside ``read_only_roots``."""
    out = Path(out_path)
    check_output_location(out, read_only_roots or [])
    return write_json_atomic(manifest, out, indent=indent)


def write_json_atomic(data: Any, out_path: str | Path, *, indent: int = 2) -> Path:
    """Write ``data`` as UTF-8 JSON via temp file + rename (no partial files)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=indent or None, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def read_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_ID:
        raise ManifestError(f"{p} is not an {SCHEMA_ID} manifest")
    return data


def load_schema() -> dict[str, Any]:
    text = resources.files("aniimo_pal").joinpath("schemas/manifest.schema.json").read_text("utf-8")
    return json.loads(text)


def _semantic_errors(manifest: dict[str, Any]) -> list[str]:
    """Checks JSON Schema cannot express: uniqueness, ordering, summary consistency."""
    errors: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, list):
        return ["files: must be a list"]
    paths = [f.get("path") for f in files if isinstance(f, dict)]
    if len(paths) != len(set(paths)):
        errors.append("files: duplicate paths")
    if paths != sorted(paths, key=str):
        errors.append("files: entries are not sorted by path")
    for p in paths:
        if isinstance(p, str) and (p.startswith("/") or ".." in p.split("/") or "\\" in p):
            errors.append(f"files: path {p!r} is not a clean relative POSIX path")
    algos = manifest.get("settings", {}).get("hash_algorithms", [])
    for f in files:
        if isinstance(f, dict) and not f.get("error") and set(f.get("hashes", {})) != set(algos):
            errors.append(f"files: {f.get('path')!r} hashes do not match settings.hash_algorithms")
    try:
        expected = summarize(files, manifest.get("skipped", []))
    except (KeyError, TypeError, AttributeError) as exc:
        errors.append(f"summary: cannot recompute ({exc})")
    else:
        summary = manifest.get("summary", {})
        for key, value in expected.items():
            if key in summary and summary[key] != value:
                errors.append(f"summary.{key}: {summary[key]!r} != recomputed {value!r}")
    return errors


def validate_manifest(manifest: dict[str, Any]) -> tuple[list[str], bool]:
    """Validate a manifest. Returns (errors, used_jsonschema).

    Full JSON Schema validation runs when the optional ``jsonschema`` package is
    installed; semantic checks always run.
    """
    errors: list[str] = []
    used_jsonschema = False
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        for key in ("schema", "schema_version", "generator", "source", "settings", "summary", "files", "skipped"):
            if key not in manifest:
                errors.append(f"missing top-level key {key!r}")
    else:
        used_jsonschema = True
        validator = jsonschema.Draft202012Validator(load_schema())
        for err in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(f"{loc}: {err.message}")
    if not errors:
        errors.extend(_semantic_errors(manifest))
    return errors, used_jsonschema
