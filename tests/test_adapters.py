import sys

import pytest

from aniimo_pal.adapters import AssetRipperAdapter, BlenderAdapter, all_adapters
from aniimo_pal.adapters.base import find_executable
from aniimo_pal.config import ToolsConfig


def test_status_never_raises():
    for adapter in all_adapters(ToolsConfig()):
        status = adapter.status()
        assert status.name and isinstance(status.available, bool)


def test_configured_path_missing_reports_unavailable(tmp_path):
    status = AssetRipperAdapter(str(tmp_path / "missing.exe")).status()
    assert status.available is False
    assert "does not exist" in status.notes


def test_configured_path_found(tmp_path):
    exe = tmp_path / "blender"
    exe.write_text("")
    status = BlenderAdapter(str(exe)).status()
    assert status.available and status.location == str(exe)


@pytest.mark.skipif(sys.platform == "win32", reason="PATHEXT rules differ on Windows")
def test_find_executable_on_path(tmp_path, monkeypatch):
    exe = tmp_path / "AssetRipper.GUI.Free"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_executable("", ("AssetRipper.GUI.Free",)) == str(exe)


def test_assetripper_headless_command():
    assert AssetRipperAdapter.headless_command("ar", 8999) == ["ar", "--headless", "--port", "8999"]
    with pytest.raises(ValueError):
        AssetRipperAdapter.headless_command("ar", 0)
