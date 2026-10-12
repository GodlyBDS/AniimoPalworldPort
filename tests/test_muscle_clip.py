import struct
import zlib

import pytest

from aniimo_pal.convert.muscle_clip import ClipNotDecodable, decode_clip, hash_lookup, structure_report


def crc(p):
    return zlib.crc32(p.encode())


def _streamed(frames):
    raw = b""
    for time, keys in frames:
        raw += struct.pack("<fi", time, len(keys))
        for index, coeffs in keys:
            raw += struct.pack("<i4f", index, *coeffs)
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _clip(extra=None, constant=(1.5,)):
    big = -3.4028234663852886e38  # float min: Unity's leading sentinel frame
    frames = [(big, [(0, (0, 0, 0, 0)), (1, (0, 0, 0, 2)), (2, (0, 0, 0, 0))]),
              (0.0, [(0, (0, 0, 1, 0)), (1, (0, 0, 0, 2)), (2, (0, 1, 0, 0))])]
    data = {"m_StreamedClip": {"data": _streamed(frames), "curveCount": 3},
            "m_DenseClip": {"m_FrameCount": 0, "m_CurveCount": 0, "m_SampleRate": 30.0, "m_BeginTime": 0.0,
                            "m_SampleArray": []},
            "m_ConstantClip": {"data": list(constant)}}
    data.update(extra or {})
    return {"m_Name": "Ani_Test", "m_SampleRate": 30.0,
            "m_MuscleClip": {"m_StartTime": 0.0, "m_StopTime": 2 / 30, "m_LoopTime": 1, "m_Clip": {"data": data}},
            "m_ClipBindingConstant": {"genericBindings": [{"path": crc("Root"), "attribute": 1, "typeID": 4},
                                                          {"path": crc("Root/Tail"), "attribute": 77, "typeID": 95}]},
            "m_Events": [{"time": 0.03, "functionName": "OnHit"}]}


def test_streamed_and_constant_curves_decoded():
    clip = decode_clip(_clip(), hash_lookup(["Root", "Root/Tail"]))
    pos = clip["tracks"][0]
    assert pos["path"] == "Root" and pos["channel"] == "position" and len(pos["keys"]) == 3
    t = 2 / 30
    assert pos["keys"][2]["v"] == pytest.approx([t, 2.0, t * t])
    assert clip["float_tracks"][0]["keys"][1]["v"] == [1.5] and clip["loop"]
    assert clip["events"][0]["function"] == "OnHit" and clip["decoder"]["curves"] == {
        "streamed": 3, "dense": 0, "constant": 1}


def test_unknown_curve_set_with_curves_is_refused_not_guessed():
    d = _clip(extra={"m_QuantizedClip": {"m_CurveCount": 709, "m_Data": [1, 2, 3]}})
    rep = structure_report(d)
    assert rep["unknown_sets_with_curves"] == {"m_QuantizedClip": 709} and rep["counts_add_up"]
    with pytest.raises(ClipNotDecodable, match="QuantizedClip"):
        decode_clip(d, {})
    with pytest.raises(ClipNotDecodable, match="does not match the bindings"):
        decode_clip(_clip(constant=()), {})


def test_dense_clip_from_a_real_type_tree(tmp_path):
    pytest.importorskip("UnityPy")
    import unity_builder as ub

    from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter

    b = ub.SerializedBuilder()
    ub.add_dense_clip(b, "Wag", [("Root/Hips/Tail", ub.ATTR_ROTATION, [(0, 0, 0, 1), (0, 0, 0.5, 0.8660254)]),
                                 ("Root", ub.ATTR_POSITION, [(0, 0.5, 0), (0, 0.7, 0)])])
    (tmp_path / "c.uab").write_bytes(ub.bundle(b.build(), "CAB-c"))
    objs = UnityPyAdapter().open_objects(tmp_path / "c.uab")
    reader = next(r for k, r in objs.items() if k != "__env__" and r.type.name == "AnimationClip")
    clip = decode_clip(reader.parse_as_dict(check_read=False), hash_lookup(ub.DEFAULT_BONES))
    by = {(t["path"], t["channel"]): t for t in clip["tracks"]}
    assert by[("Root/Hips/Tail", "rotation")]["keys"][-1]["v"] == pytest.approx([0, 0, 0.5, 0.8660254], abs=1e-6)
    assert by[("Root", "position")]["keys"][-1]["v"] == pytest.approx([0, 0.7, 0])
