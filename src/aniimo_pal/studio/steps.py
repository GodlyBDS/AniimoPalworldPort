"""Pipeline steps the Studio runs, as toolkit CLI commands inside the folder's Python environment."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from aniimo_pal.studio.layout import StudioLayout

Log = Callable[[str], None]
Progress = Callable[[int, int, str], None]

# Progress lines printed by long CLI commands, e.g. "[120/2158] survey: x.bundle  (elapsed 1.0 min, ETA 9.2 min)".
PROGRESS_LINE = re.compile(r"^\[(\d+)/(\d+)\] (.*)$")


class StepStopped(Exception):
    """The user stopped the running step."""


class StepFailed(Exception):
    """A step exited with an error (its output, with the reason, is already in the log)."""


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a step and every process it started (worker pools, AssetRipper, Blender)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)  # the step runs in its own session/process group
        proc.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class Workspace:
    layout: StudioLayout

    @property
    def root(self) -> Path:
        return self.layout.workspace

    @property
    def manifest(self) -> Path:
        return self.root / "manifests" / "game.json"

    @property
    def objects(self) -> Path:
        return self.root / "objects.json"

    @property
    def catalog(self) -> Path:
        return self.root / "catalog.json"

    @property
    def names(self) -> Path:
        """Names the user gave to game IDs (creatures and their forms)."""
        return self.root / "creature_names.toml"

    @property
    def catalog_rules(self) -> Path:
        """Catalog rules chosen in the Studio (e.g. which name groups are creatures)."""
        return self.root / "catalog_rules.toml"

    def creature_file(self, creature_id: str) -> Path:
        return self.root / "creatures" / creature_id / "creature.json"

    def creatures(self) -> dict[str, Path]:
        base = self.root / "creatures"
        if not base.is_dir():
            return {}
        return {p.parent.name: p for p in sorted(base.glob("*/creature.json"))}


def validate_game_folder(layout: StudioLayout, folder: str | Path) -> tuple[bool, str]:
    """The game folder is only read. It must exist and must not overlap the Studio's own data folder."""
    p = Path(folder)
    if not str(folder).strip():
        return False, "No folder selected."
    if not p.is_dir():
        return False, "That folder does not exist."
    p = p.resolve()
    data = layout.data.resolve()
    if p == data or data in p.parents:
        return False, "Please select the Aniimo folder, not a folder inside the Studio's data."
    if p in data.parents:
        return False, ("This folder contains the Studio itself. Select the Aniimo install folder "
                       "(the Studio folder may be anywhere else).")
    hints = []
    try:
        for child in list(p.iterdir())[:500]:
            n = child.name.lower()
            if child.is_dir() and n.endswith("_data"):
                hints.append(child.name)
            elif n.endswith((".bundle", ".assets", ".unity3d", ".ab")):
                hints.append(child.name)
    except OSError as exc:
        return False, f"Cannot read the folder: {exc}"
    if hints:
        return True, f"Looks like a Unity game folder ({', '.join(hints[:3])})."
    return True, "Folder accepted. No obvious Unity data folder at the top level; the scan will search all sub-folders."


