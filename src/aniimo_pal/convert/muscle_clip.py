"""Decode a built (compiled) Unity AnimationClip into the neutral ``aniimo-pal/clip`` format.

Why: player builds keep animation only in ``m_MuscleClip`` (the editor curves are
empty), and AssetRipper 2.0 exported no ``.anim`` for the real Aniimo clips. UnityPy
reads the clip with the type tree stored in the bundle; this module evaluates the
compiled curves itself.

Layout of a compiled clip (Unity 5-2022, generic rigs): ``m_MuscleClip.m_Clip.data``
holds three curve sets whose curve indices follow each other:

* ``m_StreamedClip`` - cubic segments: a stream of frames ``(time, key count,
  [curve index, c0, c1, c2, c3])``; value ``((c0*dt + c1)*dt + c2)*dt + c3``;
* ``m_DenseClip`` - one sample per frame and curve at ``m_SampleRate`` from ``m_BeginTime``;
* ``m_ConstantClip`` - one value per curve.

``m_ClipBindingConstant.genericBindings`` names the curves in index order: a Transform
position/scale/euler binding owns 3 curves, a rotation binding 4 (quaternion x, y, z, w),
anything else 1. Binding paths are CRC32 hashes of the transform path; they are named
through the Avatar's TOS table (verified) or the creature's skeleton paths.

Clips may carry curve sets this module does not know (Aniimo: ``QuantizedClip``,
``PredictClip``). If such a set holds curves, the clip is **not** decoded: a partial
animation would look plausible and be wrong. The structure is reported instead
(``describe``) so the format can be studied.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from aniimo_pal.convert.unity_anim import SCHEMA_ID, SCHEMA_VERSION
from aniimo_pal.errors import ToolkitError

KNOWN_SETS = ("m_StreamedClip", "m_DenseClip", "m_ConstantClip")
ATTR_CHANNEL = {1: ("position", 3), 2: ("rotation", 4), 3: ("scale", 3), 4: ("euler", 3)}
TRANSFORM_CLASS_ID = 4
_SENTINEL = 1e30  # streamed clips start/end with frames at +-infinity (or float max)


class ClipNotDecodable(ToolkitError):
    """The clip holds curves in a format this decoder cannot evaluate."""

    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def describe(value: Any, depth: int = 0, max_depth: int = 6) -> Any:
    """Compact structural summary of parsed Unity data: field names, types, lengths, first values."""
    if isinstance(value, dict):
        if depth >= max_depth:
            return {"type": "dict", "keys": sorted(value)[:40]}
        return {k: describe(v, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "len": len(value), "head_hex": bytes(value[:32]).hex()}
    if isinstance(value, (list, tuple)):
        if not value:
            return {"type": "list", "len": 0}
        if all(isinstance(v, bool) for v in value[:64]):
            return {"type": "list[bool]", "len": len(value), "head": list(value[:16])}
        if all(isinstance(v, (int, float)) for v in value[:64]):
            nums = [v for v in value if isinstance(v, (int, float))]
            kind = "int" if all(isinstance(v, int) for v in value[:64]) else "float"
            return {"type": f"list[{kind}]", "len": len(value), "head": list(value[:16]),
                    "min": min(nums), "max": max(nums)}
        return {"type": "list", "len": len(value), "first": describe(value[0], depth + 1, max_depth)}
    return value


def _clip_data(d: dict[str, Any]) -> dict[str, Any]:
    clip = (d.get("m_MuscleClip") or {}).get("m_Clip") or {}
    return clip.get("data", clip) if isinstance(clip, dict) else {}


def _set_curve_count(name: str, value: Any) -> int | None:
    """Curve count declared by a curve set (None if it has no recognisable count)."""
    if not isinstance(value, dict):
        return None
    if name == "m_StreamedClip":
        return int(value.get("curveCount") or 0)
    if name == "m_DenseClip":
        return int(value.get("m_CurveCount") or 0)
    if name == "m_ConstantClip":
        return len(value.get("data") or [])
    for key, v in value.items():
        if "curvecount" in key.lower().replace("_", "") and isinstance(v, int):
            return v
    return None


def curve_sets(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every curve set in the compiled clip with its declared curve count (known and unknown ones)."""
    out = {}
    for name, value in _clip_data(d).items():
        if not isinstance(value, dict):
            continue
        count = _set_curve_count(name, value)
        out[name] = {"known": name in KNOWN_SETS, "curve_count": count}
    return out


