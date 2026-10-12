"""Downloads and safe archive extraction (standard library only)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

Progress = Callable[[int, int | None], None]
USER_AGENT = "AniimoPalworldStudio/1.0"


class DownloadError(Exception):
    pass


def fetch_text(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise DownloadError(f"cannot fetch {url}: {exc}") from exc


def fetch_json(url: str, timeout: float = 30):
    try:
        return json.loads(fetch_text(url, timeout))
    except json.JSONDecodeError as exc:
        raise DownloadError(f"{url} did not return JSON") from exc


def download(url: str, dest: Path, progress: Progress | None = None, timeout: float = 60,
             attempts: int = 5) -> Path:
    """Download to ``dest`` via a .part file, verifying the size and resuming interrupted transfers.

    Connections that end early are common for large files; each retry continues
    from the bytes already received (HTTP Range) when the server supports it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    total: int | None = None
    last_error: Exception | None = None
    for _attempt in range(attempts):
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resumed = have and resp.status == 206
                length = resp.headers.get("Content-Length")
                length_i = int(length) if length and length.isdigit() else None
                if not resumed:
                    have = 0
                    total = length_i
                elif length_i is not None:
                    total = have + length_i
                with open(part, "ab" if resumed else "wb") as fh:
                    done = have
                    while chunk := resp.read(1024 * 256):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except OSError as exc:
            last_error = exc
            continue
        size = part.stat().st_size
        if total is None or size == total:
            part.replace(dest)
            return dest
        last_error = DownloadError(f"incomplete download ({size} of {total} bytes)")
    part.unlink(missing_ok=True)
    raise DownloadError(f"download failed for {url} after {attempts} attempts: {last_error}")


def _safe_target(base: Path, name: str) -> Path:
    target = (base / name).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise DownloadError(f"archive entry escapes the target folder: {name!r}")
    return target


def _long_path(path: Path) -> str:
    text = str(Path(path).resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def extract(archive: Path, dest: Path) -> Path:
    """Extract .zip / .tar.* into ``dest``, refusing entries that would land outside it."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                _safe_target(dest, member.filename)
            # Windows: extended-length form, so deep archive entries (e.g. Blender's bundled Python test data)
            # do not hit the 260-character path limit.
            zf.extractall(_long_path(dest))
            # zipfile drops Unix permissions; restore the executable bits recorded in the archive.
            for member in zf.infolist():
                mode = (member.external_attr >> 16) & 0o777
                if mode & 0o111:
                    (dest / member.filename).chmod(mode)
    elif re.search(r"\.tar(\.(gz|xz|bz2))?$|\.tgz$", name):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                _safe_target(dest, member.name)
                if member.issym() or member.islnk():
                    _safe_target(dest, str(Path(member.name).parent / member.linkname))
            tf.extractall(dest, filter="data")
    else:
        raise DownloadError(f"unsupported archive type: {archive.name}")
    return dest


def find_file(base: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        for hit in sorted(base.rglob(name)):
            if hit.is_file():
                return hit
    return None


def remove_tree(path: Path, inside: Path) -> None:
    """Delete ``path`` only if it is inside ``inside`` (defensive)."""
    path, inside = path.resolve(), inside.resolve()
    if inside not in path.parents:
        raise DownloadError(f"refusing to delete {path} (outside {inside})")
    shutil.rmtree(path, ignore_errors=True)
