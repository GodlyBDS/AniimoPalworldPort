"""Read-only directory scanner that produces a manifest.

Guarantees:
* Files are only ever opened with mode ``"rb"``; nothing under the scan root is
  created, modified, renamed or deleted (atime aside, which the OS controls).
* Symlinks are not followed unless configured, which also prevents escaping the
  scan root.
* Each file is read exactly once: the first ``probe_bytes`` feed format
  detection and entropy, and every chunk feeds the hashers.
* Per-file I/O errors are recorded in the manifest; the scan continues.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import platform
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.config import ScanConfig
from aniimo_pal.log import get_logger
from aniimo_pal.manifest.model import SCHEMA_ID, SCHEMA_VERSION, summarize
from aniimo_pal.scanner.formats import FORMATS, detect, shannon_entropy

log = get_logger("scanner")

CHUNK_SIZE = 1024 * 1024
SUPPORTED_ROUTES_PREFIXES = ("adapter:", "stage:")


@dataclass
class WalkResult:
    files: list[tuple[str, Path]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _excluded(rel: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatchcase(rel, pattern):
            return pattern
    return None


def walk(root: Path, cfg: ScanConfig, ignore: set[Path] | None = None) -> WalkResult:
    """Collect regular files under ``root`` in deterministic (sorted) order."""
    result = WalkResult()
    ignore = {p.resolve() for p in (ignore or set())}
    visited_dirs: set[tuple[int, int]] = set()

    def on_error(err: OSError) -> None:
        rel = _rel(Path(err.filename), root) if err.filename else "?"
        result.skipped.append({"path": rel, "reason": f"unreadable directory: {err.strerror}"})

    for dirpath, dirnames, filenames in os.walk(root, followlinks=cfg.follow_symlinks, onerror=on_error):
        current = Path(dirpath)
        if cfg.follow_symlinks:
            # Guard against symlink loops when following links.
            st = current.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited_dirs:
                dirnames[:] = []
                result.skipped.append({"path": _rel(current, root), "reason": "symlink loop"})
                continue
            visited_dirs.add(key)

        kept_dirs = []
        for d in sorted(dirnames):
            full = current / d
            rel = _rel(full, root)
            if not cfg.include_hidden and d.startswith("."):
                result.skipped.append({"path": rel + "/", "reason": "hidden"})
            elif full.is_symlink() and not cfg.follow_symlinks:
                result.skipped.append({"path": rel + "/", "reason": "symlink (not followed)"})
            elif (pattern := _excluded(rel + "/", cfg.exclude)) or (pattern := _excluded(rel, cfg.exclude)):
                result.skipped.append({"path": rel + "/", "reason": f"excluded by {pattern!r}"})
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            full = current / name
            rel = _rel(full, root)
            if not cfg.include_hidden and name.startswith("."):
                result.skipped.append({"path": rel, "reason": "hidden"})
                continue
            if (pattern := _excluded(rel, cfg.exclude)):
                result.skipped.append({"path": rel, "reason": f"excluded by {pattern!r}"})
                continue
            try:
                st = full.lstat()
            except OSError as exc:
                result.skipped.append({"path": rel, "reason": f"stat failed: {exc.strerror}"})
                continue
            if stat.S_ISLNK(st.st_mode):
                if not cfg.follow_symlinks:
                    result.skipped.append({"path": rel, "reason": "symlink (not followed)"})
                    continue
                try:
                    st = full.stat()
                except OSError:
                    result.skipped.append({"path": rel, "reason": "broken symlink"})
                    continue
            if not stat.S_ISREG(st.st_mode):
                result.skipped.append({"path": rel, "reason": "not a regular file"})
                continue
            if full.resolve() in ignore:
                result.skipped.append({"path": rel, "reason": "toolkit output file"})
                continue
            result.files.append((rel, full))
    result.files.sort(key=lambda item: item[0])
    return result


def _rel(path: Path, root: Path) -> str:
    try:
        return PurePosixPath(path.relative_to(root)).as_posix()
    except ValueError:
        return path.as_posix()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def scan_file(path: Path, rel: str, cfg: ScanConfig) -> dict[str, Any]:
    """Hash and classify one file. Never raises for I/O problems."""
    entry: dict[str, Any] = {"path": rel}
    try:
        st = path.stat()
        hashers = {name: hashlib.new(name) for name in cfg.hash_algorithms}
        head = b""
        total = 0
        with open(path, "rb") as fh:  # read-only by construction
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                if len(head) < cfg.probe_bytes:
                    head += chunk[: cfg.probe_bytes - len(head)]
                for h in hashers.values():
                    h.update(chunk)
                total += len(chunk)
    except OSError as exc:
        log.warning("cannot read %s: %s", rel, exc)
        entry.update(size=None, kind=None, category=None, supported=False,
                     confidence=None, hashes={}, details={}, warnings=[],
                     error=f"{type(exc).__name__}: {exc.strerror or exc}")
        return entry

    if total != st.st_size:
        log.warning("%s changed size while being read (%d -> %d bytes)", rel, st.st_size, total)
    det = detect(head, total, path.name, cfg.high_entropy_threshold)
    info = FORMATS[det.kind]
    warnings = list(det.warnings)
    if total != st.st_size:
        warnings.append(f"file size changed during scan ({st.st_size} -> {total} bytes)")
    entry.update(
        size=total,
        hashes={name: h.hexdigest() for name, h in hashers.items()},
        kind=det.kind,
        category=info.category,
        # Encryption-flagged bundles and bundles behind a prefix (custom container or obfuscation) are
        # reported only: prefixes are never stripped (docs/legal-and-safety.md).
        supported=bool(info.route and info.route.startswith(SUPPORTED_ROUTES_PREFIXES)
                       and det.details.get("encryption_flag") is not True
                       and not det.details.get("signature_offset")),
        route=info.route,
        confidence=det.confidence,
        entropy=round(shannon_entropy(head), 3),
        details=det.details,
        warnings=warnings,
        error=None,
    )
    if cfg.record_mtime:
        entry["mtime_utc"] = _iso(st.st_mtime)
    return entry


def link_relations(entries: list[dict[str, Any]]) -> None:
    """Annotate sidecar relationships (``.meta`` files, ``.resS`` companions)."""
    by_path = {e["path"]: e for e in entries}
    for e in entries:
        path = e["path"]
        lower = path.lower()
        relations = []
        if lower.endswith(".meta") and path[:-5] in by_path:
            relations.append({"type": "meta_for", "path": path[:-5]})
        for suffix in (".ress", ".resource"):
            if lower.endswith(suffix):
                owner = path[: -len(suffix)]
                if owner in by_path:
                    relations.append({"type": "resource_of", "path": owner})
                elif e.get("kind") == "unity_resource":
                    e["warnings"].append("no companion SerializedFile found next to this resource file")
        if relations:
            e["relations"] = relations


@dataclass
class ScanOptions:
    root_label: str | None = None
    store_absolute_root: bool = False
    ignore: set[Path] = field(default_factory=set)
    progress: Callable[[int, int, str], None] | None = None


def iter_scan(files: list[tuple[str, Path]], cfg: ScanConfig) -> Iterator[dict[str, Any]]:
    if cfg.workers == 1:
        for rel, full in files:
            yield scan_file(full, rel, cfg)
        return
    with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="scan") as pool:
        yield from pool.map(lambda item: scan_file(item[1], item[0], cfg), files)


def scan(root: str | Path, cfg: ScanConfig, options: ScanOptions | None = None) -> dict[str, Any]:
    """Scan ``root`` and return a manifest dictionary (see docs/manifest.md)."""
    options = options or ScanOptions()
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"scan root is not a directory: {root_path}")
    root_path = root_path.resolve()
    started = datetime.now(timezone.utc)
    log.info("scanning %s", root_path)

    walked = walk(root_path, cfg, options.ignore)
    log.info("found %d files (%d skipped)", len(walked.files), len(walked.skipped))
    entries = []
    for i, entry in enumerate(iter_scan(walked.files, cfg), 1):
        entries.append(entry)
        for w in entry.get("warnings", []):
            log.debug("%s: %s", entry["path"], w)
        if options.progress:
            options.progress(i, len(walked.files), entry["path"])
    link_relations(entries)
    finished = datetime.now(timezone.utc)

    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": started.isoformat(timespec="seconds"),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "source": {
            "root_label": options.root_label or root_path.name,
            "root_path": str(root_path) if options.store_absolute_root else None,
            "host_platform": platform.system(),
        },
        "settings": {
            "hash_algorithms": list(cfg.hash_algorithms),
            "follow_symlinks": cfg.follow_symlinks,
            "include_hidden": cfg.include_hidden,
            "exclude": list(cfg.exclude),
            "probe_bytes": cfg.probe_bytes,
            "high_entropy_threshold": cfg.high_entropy_threshold,
        },
        "summary": summarize(entries, walked.skipped),
        "files": entries,
        "skipped": walked.skipped,
    }
