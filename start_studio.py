#!/usr/bin/env python3
"""Aniimo -> Palworld Studio: the all-in-one launcher.

Double-click Start-Studio.bat (Windows) or run ./start-studio.sh (Linux/macOS),
which call this file. Everything the Studio installs or creates is kept inside
the folder that contains this file (in ``studio-data/``).

Options (no GUI):
  --check     print the setup status and exit
  --install   install everything that is missing (Python environment, AssetRipper, Blender), then exit
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}). "
             "Run Start-Studio.bat / start-studio.sh, which can download a portable Python into this folder.")

sys.path.insert(0, str(ROOT / "src"))
sys.pycache_prefix = str(ROOT / "studio-data" / "cache" / "pycache")  # keep bytecode caches in the folder

from aniimo_pal.studio import setup  # noqa: E402
from aniimo_pal.studio.layout import StudioLayout  # noqa: E402


def print_checks(layout: StudioLayout) -> bool:
    checks = setup.run_checks(layout, layout.load_settings())
    for c in checks:
        mark = "OK " if c.ok else ("MISSING" if c.required else "WARN")
        print(f"[{mark:^7}] {c.title}: {c.detail}" + (f"  ({c.location})" if c.location else ""))
    return all(c.ok for c in checks if c.required)


def install_all(layout: StudioLayout) -> int:
    settings = layout.load_settings()
    for c in setup.run_checks(layout, settings):
        if c.ok or not c.required:
            continue
        if not c.can_install:
            print(f"Manual step needed: {c.title}: {c.detail}")
            continue
        print(f"--- Installing {c.title} ---")
        try:
            if c.key == "python_env":
                setup.install_python_env(layout, print)
            elif c.key == "assetripper":
                setup.install_assetripper(layout, print)
            elif c.key == "blender":
                setup.install_blender(layout, print)
        except Exception as exc:  # keep going; the summary below shows what is still missing
            print(f"FAILED: {c.title}: {exc}")
    layout.write_toolkit_config(setup.tool_paths(layout, settings))
    return 0 if print_checks(layout) else 1


def main(argv: list[str]) -> int:
    layout = StudioLayout.for_root(ROOT)
    layout.ensure()
    if "--check" in argv:
        return 0 if print_checks(layout) else 1
    if "--install" in argv:
        return install_all(layout)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("The Studio window needs tkinter, which this Python does not include.\n"
              "Windows: reinstall Python from python.org with 'tcl/tk and IDLE' ticked, or delete any Python from "
              "PATH and run Start-Studio.bat to get a portable Python in this folder.\n"
              "Linux: install your distribution's python3-tk package, or run ./start-studio.sh --portable-python.")
        return 1
    from aniimo_pal.studio.app import main as gui_main

    return gui_main(ROOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
