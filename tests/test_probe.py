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
