import pytest

pytest.importorskip("UnityPy")

import unity_builder as ub  # noqa: E402

from aniimo_pal.adapters.unitypy_adapter import cab_key, iter_pptrs  # noqa: E402
from aniimo_pal.config import ScanConfig  # noqa: E402
from aniimo_pal.inventory import ObjectIndex, build_inventory  # noqa: E402
from aniimo_pal.scanner import scan  # noqa: E402


def test_inventory_lists_objects_and_resolves_refs(pipeline):
    doc = pipeline["objects"]
    s = doc["summary"]
    assert s["container_count"] == 2 and s["container_errors"] == 0
    assert s["unresolved_refs"] == 0 and s["object_errors"] == 0
    assert s["by_class"]["SkinnedMeshRenderer"] == 4  # 3 Foxlet prefabs + Mossbun
    idx = ObjectIndex(doc)
    fox = next(o for o in idx.by_class["GameObject"] if o["name"] == "Foxlet")
    assert fox["asset_path"] == "assets/creatures/foxlet/foxlet.prefab"
    assert fox["container"] == "StreamingAssets/foxlet.bundle"
    assert fox["id"].startswith("cab-foxlet:")
    tex = next(o for o in idx.by_class["Texture2D"] if o["name"] == "Foxlet_Albedo")
    assert tex["info"] == {"width": 4, "height": 4, "format": 4, "mip_count": 1, "streamed": None}
    avatar = idx.by_class["Avatar"][0]
    assert "Root/Hips/Spine/Head" in avatar["info"]["bone_paths"]


def test_encrypted_bundle_skipped_not_opened(pipeline):
    assert pipeline["objects"]["skipped"] == [{"path": "StreamingAssets/locked.bundle", "reason": "encryption flag set"}]


def test_clip_events_and_binding_hashes_recorded(pipeline):
    idx = ObjectIndex(pipeline["objects"])
    attack = next(o for o in idx.by_class["AnimationClip"] if o["name"] == "Foxlet_Attack")
    assert [e["function"] for e in attack["info"]["events"]] == ["OnHit", "PlayVfx"]
    assert attack["info"]["binding_path_hashes"] == sorted(ub.crc(p) for p in ub.DEFAULT_BONES)


def test_changed_file_is_reported_not_inventoried(creature_game):
    root, _ = creature_game
    manifest = scan(root, ScanConfig())
    (root / "StreamingAssets" / "mossbun.bundle").write_bytes(b"UnityFS\x00tampered")
    doc = build_inventory(manifest, root)
    bad = next(c for c in doc["containers"] if c["path"].endswith("mossbun.bundle"))
    assert bad["status"] == "error" and "re-scan" in bad["error"]
    assert doc["summary"]["container_errors"] == 1


def test_include_filter(creature_game):
    root, _ = creature_game
    doc = build_inventory(scan(root, ScanConfig()), root, include=["*foxlet*"])
    assert [c["path"] for c in doc["containers"]] == ["StreamingAssets/foxlet.bundle"]


def test_cross_bundle_reference_resolution(tmp_path):
    shared = ub.SerializedBuilder()
    noise = ub.add_texture(shared, "Shared_Noise")
    (tmp_path / "shared.bundle").write_bytes(ub.bundle(shared.build(), "CAB-shared"))
    b = ub.SerializedBuilder(externals=["archive:/CAB-shared/CAB-shared"])
    ub.add_creature(b, "Glimmer", bones=ub.DEFAULT_BONES, clips=["Idle"],
                    extra_textures={"_NoiseTex": ub.pptr(noise, file_id=1)})
    (tmp_path / "glimmer.bundle").write_bytes(ub.bundle(b.build(), "CAB-glimmer"))
    doc = build_inventory(scan(tmp_path, ScanConfig()), tmp_path)
    idx = ObjectIndex(doc)
    mat = next(o for o in idx.by_class["Material"] if o["name"] == "Glimmer_Mat")
    targets = [r["target"] for r in mat["refs"]]
    assert f"cab-shared:{noise}" in targets
    assert doc["summary"]["unresolved_refs"] == 0


def test_inventory_is_read_only(creature_game):
    import synthetic

    root, _ = creature_game
    before = synthetic.snapshot_tree(root)
    build_inventory(scan(root, ScanConfig()), root)
    assert synthetic.snapshot_tree(root) == before


def test_cab_key_and_pptr_walk():
    assert cab_key("archive:/CAB-ABC/CAB-ABC") == "cab-abc"
    assert cab_key("Library/unity default resources") == "unity default resources"
    data = {"m_Materials": [{"m_FileID": 0, "m_PathID": 5}, {"m_FileID": 0, "m_PathID": 0}],
            "m_TexEnvs": [("_MainTex", {"m_Texture": {"m_FileID": 1, "m_PathID": 9}})],
            "numbers": [1, 2, 3]}
    assert list(iter_pptrs(data)) == [("m_Materials[0]", 0, 5), ("m_TexEnvs[0][1].m_Texture", 1, 9)]


