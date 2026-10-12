"""Portable folder layout and the environment used for every child process."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR_NAME = "studio-data"


@dataclass(frozen=True)
class StudioLayout:
    """All Studio paths, derived from one root folder (where the launcher sits)."""

    root: Path

    @classmethod
    def for_root(cls, root: str | Path) -> "StudioLayout":
        return cls(Path(root).resolve())

    # --- directories -------------------------------------------------------
    @property
    def data(self) -> Path:
        return self.root / DATA_DIR_NAME

    @property
    def venv(self) -> Path:
        return self.data / "python-env"

    @property
    def portable_python(self) -> Path:
        return self.data / "python"

    @property
    def tools(self) -> Path:
        return self.data / "tools"

    @property
    def downloads(self) -> Path:
        return self.data / "downloads"

    @property
    def workspace(self) -> Path:
        return self.data / "workspace"

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def home(self) -> Path:
        """Private home/app-data directory handed to child processes."""
        return self.data / "home"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.json"

    @property
    def toolkit_config(self) -> Path:
        return self.data / "toolkit-config.toml"

    def ensure(self) -> None:
        for d in (self.data, self.tools, self.downloads, self.workspace, self.tmp, self.cache, self.home,
                  self.logs, self.home / "AppData" / "Roaming", self.home / "AppData" / "Local"):
            d.mkdir(parents=True, exist_ok=True)

    # --- python environment ------------------------------------------------
    @property
    def venv_python(self) -> Path:
        if sys.platform == "win32":
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    # --- containment ---------------------------------------------------------
    def contains(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def child_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for every tool the Studio runs: temp, caches and user dirs point inside the folder."""
        env = dict(os.environ if base is None else base)
        tmp, home, cache = str(self.tmp), str(self.home), str(self.cache)
        cache_dir = self.cache
        env.update({
            "TMP": tmp, "TEMP": tmp, "TMPDIR": tmp,
            # Python / pip
            "PIP_CACHE_DIR": str(self.cache / "pip"), "PYTHONUSERBASE": str(self.home / "python-user"),
            "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONUTF8": "1",
            # Blender user directories
            "BLENDER_USER_RESOURCES": str(self.home / "blender"),
            "BLENDER_USER_CONFIG": str(self.home / "blender" / "config"),
            "BLENDER_USER_SCRIPTS": str(self.home / "blender" / "scripts"),
            "BLENDER_USER_DATAFILES": str(self.home / "blender" / "datafiles"),
            "BLENDER_USER_EXTENSIONS": str(self.home / "blender" / "extensions"),
            # .NET (AssetRipper)
            "DOTNET_BUNDLE_EXTRACT_BASE_DIR": str(cache_dir / "dotnet-bundle"),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_CLI_HOME": home,
            # per-user locations
            "XDG_CACHE_HOME": cache, "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
        })
        if sys.platform == "win32":
            env["APPDATA"] = str(self.home / "AppData" / "Roaming")
            env["LOCALAPPDATA"] = str(self.home / "AppData" / "Local")
        else:
            env["HOME"] = home
        return env

    # --- settings -------------------------------------------------------------
    def load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, self.settings_file)

    def free_space_gb(self) -> float:
        target = self.data if self.data.exists() else self.root
        return shutil.disk_usage(target).free / 1024 ** 3

    def write_toolkit_config(self, tools: dict[str, str]) -> Path:
        """Config passed to every toolkit command: logs and tool paths inside the folder."""
        def esc(s: str) -> str:
            return s.replace("\\", "/").replace('"', '\\"')

        text = (
            "# Written by the Studio. Paths point inside the Studio folder.\n"
            "[logging]\nlevel = \"INFO\"\n"
            f"file = \"{esc(str(self.logs / 'toolkit.jsonl'))}\"\n\n"
            "[tools]\n"
            f"assetripper = \"{esc(tools.get('assetripper', ''))}\"\n"
            f"blender = \"{esc(tools.get('blender', ''))}\"\n"
            "unreal_editor = \"\"\n"
        )
        self.data.mkdir(parents=True, exist_ok=True)
        self.toolkit_config.write_text(text, encoding="utf-8")
        return self.toolkit_config
