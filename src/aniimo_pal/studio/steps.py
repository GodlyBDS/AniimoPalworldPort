"""Pipeline steps the Studio runs, as toolkit CLI commands inside the folder's Python environment."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aniimo_pal.studio.layout import StudioLayout

Log = Callable[[str], None]


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

    def __init__(self, layout: StudioLayout, game_folder: str, tools: dict[str, str]):
        self.layout = layout
        self.ws = Workspace(layout)
        self.game = str(game_folder)
        self.tools = tools

    def base(self) -> list[str]:
        return [str(self.layout.venv_python), "-m", "aniimo_pal", "--config", str(self.layout.toolkit_config)]

    # --- command builders (pure; unit-tested) --------------------------------------
    def cmd_scan(self) -> list[str]:
        return self.base() + ["scan", self.game, "-o", str(self.ws.manifest), "--label", "Aniimo"]

    def cmd_inventory(self) -> list[str]:
        return self.base() + ["inventory", str(self.ws.manifest), self.game, "-o", str(self.ws.objects)]

    def cmd_catalog(self) -> list[str]:
        return self.base() + ["catalog", "build", str(self.ws.objects), "-o", str(self.ws.catalog)]

    def cmd_create(self, species_key: str) -> list[str]:
        return self.base() + ["creature", "init", str(self.ws.root), "--from-catalog", str(self.ws.catalog),
                              "--species", species_key]

    def cmd_extract(self, creature_file: Path) -> list[str]:
        return self.base() + ["creature", "extract", str(creature_file), "--root", self.game]

    def cmd_assetripper(self, creature_file: Path, mode: str) -> list[str]:
        return self.base() + ["creature", "export-assetripper", str(creature_file), "--root", self.game,
                              "--mode", mode, "--assetripper", self.tools.get("assetripper", ""), "--replace"]

    def cmd_convert(self, creature_file: Path) -> list[str]:
        return self.base() + ["creature", "convert", str(creature_file), "--blender", self.tools.get("blender", "")]

    def cmd_preview(self, creature_file: Path) -> list[str]:
        return self.base() + ["creature", "preview", str(creature_file), "--blender", self.tools.get("blender", "")]

    # --- execution -------------------------------------------------------------------------
    def run(self, cmd: list[str], log: Log) -> int:
        self.layout.ensure()
        self.ws.root.mkdir(parents=True, exist_ok=True)
        log("$ aniimo-pal " + " ".join(cmd[5:]))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=self.layout.child_env(), cwd=self.ws.root, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            log(line.rstrip())
        code = proc.wait()
        log(f"(exit code {code})")
        return code

    def run_all(self, cmds: list[list[str]], log: Log, allow_check_failure: bool = False) -> bool:
        for cmd in cmds:
            code = self.run(cmd, log)
            if code == 2 or (code == 1 and not allow_check_failure):
                return False
        return True


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
