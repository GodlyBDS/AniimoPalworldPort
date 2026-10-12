import pytest

pytest.importorskip("UnityPy")

import unity_builder as ub  # noqa: E402

from aniimo_pal.config import ScanConfig  # noqa: E402
from aniimo_pal.inventory import build_inventory  # noqa: E402
from aniimo_pal.probe import probe_clip, probe_prefab  # noqa: E402
from aniimo_pal.scanner import scan  # noqa: E402


def test_probe_prefab_shows_the_material_chain_across_bundles(tmp_path):
    data, _ids = ub.creature_bundle("Foxlet")
    (tmp_path / "foxlet.uab").write_bytes(data)
    rep = probe_prefab(tmp_path, name="Foxlet", container="foxlet.uab")
    s = rep["summary"]
    assert s["renderers"] == 1 and s["renderers_with_mesh"] == 1 and s["material_slots_resolved"] == 1
    slot = rep["renderers"][0]["material_slots"][0]
    assert slot["name"] == "Foxlet_Mat" and slot["material"]["texture_slots"][0]["name"] == "Foxlet_Albedo"

    # Empty slots are reported as such, not dropped.
    b = ub.SerializedBuilder()
    ub.add_creature(b, "Bare", bones=ub.DEFAULT_BONES, clips=[], with_controller=False)
    for o in b._objects:
        if o.class_id == 137:
            o.data["m_Mesh"] = ub.pptr(0)
            o.data["m_Materials"] = [ub.pptr(0)]
    (tmp_path / "bare.uab").write_bytes(ub.bundle(b.build(), "CAB-bare"))
    doc = build_inventory(scan(tmp_path, ScanConfig()), tmp_path)
    rep = probe_prefab(tmp_path, name="Bare", container="bare.uab", objects_doc=doc)
    assert rep["summary"]["renderers_with_empty_mesh_slot"] == 1 and rep["summary"]["material_slots_empty"] == 1


def test_probe_clip_reports_layout_and_decoder_verdict(tmp_path):
    b = ub.SerializedBuilder()
    ub.add_dense_clip(b, "Ani_Wag", [("Root", ub.ATTR_POSITION, [(0, 0, 0), (0, 1, 0)])])
    (tmp_path / "c.uab").write_bytes(ub.bundle(b.build(), "CAB-c"))
    rep = probe_clip(tmp_path, name="Ani_Wag", container="c.uab", skeleton_paths=["Root"])
    assert rep["structure"]["counts_add_up"] and rep["decoder"]["status"] == "decoded"
    assert rep["curve_sets"]["m_DenseClip"]["m_SampleArray"]["len"] == 6


def test_follow_shows_mesh_and_material_held_by_a_data_asset():
    """Stub resolver: a renderer's data asset (like XRender PlatformData) holding the mesh and the material."""
    from aniimo_pal.probe import _follow, _reachable_classes

    objs = {
        "c:1": ("MonoBehaviour", {"m_Name": "P_X_Body_PlatformData", "m_Script": {"m_FileID": 0, "m_PathID": 9},
                                  "m_Mesh": {"m_FileID": 0, "m_PathID": 2},
                                  "m_Materials": [{"m_FileID": 0, "m_PathID": 3}]}),
        "c:9": ("MonoScript", {"m_ClassName": "XRenderPlatformData"}),
        "c:2": ("Mesh", {"m_Name": "M_X_Body_123", "m_VertexData": {"m_VertexCount": 812}, "m_BindPose": [0] * 40}),
        "c:3": ("Material", {"m_Name": "MI_X_Body_01a", "m_Shader": {"m_FileID": 0, "m_PathID": 0},
                             "m_SavedProperties": {"m_TexEnvs": [("_BaseMap", {"m_Texture": {"m_FileID": 0,
                                                                                             "m_PathID": 4}})]}}),
        "c:4": ("Texture2D", {"m_Name": "T_X_Body_D", "m_Width": 1024, "m_Height": 1024}),
    }

    class Reader:
        def __init__(self, cls):
            self.type = type("T", (), {"name": cls})

    class Res:
        readers = {k: Reader(v[0]) for k, v in objs.items()}

        def read(self, oid):
            return dict(objs[oid][1]) if oid in objs else None

        def target(self, _reader, pptr):
            pid = int(pptr.get("m_PathID") or 0)
            return (f"c:{pid}", "resolved") if pid else (None, "null (empty slot)")

        def summary(self, oid):
            if oid not in objs:
                return {}
            return {"class": objs[oid][0], "name": objs[oid][1].get("m_Name") or objs[oid][1].get("m_ClassName")}

    out = _follow(Res(), "c:1", depth=2)
    assert out["script"]["class_name"] == "XRenderPlatformData"
    by_field = {r["field"]: r for r in out["references"]}
    assert by_field["m_Mesh"]["name"] == "M_X_Body_123" and by_field["m_Mesh"]["vertex_count"] == 812
    assert by_field["m_Materials[0]"]["material"]["texture_slots"][0]["name"] == "T_X_Body_D"
    assert {"Mesh", "Material"} <= _reachable_classes([{"class": "MonoBehaviour", "follow": out}])
