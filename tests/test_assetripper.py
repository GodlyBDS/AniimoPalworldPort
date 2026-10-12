import os
import sys
from pathlib import Path

import pytest

from aniimo_pal.adapters.assetripper import AssetRipperError, AssetRipperSession

FAKE = [sys.executable, str(Path(__file__).with_name("fake_assetripper.py"))]


def test_session_load_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AR_FILES", "Assets/Texture2D/Foxlet_Albedo.png")
    src = tmp_path / "in" / "foxlet.bundle"
    src.parent.mkdir()
    src.write_bytes(b"x")
    with AssetRipperSession(FAKE, tmp_path / "logs", startup_timeout=20, command_timeout=20) as s:
        s.load_files([src])
        out = s.export(tmp_path / "export", "primary")
        s.reset()
        assert s.proc is not None
    assert (out / "Assets" / "Loaded" / "foxlet.glb").is_file()
    assert (out / "Assets" / "Texture2D" / "Foxlet_Albedo.png").is_file()
    assert "Finished processing assets" in (tmp_path / "logs" / "assetripper.stdout.log").read_text()


def test_export_dir_must_be_empty(tmp_path):
    (tmp_path / "export").mkdir()
    (tmp_path / "export" / "keep.txt").write_text("user data")
    with AssetRipperSession(FAKE, tmp_path / "logs", startup_timeout=20) as s:
        with pytest.raises(AssetRipperError, match="empty"):
            s.export(tmp_path / "export")
    assert (tmp_path / "export" / "keep.txt").read_text() == "user data"


def test_startup_failure_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AR_FAIL_STARTUP", "1")
    with pytest.raises(AssetRipperError, match="exited during startup"):
        AssetRipperSession(FAKE, tmp_path / "logs", startup_timeout=20).start()


def test_missing_executable(tmp_path):
    with pytest.raises(AssetRipperError, match="cannot start"):
        AssetRipperSession([str(tmp_path / "missing")], tmp_path / "logs").start()


def test_command_timeout(tmp_path):
    with AssetRipperSession(FAKE, tmp_path / "logs", startup_timeout=20, command_timeout=0.1) as s:
        with pytest.raises(AssetRipperError, match="did not finish"):
            s.load_folder(tmp_path)


def test_creature_export_with_fake(pipeline, monkeypatch):
    from aniimo_pal.creature.model import creature_path, save_creature
    from aniimo_pal.creature.workflow import creature_from_catalog
    from aniimo_pal.errors import ToolkitError
    from aniimo_pal.extract import assetripper_export_creature

    monkeypatch.setenv("FAKE_AR_FILES", "Assets/Texture2D/Foxlet_Albedo.png,Assets/creatures/foxlet/Foxlet_Shiny.glb")
    c = creature_from_catalog(pipeline["catalog"], "foxlet")
    path = save_creature(c, creature_path(pipeline["workspace"], c["id"]))
    report = assetripper_export_creature(c, path.parent, pipeline["root"], FAKE)
    assert report["containers"] == ["StreamingAssets/foxlet.bundle"]
    tex = next(a for a in c["assets"] if a["name"] == "Foxlet_Albedo")
    assert tex["assetripper"]["primary"] == ["extract/assetripper/primary/Assets/Texture2D/Foxlet_Albedo.png"]
    shiny = next(p for p in c["prefabs"] if p["variant"] == "shiny")
    assert shiny["assetripper"]["primary"][0].endswith("Foxlet_Shiny.glb")
    with pytest.raises(ToolkitError, match="--replace"):
        assetripper_export_creature(c, path.parent, pipeline["root"], FAKE)
    assetripper_export_creature(c, path.parent, pipeline["root"], FAKE, replace=True)


@pytest.mark.skipif(not os.environ.get("ASSETRIPPER_PATH"), reason="set ASSETRIPPER_PATH to run against real AssetRipper")
def test_real_assetripper_exports_synthetic_creature(pipeline):
    """Integration test against a real AssetRipper.GUI.Free build (not run in CI)."""
    from aniimo_pal.creature.model import creature_path, save_creature
    from aniimo_pal.creature.workflow import creature_from_catalog
    from aniimo_pal.extract import assetripper_export_creature

    exe = os.environ["ASSETRIPPER_PATH"]
    c = creature_from_catalog(pipeline["catalog"], "foxlet")
    path = save_creature(c, creature_path(pipeline["workspace"], c["id"]))
    primary = assetripper_export_creature(c, path.parent, pipeline["root"], exe, mode="primary")
    assert primary["summary"]["prefabs_matched"] == 3
    assert {a["name"] for a in c["assets"] if a["role"] == "texture" and a.get("assetripper")} == {
        "Foxlet_Albedo", "Foxlet_Normal", "Foxlet_Shiny_Albedo", "Foxlet_Shiny_Normal",
        "Foxlet_Prismana_Albedo", "Foxlet_Prismana_Normal"}
    project = assetripper_export_creature(c, path.parent, pipeline["root"], exe, mode="project")
    anims = {a["name"]: a for a in c["assets"] if a["role"] == "animation"}
    assert all(a["assetripper"]["project"][0].endswith(".anim") for a in anims.values())
    assert project["summary"]["assets_unmatched"] == 0
