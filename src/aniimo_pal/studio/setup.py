"""First-run setup: what the Studio needs, how to check it, and how to install it into the folder.

Requirements (all installed under ``studio-data/``):

* Python environment: a virtual environment with the toolkit and its
  extraction/conversion packages (UnityPy, PyYAML, numpy, jsonschema).
* AssetRipper: downloaded from its official GitHub releases (latest), or
  from a release zip the user downloaded.
* Blender: the portable build from download.blender.org (zip on Windows,
  tar.xz on Linux). On macOS Blender ships as a .dmg, so the user installs it
  and selects the app.

Every check is read-only. Installers only write inside the Studio folder.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from aniimo_pal.studio import downloads
from aniimo_pal.studio.layout import StudioLayout

Log = Callable[[str], None]
Progress = Callable[[int, int | None], None]

ASSETRIPPER_RELEASES_API = "https://api.github.com/repos/AssetRipper/AssetRipper/releases/latest"
ASSETRIPPER_RELEASES_PAGE = "https://github.com/AssetRipper/AssetRipper/releases/latest"
BLENDER_SERIES = "5.2"  # LTS series the conversion stage was verified with (bpy 5.2.2)
BLENDER_INDEX = "https://download.blender.org/release/Blender{series}/"
PYTHON_PACKAGES_EXTRAS = "unitypy,convert,validate"
MIN_FREE_GB = 10.0

ASSETRIPPER_EXE = ("AssetRipper.GUI.Free.exe", "AssetRipper.GUI.Free")
BLENDER_EXE = ("blender.exe", "blender")


@dataclass
class CheckResult:
    key: str
    title: str
    ok: bool
    detail: str
    required: bool = True
    can_install: bool = True
    help_url: str | None = None
    location: str | None = None


@dataclass
class PlatformInfo:
    system: str = field(default_factory=lambda: {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"))
    arch: str = field(default_factory=lambda: "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64")


# --- Python environment ---------------------------------------------------------

def check_python_env(layout: StudioLayout) -> CheckResult:
    title = "Python environment (toolkit, UnityPy, PyYAML, numpy)"
    py = layout.venv_python
    if not py.is_file():
        return CheckResult("python_env", title, False, "not installed yet")
    probe = "import UnityPy, yaml, numpy, jsonschema, aniimo_pal; print(aniimo_pal.__version__, UnityPy.__version__)"
    try:
        out = subprocess.run([str(py), "-c", probe], capture_output=True, text=True, timeout=120,
                             env=layout.child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("python_env", title, False, f"cannot run environment: {exc}")
    if out.returncode != 0:
        return CheckResult("python_env", title, False, "incomplete (packages missing); click Install to repair")
    toolkit, unitypy = (out.stdout.split() + ["?", "?"])[:2]
    return CheckResult("python_env", title, True, f"toolkit {toolkit}, UnityPy {unitypy}", location=str(layout.venv))


def install_python_env(layout: StudioLayout, log: Log, python: str = sys.executable) -> None:
    layout.ensure()
    env = layout.child_env()
    if not layout.venv_python.is_file():
        log(f"Creating Python environment in {layout.venv} ...")
        _run([python, "-m", "venv", str(layout.venv)], env, log)
    log("Installing the toolkit and its packages (UnityPy, PyYAML, numpy, jsonschema) ...")
    _run([str(layout.venv_python), "-m", "pip", "install", "--upgrade", "pip"], env, log)
    _run([str(layout.venv_python), "-m", "pip", "install", "-e", f"{layout.root}[{PYTHON_PACKAGES_EXTRAS}]"],
         env, log)


def _run(cmd: list[str], env: dict[str, str], log: Log, cwd: Path | None = None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=cwd,
                            bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    if proc.wait() != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd[:4])} ...")


# --- AssetRipper -------------------------------------------------------------------

def check_assetripper(layout: StudioLayout) -> CheckResult:
    title = "AssetRipper (Unity asset exporter)"
    exe = downloads.find_file(layout.tools / "assetripper", ASSETRIPPER_EXE) if (layout.tools / "assetripper").exists() \
        else None
    if exe:
        return CheckResult("assetripper", title, True, "installed", location=str(exe),
                           help_url=ASSETRIPPER_RELEASES_PAGE)
    return CheckResult("assetripper", title, False, "not installed yet", help_url=ASSETRIPPER_RELEASES_PAGE)


def pick_assetripper_asset(release: dict, plat: PlatformInfo) -> dict:
    """Choose the release zip for this platform (names like AssetRipper_win_x64.zip)."""
    token = {"windows": "win", "macos": "mac", "linux": "linux"}[plat.system]
    candidates = []
    for asset in release.get("assets") or []:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".zip") and re.search(rf"(^|[_\-.]){token}([_\-.]|$)", name) and plat.arch in name:
            candidates.append(asset)
    if not candidates:
        names = [a.get("name") for a in release.get("assets") or []]
        raise downloads.DownloadError(f"no AssetRipper download for {plat.system}-{plat.arch} in {names}")
    return sorted(candidates, key=lambda a: len(a["name"]))[0]


def install_assetripper(layout: StudioLayout, log: Log, progress: Progress | None = None,
                        zip_path: Path | None = None, plat: PlatformInfo | None = None) -> Path:
    """Install AssetRipper into studio-data/tools/assetripper (from GitHub, or from a local release zip)."""
    layout.ensure()
    plat = plat or PlatformInfo()
    if zip_path is None:
        log("Looking up the latest AssetRipper release on GitHub ...")
        release = downloads.fetch_json(ASSETRIPPER_RELEASES_API)
        asset = pick_assetripper_asset(release, plat)
        log(f"Downloading {asset['name']} ({release.get('tag_name', '?')}) ...")
        zip_path = downloads.download(asset["browser_download_url"], layout.downloads / asset["name"], progress)
    target = layout.tools / "assetripper"
    if target.exists():
        downloads.remove_tree(target, layout.data)
    log(f"Extracting into {target} ...")
    downloads.extract(Path(zip_path), target)
    exe = downloads.find_file(target, ASSETRIPPER_EXE)
    if exe is None:
        raise downloads.DownloadError("the archive does not contain AssetRipper.GUI.Free")
    if plat.system != "windows":
        exe.chmod(exe.stat().st_mode | 0o111)
    log(f"AssetRipper ready: {exe}")
    return exe


# --- Blender ---------------------------------------------------------------------------

def check_blender(layout: StudioLayout, settings: dict | None = None) -> CheckResult:
    title = f"Blender {BLENDER_SERIES} (model + animation conversion)"
    base = layout.tools / "blender"
    exe = downloads.find_file(base, BLENDER_EXE) if base.exists() else None
    if exe is None and settings and settings.get("blender_external"):
        p = Path(settings["blender_external"])
        if p.is_file():
            return CheckResult("blender", title, True, "using the Blender you selected (outside the folder)",
                               location=str(p), help_url="https://www.blender.org/download/")
    if exe:
        return CheckResult("blender", title, True, "installed", location=str(exe),
                           help_url="https://www.blender.org/download/")
    if PlatformInfo().system == "macos":
        return CheckResult("blender", title, False, "install Blender from blender.org, then click 'Select...'",
                           can_install=False, help_url="https://www.blender.org/download/")
    return CheckResult("blender", title, False, "not installed yet", help_url="https://www.blender.org/download/")


def pick_blender_build(index_html: str, plat: PlatformInfo, series: str = BLENDER_SERIES) -> str:
    """Newest portable build file name for this platform from a download.blender.org index page."""
    ext = {"windows": r"zip", "linux": r"tar\.xz"}.get(plat.system)
    if ext is None:
        raise downloads.DownloadError("automatic Blender install is not available on macOS")
    plat_name = {"windows": "windows", "linux": "linux"}[plat.system]
    pattern = re.compile(rf"blender-{re.escape(series)}\.(\d+)-{plat_name}-{plat.arch}\.{ext}")
    found = {(int(m.group(1)), m.group(0)) for m in pattern.finditer(index_html)}
    if not found:
        raise downloads.DownloadError(f"no Blender {series} build for {plat_name}-{plat.arch} found")
    return max(found)[1]


def install_blender(layout: StudioLayout, log: Log, progress: Progress | None = None,
                    plat: PlatformInfo | None = None, series: str = BLENDER_SERIES) -> Path:
    layout.ensure()
    plat = plat or PlatformInfo()
    index_url = BLENDER_INDEX.format(series=series)
    log(f"Looking up Blender {series} builds on download.blender.org ...")
    name = pick_blender_build(downloads.fetch_text(index_url), plat, series)
    log(f"Downloading {name} (about 300-400 MB) ...")
    archive = downloads.download(index_url + name, layout.downloads / name, progress)
    target = layout.tools / "blender"
    if target.exists():
        downloads.remove_tree(target, layout.data)
    log(f"Extracting into {target} (this can take a few minutes) ...")
    downloads.extract(archive, target)
    exe = downloads.find_file(target, BLENDER_EXE)
    if exe is None:
        raise downloads.DownloadError("the archive does not contain a Blender executable")
    log(f"Blender ready: {exe}")
    archive.unlink(missing_ok=True)  # the extracted copy is all we need
    return exe


# --- disk space / overview ---------------------------------------------------------------

def check_disk(layout: StudioLayout) -> CheckResult:
    free = layout.free_space_gb()
    ok = free >= MIN_FREE_GB
    return CheckResult("disk", "Free disk space in this folder's drive", ok,
                       f"{free:.1f} GB free" + ("" if ok else f" (at least {MIN_FREE_GB:.0f} GB recommended)"),
                       required=False, can_install=False)


def run_checks(layout: StudioLayout, settings: dict | None = None) -> list[CheckResult]:
    py_ok = sys.version_info >= (3, 11)
    results = [CheckResult("python", "Python 3.11+ for the Studio", py_ok,
                           f"Python {platform.python_version()}" + ("" if py_ok else " (too old)"),
                           can_install=False, help_url="https://www.python.org/downloads/")]
    results.append(check_python_env(layout))
    results.append(check_assetripper(layout))
    results.append(check_blender(layout, settings))
    results.append(check_disk(layout))
    return results


def tool_paths(layout: StudioLayout, settings: dict | None = None) -> dict[str, str]:
    ar = check_assetripper(layout)
    bl = check_blender(layout, settings)
    return {"assetripper": ar.location or "", "blender": bl.location or ""}