def binding_widths(bindings: list[dict[str, Any]]) -> list[int]:
    out = []
    for b in bindings:
        if int(b.get("typeID", 0) or 0) == TRANSFORM_CLASS_ID and int(b.get("attribute", 0) or 0) in ATTR_CHANNEL:
            out.append(ATTR_CHANNEL[int(b["attribute"])][1])
        else:
            out.append(1)
    return out


def structure_report(d: dict[str, Any]) -> dict[str, Any]:
    """What a compiled clip contains, and whether its curve counts add up to its bindings."""
    bindings = (d.get("m_ClipBindingConstant") or {}).get("genericBindings") or []
    sets = curve_sets(d)
    needed = sum(binding_widths(bindings))
    known = sum(s["curve_count"] or 0 for s in sets.values() if s["known"])
    unknown = {k: s["curve_count"] for k, s in sets.items() if not s["known"]}
    return {"name": d.get("m_Name"), "sample_rate": d.get("m_SampleRate"),
            "bindings": len(bindings), "curves_needed_by_bindings": needed,
            "curves_in_known_sets": known, "curve_sets": sets,
            "unknown_sets_with_curves": {k: v for k, v in unknown.items() if v},
            "unknown_sets_without_count": sorted(k for k, v in unknown.items() if v is None),
            "counts_add_up": known == needed}


def _streamed_frames(data: list[int]) -> list[tuple[float, list[tuple[int, tuple[float, float, float, float]]]]]:
    raw = struct.pack(f"<{len(data)}I", *[int(x) & 0xFFFFFFFF for x in data])
    frames, pos = [], 0
    while pos + 8 <= len(raw):
        time, count = struct.unpack_from("<fi", raw, pos)
        pos += 8
        if count < 0 or pos + count * 20 > len(raw):
            raise ToolkitError(f"streamed clip data malformed at byte {pos}")
        keys = []
        for _ in range(count):
            index, c0, c1, c2, c3 = struct.unpack_from("<i4f", raw, pos)
            pos += 20
            keys.append((index, (c0, c1, c2, c3)))
        frames.append((time, keys))
    return frames


class _Curves:
    """Evaluates every curve index of a compiled clip at a given time."""

    def __init__(self, data: dict[str, Any]):
        streamed = data.get("m_StreamedClip") or {}
        dense = data.get("m_DenseClip") or {}
        self.n_streamed = int(streamed.get("curveCount") or 0)
        self.n_dense = int(dense.get("m_CurveCount") or 0)
        self.constant = [float(v) for v in (data.get("m_ConstantClip") or {}).get("data") or []]
        self.segments: dict[int, list[tuple[float, tuple[float, float, float, float]]]] = {}
        for time, keys in _streamed_frames(streamed.get("data") or []):
            for index, coeffs in keys:
                self.segments.setdefault(index, []).append((time, coeffs))
        self.dense_rate = float(dense.get("m_SampleRate") or 0) or 30.0
        self.dense_begin = float(dense.get("m_BeginTime") or 0)
        self.dense_frames = int(dense.get("m_FrameCount") or 0)
        self.dense = [float(v) for v in dense.get("m_SampleArray") or []]
        if self.n_dense and len(self.dense) < self.dense_frames * self.n_dense:
            raise ToolkitError("dense clip has fewer samples than frames x curves")

    @property
    def count(self) -> int:
        return self.n_streamed + self.n_dense + len(self.constant)

    def value(self, index: int, t: float) -> float:
        if index < self.n_streamed:
            segs = self.segments.get(index)
            if not segs:
                raise ToolkitError(f"streamed curve {index} has no keys")
            real = [s for s in segs if abs(s[0]) < _SENTINEL] or segs
            seg = real[0]
            for s in real:
                if s[0] <= t:
                    seg = s
                else:
                    break
            c0, c1, c2, c3 = seg[1]
            if t < seg[0] or abs(seg[0]) >= _SENTINEL:
                return c3
            dt = t - seg[0]
            return ((c0 * dt + c1) * dt + c2) * dt + c3
        index -= self.n_streamed
        if index < self.n_dense:
            f = (t - self.dense_begin) * self.dense_rate
            f = min(max(f, 0.0), max(self.dense_frames - 1, 0))
            lo = int(math.floor(f))
            hi = min(lo + 1, self.dense_frames - 1)
            a, b = self.dense[lo * self.n_dense + index], self.dense[hi * self.n_dense + index]
            return a + (b - a) * (f - lo)
        index -= self.n_dense
        if index < len(self.constant):
            return self.constant[index]
        raise ToolkitError(f"curve index {index} beyond the clip's curves")