def test_text_assets_described_and_searchable(tmp_path, capsys):
    import json

    from aniimo_pal.cli import EXIT_OK, main

    b = ub.SerializedBuilder()
    b.add(49, m_Name="MonsterStatTable", m_Script="id,hp,atk\n1,80,101\n")
    b.add(49, m_Name="ElementConfig", m_Script='{"elements": ["fire"]}')
    b.add(49, m_Name="WorkBlob", m_Script=b"\x00\x01\xff\xfe".decode("utf-8", "surrogateescape"))
    b.add(49, m_Name="Readme", m_Script="hello")
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "data.bundle").write_bytes(ub.bundle(b.build(), "CAB-data"))
    doc = build_inventory(scan(tmp_path / "game", ScanConfig()), tmp_path / "game")
    info = {o["name"]: o["info"] for o in doc["objects"] if o["class"] == "TextAsset"}
    assert info == {"MonsterStatTable": {"size": 19, "content": "csv"},
                    "ElementConfig": {"size": 22, "content": "json"},
                    "WorkBlob": {"size": 4, "content": "binary"},
                    "Readme": {"size": 5, "content": "text"}}
    objs = tmp_path / "objects.json"
    objs.write_text(json.dumps(doc))
    capsys.readouterr()
    assert main(["objects", str(objs), "--class", "TextAsset", "--name", "stat|element|work", "--json"]) == EXIT_OK
    assert sorted(o["name"] for o in json.loads(capsys.readouterr().out)) == ["ElementConfig", "MonsterStatTable",
                                                                            "WorkBlob"]
    assert main(["objects", str(objs), "--name", "("]) == 2


def _three_bundle_game(tmp_path):
    """A creature bundle using a texture from a shared bundle, plus an unrelated bundle."""
    root = tmp_path / "game"
    root.mkdir()
    shared = ub.SerializedBuilder()
    noise = ub.add_texture(shared, "Shared_Noise")
    (root / "shared.bundle").write_bytes(ub.bundle(shared.build(), "CAB-shared"))
    b = ub.SerializedBuilder(externals=["archive:/CAB-shared/CAB-shared"])
    ub.add_creature(b, "Glimmer", bones=ub.DEFAULT_BONES, clips=["Idle"],
                    extra_textures={"_NoiseTex": ub.pptr(noise, file_id=1)})
    (root / "glimmer.bundle").write_bytes(ub.bundle(b.build(), "CAB-glimmer"))
    other = ub.SerializedBuilder()
    ub.add_texture(other, "Ui_Icon")
    other.add(49, m_Name="MonsterStatTable", m_Script="id,hp,atk\n1,80,101\n")
    (root / "ui.bundle").write_bytes(ub.bundle(other.build(), "CAB-ui"))
    return root


def test_targeted_reads_creatures_and_their_dependencies_only(tmp_path):
    root = _three_bundle_game(tmp_path)
    doc = build_inventory(scan(root, ScanConfig()), root)
    read = {c["path"]: c["read"] for c in doc["containers"]}
    assert read == {"glimmer.bundle": "deep", "shared.bundle": "deep", "ui.bundle": "survey"}
    s = doc["summary"]
    assert s["containers_deep_read"] == 2 and s["containers_with_skinned_meshes"] == 1
    assert s["dependency_rounds"] == 2 and s["unresolved_refs"] == 0
    names = {o["name"] for o in doc["objects"]}
    assert "Shared_Noise" in names and "Ui_Icon" not in names
    # Survey-only containers still list their data tables, and counts cover every class.
    table = next(o for o in doc["objects"] if o["name"] == "MonsterStatTable")
    assert table["info"] == {"size": 19, "content": "csv"} and table["container"] == "ui.bundle"
    assert s["all_classes_seen"]["Texture2D"] > s["by_class"]["Texture2D"]  # Ui_Icon counted, not listed


def test_targeted_matches_full_for_creature_objects(tmp_path):
    root = _three_bundle_game(tmp_path)
    manifest = scan(root, ScanConfig())
    targeted = build_inventory(manifest, root)
    full = build_inventory(manifest, root, strategy="full")
    keep = {"glimmer.bundle", "shared.bundle"}
    strip = ("container",)

    def objs(doc):
        return sorted(({k: v for k, v in o.items() if k not in strip} for o in doc["objects"] if o["container"] in keep),
                      key=lambda o: o["id"])

    assert objs(targeted) == objs(full)
    assert full["summary"]["containers_deep_read"] == 3


def test_cache_resumes_and_parallel_workers_agree(tmp_path):
    root = _three_bundle_game(tmp_path)
    manifest = scan(root, ScanConfig())
    cache = tmp_path / "cache"
    seen = []
    first = build_inventory(manifest, root, cache_dir=cache, progress=lambda d, t, m: seen.append((d, t, m)))
    assert first["summary"]["containers_from_cache"] == 0 and seen and seen[-1][0] == seen[-1][1]
    assert any(p.name.startswith("survey-") for p in cache.iterdir())
    again = build_inventory(manifest, root, cache_dir=cache)
    assert again["summary"]["containers_from_cache"] == 3
    parallel = build_inventory(manifest, root, workers=2)
    for doc in (again, parallel):
        assert doc["objects"] == first["objects"]


def test_quick_verify_catches_size_change_and_hash_mode_catches_edits(tmp_path):
    import os

    root = _three_bundle_game(tmp_path)
    manifest = scan(root, ScanConfig())
    target = root / "ui.bundle"
    data = bytearray(target.read_bytes())
    stat = target.stat()
    data[-1] ^= 0xFF  # same size, same mtime: only a full hash notices
    target.write_bytes(bytes(data))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert build_inventory(manifest, root)["summary"]["container_errors"] == 0
    assert build_inventory(manifest, root, verify="hash")["summary"]["container_errors"] == 1
    target.write_bytes(bytes(data) + b"x")
    bad = next(c for c in build_inventory(manifest, root)["containers"] if c["path"] == "ui.bundle")
    assert bad["status"] == "error" and "re-scan" in bad["error"]
