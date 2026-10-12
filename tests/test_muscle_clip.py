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
    d = _clip(extra={"m_PredictClip": {"m_CurveCount": 709, "m_Data": [1, 2, 3]}})
    rep = structure_report(d)
    assert rep["unknown_sets_with_curves"] == {"m_PredictClip": 709} and rep["counts_add_up"]
    with pytest.raises(ClipNotDecodable, match="PredictClip"):
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


def _quat(axis, deg):
    import math
    h = math.radians(deg) / 2
    n = math.sqrt(sum(a * a for a in axis))
    return [a / n * math.sin(h) for a in axis] + [math.cos(h)]


def _quantized_clip():
    """A clip shaped like Aniimo's: 3 rotations + 1 position; 4 constant curves, the rest quantized.

    Quantized layout as observed in a real clip: types | int32 indices | float32 statics | per dynamic curve
    (float a, float b, uint16 sample per frame); here value = a + b * q / 65535 and indices = global curve index.
    """
    import random

    random.seed(3)
    frames, paths = 6, ["Root", "Root/Hips", "Root/Hips/Tail"]
    rows = []  # global curve order: binding order
    for i, _p in enumerate(paths):
        qs = [_quat([1, 0.3 * i, 0.2], 10 * f + 15 * i) for f in range(frames)]
        rows += [[q[c] for q in qs] for c in range(4)]
    rows += [[0.1 * f for f in range(frames)], [0.5] * frames, [0.0] * frames]  # position x, y, z
    total = len(rows)
    constant_ids = [13, 1]  # interleaved with quantized ones: placement must come from the indices
    quant_ids = [i for i in range(total) if i not in constant_ids]
    random.shuffle(quant_ids)
    static_ids = [i for i in quant_ids if len(set(rows[i])) == 1]
    dyn_ids = [i for i in quant_ids if i not in static_ids]
    order = static_ids + dyn_ids
    types = bytes(1 if i in dyn_ids else 0 for i in order)
    t_off, i_off = 0, (len(order) + 3) // 4 * 4
    s_off = i_off + 4 * len(order)
    d_off = s_off + 4 * len(static_ids)
    blob = bytearray(types.ljust(i_off, b"\0"))
    blob += struct.pack(f"<{len(order)}i", *order)
    blob += struct.pack(f"<{len(static_ids)}f", *[rows[i][0] for i in static_ids])
    for i in dyn_ids:
        lo, hi = min(rows[i]), max(rows[i])
        blob += struct.pack("<2f", lo, hi - lo)
        blob += struct.pack(f"<{frames}H", *[round((v - lo) / (hi - lo) * 65535) for v in rows[i]])
    qc = {"m_FrameCount": frames, "m_CurveCount": len(order), "m_SampleRate": 30.0, "m_BeginTime": 0.0,
          "m_NumStatic": len(static_ids), "m_NumDynamic": len(dyn_ids), "m_TypeOffset": t_off,
          "m_IndicesOffset": i_off, "m_StaticOffset": s_off, "m_FrameSize": 8 + 2 * frames, "m_DynamicOffset": d_off,
          "m_Data": list(blob)}
    data = {"m_StreamedClip": {"data": [], "curveCount": 0},
            "m_DenseClip": {"m_FrameCount": 0, "m_CurveCount": 0, "m_SampleRate": 30.0, "m_BeginTime": 0.0,
                            "m_SampleArray": []},
            "m_ConstantClip": {"data": [rows[i][0] for i in sorted(constant_ids)]},
            "m_QuantizedClip": qc}
    bindings = [{"path": crc(p), "attribute": 2, "typeID": 4} for p in paths] + \
               [{"path": crc("Root"), "attribute": 1, "typeID": 4}]
    d = {"m_Name": "Ani_Q", "m_SampleRate": 30.0,
         "m_MuscleClip": {"m_StartTime": 0.0, "m_StopTime": (frames - 1) / 30, "m_Clip": {"data": data}},
         "m_ClipBindingConstant": {"genericBindings": bindings}}
    return d, rows


def test_quantized_clip_reading_is_chosen_by_the_data_and_reported():
    d, rows = _quantized_clip()
    for c in (13, 1, 14):  # the constant curves must hold the constant values for the test to mean anything
        assert len(set(rows[c])) == 1
    clip = decode_clip(d, hash_lookup(["Root", "Root/Hips", "Root/Hips/Tail"]))
    reading = clip["decoder"]["quantized_reading"]
    assert reading["placement"] == "global index = indices[k]" and reading["dequantize"] == "a + b * q / 65535"
    assert reading["score"] == 1.0 and clip["decoder"]["provenance"].startswith("inferred")
    tail = next(t for t in clip["tracks"] if t["path"] == "Root/Hips/Tail")
    assert tail["keys"][3]["v"] == pytest.approx([r[3] for r in rows[8:12]], abs=1e-4)
    pos = next(t for t in clip["tracks"] if t["channel"] == "position")
    assert pos["keys"][5]["v"] == pytest.approx([0.5, 0.5, 0.0], abs=1e-4)


def test_quantized_clip_refused_when_no_reading_fits():
    d, _rows = _quantized_clip()
    qc = d["m_MuscleClip"]["m_Clip"]["data"]["m_QuantizedClip"]
    qc["m_Data"] = [b ^ 0x5A for b in qc["m_Data"]]  # scrambled bytes: nothing yields unit quaternions
    with pytest.raises(ClipNotDecodable, match="unit quaternion|layout not understood"):
        decode_clip(d, {})
