import os

import pytest
import synthetic

from aniimo_pal.config import ScanConfig
from aniimo_pal.manifest.io import validate_manifest
from aniimo_pal.scanner import ScanOptions, scan
from aniimo_pal.scanner import scanner as scanner_mod

EXPECTED_KINDS = {
    "Game_Data/data.unity3d": "unity_bundle",
    "Game_Data/sharedassets0.assets": "unity_serialized",
    "Game_Data/sharedassets0.assets.resS": "unity_resource",
    "Game_Data/level0": "unity_serialized",
    "Game_Data/StreamingAssets/creatures/creature_a.bundle": "unity_bundle",
    "Game_Data/StreamingAssets/creatures/locked.bundle": "unity_bundle",
    "Game_Data/StreamingAssets/creatures/mystery.bundle": "unknown_high_entropy",
    "Game_Data/il2cpp_data/Metadata/global-metadata.dat": "il2cpp_metadata",
    "Exports/creature_a.fbx": "model_fbx",
    "Exports/creature_a.glb": "model_glb",
    "Exports/creature_a_albedo.png": "image_png",
    "Exports/creature_a_normal.dds": "texture_dds",
    "Exports/creature_a_mask.ktx2": "texture_ktx2",
    "Project/Assets/Creature.prefab": "unity_yaml",
    "Project/Assets/Creature.prefab.meta": "unity_meta",
    "Project/notes.txt": "text",
    "Project/settings.json": "json",
    "empty.dat": "empty",
}


def test_synthetic_tree_kinds(game_tree):
    root, files = game_tree
    manifest = scan(root, ScanConfig())
    kinds = {f["path"]: f["kind"] for f in manifest["files"]}
    assert kinds == EXPECTED_KINDS
    assert set(kinds) == set(files)