QUANTIZED_SET = "m_QuantizedClip"


def _known_rows(curves: _Curves, times: list[float]):
    import numpy as np

    t = np.asarray(times, dtype=float)
    streamed = np.array([[curves.value(i, x) for x in times] for i in range(curves.n_streamed)]).reshape(
        curves.n_streamed, len(times))
    dense = np.array([[curves.value(curves.n_streamed + i, x) for x in times] for i in range(curves.n_dense)]
                     ).reshape(curves.n_dense, len(times))
    constant = np.repeat(np.asarray(curves.constant, dtype=float).reshape(-1, 1), len(t), axis=1)
    return {"streamed": streamed, "dense": dense, "constant": constant}


def _rotation_rows(bindings: list[dict[str, Any]], widths: list[int]) -> list[list[int]]:
    rows, index = [], 0
    for b, w in zip(bindings, widths, strict=True):
        if int(b.get("typeID", 0) or 0) == TRANSFORM_CLASS_ID and int(b.get("attribute", 0) or 0) == 2:
            rows.append([index, index + 1, index + 2, index + 3])
        index += w
    return rows


def decode_clip(d: dict[str, Any], hash_to_path: dict[int, str],
                rest: dict[str, dict[str, list[float]]] | None = None) -> dict[str, Any]:
    """Parsed AnimationClip dict (UnityPy ``parse_as_dict``) -> ``aniimo-pal/clip`` document.

    ``hash_to_path``: binding path hash -> transform path (Avatar TOS or skeleton paths).
    Raises :class:`ClipNotDecodable` when curves live in a set this decoder does not know, or when Aniimo's
    ``m_QuantizedClip`` cannot be read with confidence (see :mod:`aniimo_pal.convert.quantized_clip`).
    """
    import numpy as np

    report = structure_report(d)
    unknown = dict(report["unknown_sets_with_curves"])
    quantized = unknown.pop(QUANTIZED_SET, None)
    if unknown:
        raise ClipNotDecodable("curves stored in a format not decoded yet: " + ", ".join(
            f"{k} ({v} curves)" for k, v in unknown.items()), report)
    data = _clip_data(d)
    curves = _Curves(data)
    bindings = (d.get("m_ClipBindingConstant") or {}).get("genericBindings") or []
    widths = binding_widths(bindings)
    total = sum(widths)
    if curves.count + (quantized or 0) != total:
        raise ClipNotDecodable(f"curve count {curves.count + (quantized or 0)} does not match the bindings "
                               f"({total} curves); the clip has curves this decoder does not see", report)
    muscle = d.get("m_MuscleClip") or {}
    rate = float(d.get("m_SampleRate") or 30.0) or 30.0
    start = float(muscle.get("m_StartTime") or 0.0)
    stop = float(muscle.get("m_StopTime") or start)
    n = max(0, int(round((stop - start) * rate)))
    times = [start + i / rate for i in range(n + 1)]
    known = _known_rows(curves, times)
    decoder: dict[str, Any] = {"curves": {"streamed": curves.n_streamed, "dense": curves.n_dense,
                                          "constant": len(curves.constant)},
                               "sampled_frames": len(times), "provenance": "verified (decoded from the built clip)"}
    if quantized:
        from aniimo_pal.convert import quantized_clip as qz

        try:
            parsed = qz.parse(data[QUANTIZED_SET])
        except (KeyError, ValueError) as exc:
            raise ClipNotDecodable(f"{QUANTIZED_SET}: layout not understood ({exc})", report) from exc
        found = qz.search(parsed, known, np.asarray(times), _rotation_rows(bindings, widths), total)
        best = found["best"]
        report["quantized_search"] = {k: found[k] for k in ("tried", "top", "runner_up_score")}
        if best is None or best["score"] < qz.ACCEPT_SCORE:
            raise ClipNotDecodable(f"{QUANTIZED_SET}: no reading makes every rotation a unit quaternion "
                                   f"(best {best['score'] if best else 0:.3f})", report)
        rivals = [r for r in found["candidates"][1:] if r["score"] >= qz.ACCEPT_SCORE
                  and not np.allclose(best["matrix"], r["matrix"], atol=1e-4, equal_nan=True)]
        if rivals:
            report["quantized_search"]["differing_readings_that_also_fit"] = [r["reading"] for r in rivals[:5]]
            raise ClipNotDecodable(f"{QUANTIZED_SET}: {len(rivals) + 1} different readings fit; not choosing one",
                                   report)
        matrix = best["matrix"]
        decoder["curves"]["quantized"] = quantized
        decoder["quantized_reading"] = dict(best["reading"], score=round(best["score"], 4))
        decoder["provenance"] = ("inferred: QuantizedClip layout derived from the data; reading chosen because "
                                 "every rotation evaluates to a unit quaternion")
    else:
        matrix = np.vstack([known["streamed"], known["dense"], known["constant"]]) if total else \
            np.zeros((0, len(times)))
    tracks, float_tracks, unknown_hashes = [], [], set()
    index = 0
    for b, width in zip(bindings, widths, strict=True):
        h = int(b.get("path") or 0)
        path = hash_to_path.get(h)
        attr = int(b.get("attribute", 0) or 0)
        is_transform = int(b.get("typeID", 0) or 0) == TRANSFORM_CLASS_ID and attr in ATTR_CHANNEL
        block = matrix[index:index + width]
        index += width
        if path is None:
            unknown_hashes.add(h)
            path = f"hash:{h}"
        keys = [{"t": t, "v": [float(x) for x in block[:, f]], "in": [0.0] * width, "out": [0.0] * width}
                for f, t in enumerate(times)]
        if is_transform:
            channel = ATTR_CHANNEL[attr][0]
            if channel == "rotation":
                for k in keys:
                    norm = math.sqrt(sum(c * c for c in k["v"])) or 1.0
                    k["v"] = [c / norm for c in k["v"]]
            tracks.append({"path": path, "channel": channel, "keys": keys, "pre_infinity": None, "post_infinity": None})
        else:
            float_tracks.append({"path": path, "attribute": b.get("attribute"), "class_id": b.get("typeID"),
                                 "custom_type": b.get("customType"), "keys": keys})
    warnings = []
    if unknown_hashes:
        warnings.append(f"{len(unknown_hashes)} bound path hash(es) not found in the skeleton")
    clip = {
        "schema": SCHEMA_ID, "schema_version": SCHEMA_VERSION, "name": d.get("m_Name"), "space": "unity-local",
        "sample_rate": rate, "start_time": start, "stop_time": stop,
        "loop": bool(muscle.get("m_LoopTime", 0)), "legacy": bool(d.get("m_Legacy", 0)),
        "tracks": tracks, "float_tracks": float_tracks,
        "events": [{"time": float(e.get("time", 0)), "function": e.get("functionName"), "data": e.get("data"),
                    "float": e.get("floatParameter"), "int": e.get("intParameter")} for e in d.get("m_Events") or []],
        "warnings": warnings, "source": "unitypy-muscle-clip", "decoder": decoder,
    }
    decoder["validation"] = validate_clip(clip, rest)
    return clip


