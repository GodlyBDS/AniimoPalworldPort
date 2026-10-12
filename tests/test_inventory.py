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
    assert tex["info"] == {"width": 4, "height": 4, "format": 4, "mip_count": 1, "streamed": False}
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
