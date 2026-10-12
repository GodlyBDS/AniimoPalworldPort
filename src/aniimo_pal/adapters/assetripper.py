"""AssetRipper adapter (https://github.com/AssetRipper/AssetRipper, GPL-3.0).

AssetRipper is a separate program; this toolkit launches it as an external
process and talks to its local web server, so its GPL code is not linked into
this package.

Interface (AssetRipper master @ beeb50e, built from source and exercised by
this project on 2026-10-12, see docs/research.md):

* Launch: ``AssetRipper.GUI.Free --headless --port <n> [--log-path <file>]``.
  ``--headless`` and ``--log`` are switches (``--log false`` is rejected).
* Commands are ``POST`` with an ``application/x-www-form-urlencoded`` body
  (multipart is rejected with HTTP 415) and form field ``Path`` (repeatable
  for ``/LoadFile``): ``/LoadFile``, ``/LoadFolder``, ``/Export/PrimaryContent``,
  ``/Export/UnityProject``, ``/Reset``. Each answers ``302`` immediately.
* Completion is reported on stdout; we wait for these lines:
  ``Processing : Finished processing assets`` (load),
  ``Export : Finished exporting primary content.`` (primary content),
  ``Export : Finished post-export`` (Unity project).

Observed export behaviour on synthetic data: PrimaryContent writes textures as
PNG and meshes/prefabs as GLB but no AnimationClips; UnityProject writes
``.anim``, ``.controller``, ``.prefab``, ``.mat`` and mesh ``.asset`` files.
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Sequence

from aniimo_pal.adapters.base import ToolAdapter, ToolStatus, find_executable
from aniimo_pal.errors import ToolkitError

EXECUTABLE_CANDIDATES = ("AssetRipper.GUI.Free", "AssetRipper.GUI.Free.exe", "AssetRipper")

ENDPOINTS = {
    "load_folder": "/LoadFolder",
    "load_file": "/LoadFile",
    "export_primary_content": "/Export/PrimaryContent",
    "export_unity_project": "/Export/UnityProject",
    "reset": "/Reset",
}
MARKERS = {
    "load": "Processing : Finished processing assets",
    "export_primary_content": "Export : Finished exporting primary content.",
    "export_unity_project": "Export : Finished post-export",
}


class AssetRipperError(ToolkitError):
    """AssetRipper could not be started or a command did not complete."""


class AssetRipperAdapter(ToolAdapter):
    name = "AssetRipper"
    license = "GPL-3.0"
    homepage = "https://github.com/AssetRipper/AssetRipper"
    handles = ("unity_bundle", "unity_bundle_legacy", "unity_serialized", "unity_resource", "unity_package")

    def __init__(self, configured_path: str = "") -> None:
        self.configured_path = configured_path

    def locate(self) -> str | None:
        return find_executable(self.configured_path, EXECUTABLE_CANDIDATES)

    def status(self) -> ToolStatus:
        location = self.locate()
        notes = "headless HTTP interface; see docs/research.md"
        if location is None and self.configured_path:
            notes = f"configured path does not exist: {self.configured_path}"
        return ToolStatus(self.name, location is not None, location=location,
                          license=self.license, homepage=self.homepage, notes=notes)

    @staticmethod
    def headless_command(executable: str | Sequence[str], port: int, log_path: str | None = None) -> list[str]:
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535")
        base = [executable] if isinstance(executable, str) else list(executable)
        cmd = [*base, "--headless", "--port", str(port)]
        if log_path:
            cmd += ["--log-path", log_path]
        return cmd


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AssetRipperSession:
    """Run one AssetRipper process for the duration of a ``with`` block."""

    def __init__(self, executable: str | Sequence[str], work_dir: str | Path, *, port: int | None = None,
                 startup_timeout: float = 60.0, command_timeout: float = 3600.0):
        self.executable = executable
        self.work_dir = Path(work_dir)
        self.port = port or free_port()
        self.startup_timeout = startup_timeout
        self.command_timeout = command_timeout
        self.stdout_path = self.work_dir / "assetripper.stdout.log"
        self.proc: subprocess.Popen | None = None
        self._stdout = None

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> "AssetRipperSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        cmd = AssetRipperAdapter.headless_command(self.executable, self.port,
                                                  str(self.work_dir / "assetripper.file.log"))
        self._stdout = open(self.stdout_path, "wb")
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.work_dir, stdout=self._stdout, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
        except OSError as exc:
            self._stdout.close()
            raise AssetRipperError(f"cannot start AssetRipper: {exc}") from exc
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssetRipperError(f"AssetRipper exited during startup (code {self.proc.returncode}):\n"
                                       + self.log_tail())
            try:
                with urllib.request.urlopen(self.base_url + "/", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.25)
        self.stop()
        raise AssetRipperError(f"AssetRipper did not become ready within {self.startup_timeout}s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None
        if self._stdout is not None:
            self._stdout.close()
            self._stdout = None

    # -- commands ---------------------------------------------------------------

    def log_text(self) -> str:
        try:
            return self.stdout_path.read_text("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def log_tail(self, lines: int = 30) -> str:
        return "\n".join(self.log_text().splitlines()[-lines:])

    def _post(self, endpoint: str, paths: Sequence[str] = ()) -> int:
        body = urllib.parse.urlencode([("Path", p) for p in paths]).encode("ascii")
        req = urllib.request.Request(self.base_url + endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            # Export requests can block until AssetRipper has finished, which takes minutes for big inputs.
            with opener.open(req, timeout=max(60.0, self.command_timeout)) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303):
                return exc.code
            raise AssetRipperError(f"{endpoint} failed with HTTP {exc.code}") from exc

    def _run(self, endpoint: str, marker_key: str, paths: Sequence[str]) -> None:
        marker = MARKERS[marker_key]
        before = self.log_text().count(marker)
        self._post(endpoint, paths)
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline:
            if self.log_text().count(marker) > before:
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise AssetRipperError(f"AssetRipper exited during {endpoint}:\n{self.log_tail()}")
            time.sleep(0.2)
        raise AssetRipperError(f"{endpoint} did not finish within {self.command_timeout}s:\n{self.log_tail()}")

    def load_files(self, files: Sequence[str | Path]) -> None:
        if not files:
            raise AssetRipperError("no files to load")
        self._run(ENDPOINTS["load_file"], "load", [str(Path(f).resolve()) for f in files])

    def load_folder(self, folder: str | Path) -> None:
        self._run(ENDPOINTS["load_folder"], "load", [str(Path(folder).resolve())])

    def export(self, out_dir: str | Path, mode: str = "primary") -> Path:
        out = Path(out_dir).resolve()
        if out.exists() and any(out.iterdir()):
            raise AssetRipperError(f"export directory must be empty or absent: {out}")
        if mode == "primary":
            self._run(ENDPOINTS["export_primary_content"], "export_primary_content", [str(out)])
        elif mode == "project":
            self._run(ENDPOINTS["export_unity_project"], "export_unity_project", [str(out)])
        else:
            raise ValueError("mode must be 'primary' or 'project'")
        return out

    def reset(self) -> None:
        self._post(ENDPOINTS["reset"])
