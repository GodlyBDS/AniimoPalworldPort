"""AssetRipper adapter (https://github.com/AssetRipper/AssetRipper, GPL-3.0).

AssetRipper is a separate program; this toolkit only launches it as an external
process and talks to its local web server, so its GPL code is not linked into
this package.

Interface verified from AssetRipper source (master @ beeb50e, 2026-10-08),
``Source/AssetRipper.GUI.Web``; NOT exercised against a running AssetRipper:

* Launch: ``AssetRipper.GUI.Free --headless --port <n>`` (Ookii POSIX options:
  ``--port``, ``--headless``, ``--log``, ``--log-path``).
* ``POST /LoadFolder`` or ``/LoadFile`` with form field ``Path``.
* ``POST /Export/PrimaryContent`` (meshes as glTF/GLB, textures as images) or
  ``/Export/UnityProject`` with form field ``Path`` (output directory).
* ``POST /Reset`` to unload.

Milestone 1 provides discovery and command construction only.
"""

from __future__ import annotations

from aniimo_pal.adapters.base import ToolAdapter, ToolStatus, find_executable

EXECUTABLE_CANDIDATES = ("AssetRipper.GUI.Free", "AssetRipper.GUI.Free.exe", "AssetRipper")

ENDPOINTS = {
    "load_folder": "/LoadFolder",
    "load_file": "/LoadFile",
    "export_primary_content": "/Export/PrimaryContent",
    "export_unity_project": "/Export/UnityProject",
    "reset": "/Reset",
}


class AssetRipperAdapter(ToolAdapter):
    name = "AssetRipper"
    license = "GPL-3.0"
    homepage = "https://github.com/AssetRipper/AssetRipper"
    handles = ("unity_bundle", "unity_bundle_legacy", "unity_serialized", "unity_resource", "unity_package")

    def __init__(self, configured_path: str = "") -> None:
        self.configured_path = configured_path

    def status(self) -> ToolStatus:
        location = find_executable(self.configured_path, EXECUTABLE_CANDIDATES)
        notes = "headless HTTP interface; see docs/research.md"
        if location is None and self.configured_path:
            notes = f"configured path does not exist: {self.configured_path}"
        return ToolStatus(self.name, location is not None, location=location,
                          license=self.license, homepage=self.homepage, notes=notes)

    @staticmethod
    def headless_command(executable: str, port: int) -> list[str]:
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535")
        return [executable, "--headless", "--port", str(port)]