def hash_lookup(paths: list[str], root_name: str | None = None, tos: list[tuple[int, str]] | None = None) -> dict[int, str]:
    """Path hash -> skeleton path. Avatar TOS entries (verified) win over hashes computed from paths."""
    from aniimo_pal.catalog.builder import path_hash

    out: dict[int, str] = {}
    for p in paths:
        out.setdefault(path_hash(p), p)
        if root_name:
            out.setdefault(path_hash(f"{root_name}/{p}"), p)
    for h, p in tos or []:
        out[int(h)] = p
    return out


def avatar_tables(d: dict[str, Any]) -> dict[str, Any]:
    """From a parsed Avatar: TOS (path hash -> path) and the skeleton rest pose per path hash (verified data)."""
    tos = [(int(h), str(p)) for h, p in d.get("m_TOS") or [] if p is not None]
    rest: dict[int, dict[str, list[float]]] = {}
    try:
        avatar = d.get("m_Avatar") or {}
        ids = (avatar.get("m_AvatarSkeleton") or {}).get("m_ID") or []
        xs = (avatar.get("m_AvatarSkeletonPose") or {}).get("m_X") or []
        for h, x in zip(ids, xs, strict=False):
            t, q, s = x.get("t") or {}, x.get("q") or {}, x.get("s") or {}
            rest[int(h)] = {"position": [t.get("x", 0.0), t.get("y", 0.0), t.get("z", 0.0)],
                            "rotation": [q.get("x", 0.0), q.get("y", 0.0), q.get("z", 0.0), q.get("w", 1.0)],
                            "scale": [s.get("x", 1.0), s.get("y", 1.0), s.get("z", 1.0)]}
    except Exception:  # an Avatar layout this code does not know: no rest pose, the TOS still names bones
        rest = {}
    return {"tos": tos, "rest_by_hash": rest}