class StepRunner:
    """Builds and runs toolkit commands; every output path is inside the workspace."""

    def __init__(self, layout: StudioLayout, game_folder: str, tools: dict[str, str],
                 on_progress: Progress | None = None):
        self.layout = layout
        self.ws = Workspace(layout)
        self.game = str(game_folder)
        self.tools = tools
        self.on_progress = on_progress
        self.stopped = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def base(self) -> list[str]:
        return [str(self.layout.venv_python), "-m", "aniimo_pal", "--config", str(self.layout.toolkit_config)]

    # --- command builders (pure; unit-tested) --------------------------------------
    def cmd_scan(self) -> list[str]:
        return self.base() + ["scan", self.game, "-o", str(self.ws.manifest), "--label", "Aniimo"]

    def cmd_inventory(self) -> list[str]:
        return self.base() + ["inventory", str(self.ws.manifest), self.game, "-o", str(self.ws.objects)]

    def cmd_catalog(self) -> list[str]:
        rules = ["--rules", str(self.ws.catalog_rules)] if self.ws.catalog_rules.is_file() else []
        rules += ["--names", str(self.ws.names)] if self.ws.names.is_file() else []
        return self.base() + ["catalog", "build", str(self.ws.objects), "-o", str(self.ws.catalog)] + rules

    def cmd_create(self, species_key: str) -> list[str]:
        return self.base() + ["creature", "init", str(self.ws.root), "--from-catalog", str(self.ws.catalog),
                              "--species", species_key]

    def cmd_extract(self, creature_file: Path, include_proposed: bool = False) -> list[str]:
        return self.base() + ["creature", "extract", str(creature_file), "--root", self.game] + \
            (["--include-proposed"] if include_proposed else [])

    def cmd_assetripper(self, creature_file: Path, mode: str, include_proposed: bool = False) -> list[str]:
        return self.base() + ["creature", "export-assetripper", str(creature_file), "--root", self.game,
                              "--mode", mode, "--assetripper", self.tools.get("assetripper", ""), "--replace"] + \
            (["--include-proposed"] if include_proposed else [])

    def cmd_convert(self, creature_file: Path, include_proposed: bool = False) -> list[str]:
        return self.base() + ["creature", "convert", str(creature_file), "--blender", self.tools.get("blender", "")] + \
            (["--include-proposed"] if include_proposed else [])

    def cmd_preview(self, creature_file: Path) -> list[str]:
        return self.base() + ["creature", "preview", str(creature_file), "--blender", self.tools.get("blender", "")]

    # --- execution -------------------------------------------------------------------------
    def run(self, cmd: list[str], log: Log) -> int:
        """Run one step, streaming its output; raises StepStopped if the user stopped it."""
        self.layout.ensure()
        self.ws.root.mkdir(parents=True, exist_ok=True)
        log("$ aniimo-pal " + " ".join(cmd[5:]))
        extra: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else \
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        with self._lock:
            if self.stopped:
                raise StepStopped()
            proc = self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                 encoding="utf-8", errors="replace", env=self.layout.child_env(),
                                                 cwd=self.ws.root, bufsize=1, **extra)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            m = PROGRESS_LINE.match(line)
            if m and self.on_progress:
                self.on_progress(int(m.group(1)), int(m.group(2)), m.group(3))
            log(line)
        code = proc.wait()
        with self._lock:
            self._proc = None
        if self.stopped:
            log("(stopped by user)")
            raise StepStopped()
        log(f"(exit code {code})")
        return code

    def stop(self) -> bool:
        """Stop the running step (and its child processes). Returns False if nothing was running."""
        with self._lock:
            self.stopped = True
            proc = self._proc
        if proc is None:
            return False
        kill_tree(proc)
        return True

    def run_all(self, cmds: list[list[str]], log: Log, allow_check_failure: bool = False) -> bool:
        for cmd in cmds:
            code = self.run(cmd, log)
            if code == 2 or (code == 1 and not allow_check_failure):
                return False
        return True


# AssetRipper's model ("primary") export is no longer part of the pipeline: `extract` writes the mesh GLB itself.
CREATURE_STEPS = ("extract", "project", "convert", "preview")
STEP_LABELS = {"extract": "extract (models, textures)", "primary": "AssetRipper model export",
               "project": "AssetRipper animation export",
               "convert": "convert to FBX", "preview": "3D preview"}


def step_done(creature_dir: Path, step: str) -> bool:
    if step == "extract":
        return (creature_dir / "extract" / "unitypy" / "report.json").is_file()
    if step in ("primary", "project"):
        out = creature_dir / "extract" / "assetripper" / step
        return out.is_dir() and any(out.iterdir())
    if step == "convert":
        return (creature_dir / "convert" / "report.json").is_file()
    if step == "preview":
        return (creature_dir / "preview" / "preview.blend").is_file()
    raise ValueError(step)


def with_prerequisites(creature_dir: Path, which: tuple[str, ...]) -> tuple[str, ...]:
    """``which`` plus the earlier steps it needs that have not run yet, in pipeline order.

    The preview needs the converted FBX; conversion needs the extracted mesh and AssetRipper's animations.
    """
    needs = {"preview": ("convert",), "convert": ("extract", "project")}
    wanted = set(which)
    todo = list(which)
    while todo:
        for dep in needs.get(todo.pop(), ()):
            if dep not in wanted and not step_done(creature_dir, dep):
                wanted.add(dep)
                todo.append(dep)
    return tuple(s for s in CREATURE_STEPS if s in wanted)


def unknown_files_breakdown(manifest: dict[str, Any], top: int = 8) -> dict[str, list[tuple[str, int]]]:
    """Where the unrecognised high-entropy files are: by extension and by folder (two levels deep)."""
    exts: Counter[str] = Counter()
    folders: Counter[str] = Counter()
    for f in manifest.get("files", []):
        if f.get("kind") != "unknown_high_entropy":
            continue
        p = PurePosixPath(f["path"])
        exts[p.suffix.lower() or "(no extension)"] += 1
        folders["/".join(p.parts[:-1][:2]) or "(top level)"] += 1
    return {"extensions": exts.most_common(top), "folders": folders.most_common(top)}


def read_objects_summary(objects: Path, max_full_load: int = 50 * 1024 ** 2) -> dict | None:
    """The object list's summary without loading the (possibly 1 GB) object list itself.

    None: no object list. {}: an object list from an older version without a summary file.
    """
    from aniimo_pal.inventory import summary_path

    if not objects.is_file():
        return None
    side = read_json(summary_path(objects))
    if side is not None:
        return side
    if objects.stat().st_size <= max_full_load:
        return read_json(objects) or {}
    return {}


def is_older(path: Path, than: Path) -> bool:
    try:
        return path.stat().st_mtime < than.stat().st_mtime
    except OSError:
        return False


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
