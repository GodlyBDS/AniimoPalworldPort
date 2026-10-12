import copy
import json

import pytest

from aniimo_pal.config import ScanConfig
from aniimo_pal.errors import ManifestError, UnsafeOutputError
from aniimo_pal.manifest.diff import diff_manifests
from aniimo_pal.manifest.io import load_schema, read_manifest, validate_manifest, write_manifest
from aniimo_pal.manifest.verify import verify_manifest
from aniimo_pal.scanner import scan


@pytest.fixture
def manifest(game_tree):
    root, _ = game_tree
    return scan(root, ScanConfig())


def test_schema_is_valid_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(load_schema())


def test_roundtrip(manifest, out_dir):
    path = write_manifest(manifest, out_dir / "m.json")
    assert read_manifest(path) == json.loads(json.dumps(manifest))
    assert not list(out_dir.glob("*.tmp"))


def test_refuses_output_inside_input(game_tree):
    root, _ = game_tree
    with pytest.raises(UnsafeOutputError):
        write_manifest({}, root / "sub" / "m.json", read_only_roots=[root])
    assert not (root / "sub").exists()


def test_read_rejects_foreign_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}')
    with pytest.raises(ManifestError):
        read_manifest(p)
    p.write_text("not json")
    with pytest.raises(ManifestError):
        read_manifest(p)
    with pytest.raises(ManifestError):
        read_manifest(tmp_path / "missing.json")


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["summary"].update(file_count=999), "summary.file_count"),
    (lambda m: m["files"].reverse(), "not sorted"),
    (lambda m: m["files"].append(copy.deepcopy(m["files"][-1])), "duplicate"),
    (lambda m: m["files"][0].update(path="../escape"), "clean relative"),
    (lambda m: m["files"][0]["hashes"].update(md5="00"), "hashes do not match"),
])
def test_semantic_validation(manifest, mutate, needle):
    mutate(manifest)
    errors, _ = validate_manifest(manifest)
    assert any(needle in e for e in errors), errors


def test_schema_validation_catches_bad_types(manifest):
    pytest.importorskip("jsonschema")
    manifest["files"][0]["size"] = "big"
    manifest["unexpected"] = True
    errors, used = validate_manifest(manifest)
    assert used
    assert any("size" in e for e in errors)
    assert any("unexpected" in e for e in errors)


def test_diff(game_tree):
    root, files = game_tree
    old = scan(root, ScanConfig())
    (root / "Project/notes.txt").write_bytes(b"changed notes\n")
    (root / "Exports/creature_a.glb").rename(root / "Exports/renamed.glb")
    (root / "empty.dat").unlink()
    (root / "new.txt").write_text("new")
    new = scan(root, ScanConfig())
    d = diff_manifests(old, new)
    assert d["added"] == ["new.txt"]
    assert d["removed"] == ["empty.dat"]
    assert [m["path"] for m in d["modified"]] == ["Project/notes.txt"]
    assert d["moved"] == [{"from": "Exports/creature_a.glb", "to": "Exports/renamed.glb"}]
    assert not d["identical"]
    assert diff_manifests(old, old)["identical"]


def test_diff_requires_common_algorithm(game_tree):
    root, _ = game_tree
    a = scan(root, ScanConfig(hash_algorithms=("sha256",)))
    b = scan(root, ScanConfig(hash_algorithms=("md5",)))
    with pytest.raises(ManifestError):
        diff_manifests(a, b)


def test_verify(game_tree):
    root, _ = game_tree
    m = scan(root, ScanConfig())
    assert verify_manifest(m, root)["passed"]
    (root / "Project/notes.txt").write_text("tampered")
    (root / "empty.dat").unlink()
    (root / "extra.bin").write_bytes(b"x")
    r = verify_manifest(m, root)
    assert not r["passed"]
    assert r["changed"] == ["Project/notes.txt"]
    assert r["missing"] == ["empty.dat"]
    assert r["extra"] == ["extra.bin"]
    assert verify_manifest(m, root, check_extra=False)["extra"] == []