def test_hashes_and_sizes_match(game_tree):
    import hashlib

    root, files = game_tree
    manifest = scan(root, ScanConfig(hash_algorithms=("sha256", "md5")))
    for entry in manifest["files"]:
        data = files[entry["path"]]
        assert entry["size"] == len(data)
        assert entry["hashes"]["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["hashes"]["md5"] == hashlib.md5(data).hexdigest()


def test_manifest_is_valid_and_sorted(game_tree):
    root, _ = game_tree
    manifest = scan(root, ScanConfig())
    errors, _ = validate_manifest(manifest)
    assert errors == []
    paths = [f["path"] for f in manifest["files"]]
    assert paths == sorted(paths)


def test_summary_counts(game_tree):
    root, files = game_tree
    s = scan(root, ScanConfig())["summary"]
    assert s["file_count"] == len(files)
    assert s["total_bytes"] == sum(len(b) for b in files.values())
    assert s["by_kind"]["unity_bundle"] == 3
    assert s["encrypted_count"] == 1
    assert s["unity_versions"]["2021.3.15f1"] >= 3
    assert s["unity_versions"]["2019.4.40f1"] == 1


def test_encrypted_bundle_not_supported(game_tree):
    root, _ = game_tree
    entries = {f["path"]: f for f in scan(root, ScanConfig())["files"]}
    locked = entries["Game_Data/StreamingAssets/creatures/locked.bundle"]
    assert locked["details"]["encryption_flag"] is True
    assert locked["supported"] is False
    assert any("does not decrypt" in w for w in locked["warnings"])
    assert entries["Game_Data/StreamingAssets/creatures/creature_a.bundle"]["supported"] is True
    assert entries["Game_Data/StreamingAssets/creatures/mystery.bundle"]["supported"] is False


def test_relations(game_tree):
    root, _ = game_tree
    entries = {f["path"]: f for f in scan(root, ScanConfig())["files"]}
    assert entries["Game_Data/sharedassets0.assets.resS"]["relations"] == [
        {"type": "resource_of", "path": "Game_Data/sharedassets0.assets"}]
    assert entries["Project/Assets/Creature.prefab.meta"]["relations"] == [
        {"type": "meta_for", "path": "Project/Assets/Creature.prefab"}]


def test_orphan_resource_warns(tmp_path):
    (tmp_path / "lonely.resS").write_bytes(bytes(range(256)))
    entry = scan(tmp_path, ScanConfig())["files"][0]
    assert any("no companion" in w for w in entry["warnings"])


def test_scan_is_read_only(game_tree):
    root, _ = game_tree
    before = synthetic.snapshot_tree(root)
    scan(root, ScanConfig(workers=4))
    assert synthetic.snapshot_tree(root) == before


def test_worker_count_does_not_change_output(game_tree):
    root, _ = game_tree
    a = scan(root, ScanConfig(workers=1))
    b = scan(root, ScanConfig(workers=8))
    assert a["files"] == b["files"]


def test_hidden_and_excluded(tmp_path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    m = scan(tmp_path, ScanConfig(exclude=("logs",)))
    assert [f["path"] for f in m["files"]] == ["keep.txt"]
    reasons = {s["path"]: s["reason"] for s in m["skipped"]}
    assert reasons[".hidden"] == "hidden"
    assert reasons[".git/"] == "hidden"
    assert reasons["logs/"].startswith("excluded")

    m2 = scan(tmp_path, ScanConfig(include_hidden=True, exclude=("*.log",)))
    assert [f["path"] for f in m2["files"]] == [".git/config", ".hidden", "keep.txt"]


def _symlink(src, dst):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform/user")


def test_symlinks_not_followed_by_default(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside root")
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_text("x")
    _symlink(outside, root / "linkdir")
    _symlink(outside / "secret.txt", root / "link.txt")
    m = scan(root, ScanConfig())
    assert [f["path"] for f in m["files"]] == ["real.txt"]
    reasons = {s["path"]: s["reason"] for s in m["skipped"]}
    assert "symlink" in reasons["link.txt"] and "symlink" in reasons["linkdir/"]

    m2 = scan(root, ScanConfig(follow_symlinks=True))
    assert [f["path"] for f in m2["files"]] == ["link.txt", "linkdir/secret.txt", "real.txt"]


def test_symlink_loop_terminates(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("x")
    _symlink(tmp_path, tmp_path / "a" / "loop")
    m = scan(tmp_path, ScanConfig(follow_symlinks=True))
    assert "a/f.txt" in [f["path"] for f in m["files"]]
    assert any(s["reason"] == "symlink loop" for s in m["skipped"])


def test_unreadable_file_is_recorded_not_fatal(game_tree, monkeypatch):
    root, files = game_tree
    real_open = open

    def flaky_open(path, mode="r", *a, **kw):
        if str(path).endswith("level0"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(scanner_mod, "open", flaky_open, raising=False)
    m = scan(root, ScanConfig(workers=1))
    entry = next(f for f in m["files"] if f["path"] == "Game_Data/level0")
    assert entry["error"].startswith("PermissionError")
    assert m["summary"]["error_count"] == 1
    assert m["summary"]["file_count"] == len(files)
    assert validate_manifest(m)[0] == []


def test_root_label_and_absolute_root(game_tree):
    root, _ = game_tree
    m = scan(root, ScanConfig())
    assert m["source"] == {"root_label": "game", "root_path": None, "host_platform": m["source"]["host_platform"]}
    m = scan(root, ScanConfig(), ScanOptions(root_label="Aniimo 1.0", store_absolute_root=True))
    assert m["source"]["root_label"] == "Aniimo 1.0"
    assert m["source"]["root_path"] == str(root.resolve())


def test_no_mtime(game_tree):
    root, _ = game_tree
    m = scan(root, ScanConfig(record_mtime=False))
    assert all("mtime_utc" not in f for f in m["files"])


def test_probe_bytes_limit_still_hashes_whole_file(tmp_path):
    import hashlib

    data = synthetic.high_entropy(3 * 1024 * 1024 + 7)
    (tmp_path / "big.bin").write_bytes(data)
    entry = scan(tmp_path, ScanConfig(probe_bytes=128))["files"][0]
    assert entry["size"] == len(data)
    assert entry["hashes"]["sha256"] == hashlib.sha256(data).hexdigest()