def validate_clip(clip: dict[str, Any], rest: dict[str, dict[str, list[float]]] | None = None) -> dict[str, Any]:
    """Checks beyond unit quaternions: bones named, continuity, positions/scales against the rest pose, motion.

    ``rest``: {path: {"position", "rotation", "scale"}} in Unity local space (Avatar pose or prefab transforms).
    Wrong curve mapping shows up as unnamed bones, frame-to-frame jumps, bone offsets far from their rest
    length, or scales far from rest. These are plausibility checks, not proof; the motion summary is meant
    to be compared with the game.
    """
    import numpy as np

    tracks = clip["tracks"]
    named = [t for t in tracks if not t["path"].startswith("hash:")]
    out: dict[str, Any] = {"tracks": len(tracks), "tracks_on_named_bones": len(named),
                           "float_tracks": len(clip.get("float_tracks") or [])}
    rot_jumps, worst_jump, motion = 0, 0.0, []
    steps = 0
    for t in tracks:
        v = np.array([k["v"] for k in t["keys"]], dtype=float)
        if len(v) < 2:
            continue
        if t["channel"] == "rotation":
            dots = np.clip(np.abs((v[1:] * v[:-1]).sum(axis=1)), 0.0, 1.0)
            angles = np.degrees(2 * np.arccos(dots))
            steps += len(angles)
            rot_jumps += int((angles > 30.0).sum())
            worst_jump = max(worst_jump, float(angles.max()))
            from_start = np.degrees(2 * np.arccos(np.clip(np.abs(v @ v[0]), 0.0, 1.0)))
            motion.append((float(from_start.max()), t["path"]))
    out["rotation_steps_over_30_deg"] = rot_jumps
    out["rotation_steps"] = steps
    out["largest_rotation_step_deg"] = round(worst_jump, 2)
    out["most_moving_bones"] = [{"path": p, "max_rotation_deg": round(a, 1)} for a, p in sorted(motion)[::-1][:8]]
    if rest:
        ratios, scale_ok, scale_n, rot_ok, rot_n = [], 0, 0, 0, 0
        for t in tracks:
            r = rest.get(t["path"])
            if not r:
                continue
            first = np.array(t["keys"][0]["v"], dtype=float)
            if t["channel"] == "position":
                rest_len = float(np.linalg.norm(r["position"]))
                if rest_len > 1e-4:
                    ratios.append(float(np.linalg.norm(first)) / rest_len)
            elif t["channel"] == "scale":
                values = np.array([k["v"] for k in t["keys"]], dtype=float)
                ref = np.array(r["scale"], dtype=float)
                with np.errstate(all="ignore"):
                    rel = values / np.where(np.abs(ref) > 1e-6, ref, 1.0)
                scale_ok += int(((rel > 0.5) & (rel < 2.0)).all(axis=1).sum())
                scale_n += len(values)
            elif t["channel"] == "rotation":
                q0 = np.array(r["rotation"], dtype=float)
                angle = float(np.degrees(2 * np.arccos(min(1.0, abs(float(first @ q0))))))
                rot_n += 1
                rot_ok += angle < 90.0
        if ratios:
            arr = np.array(ratios)
            out["bone_offsets_vs_rest"] = {"tracks": len(arr), "within_20_percent": int(((arr > 0.8) & (arr < 1.25)).sum()),
                                           "median_ratio": round(float(np.median(arr)), 3)}
        if scale_n:
            out["scale_samples_within_half_to_double_of_rest"] = f"{scale_ok}/{scale_n}"
        if rot_n:
            out["rotations_within_90_deg_of_rest_at_frame_0"] = f"{rot_ok}/{rot_n}"
    return out
