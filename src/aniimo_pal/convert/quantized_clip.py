"""Aniimo's ``m_QuantizedClip`` (not in stock Unity): layout derived from the data, choice validated by the data.

What is known from a real clip (``Ani_Parmon_10052_Attack01``, ``probe clip``)::

    m_FrameCount 84, m_CurveCount 709, m_NumStatic 236, m_NumDynamic 473, m_SampleRate 60
    m_TypeOffset 0, m_IndicesOffset 712, m_StaticOffset 3548, m_DynamicOffset 4492, m_FrameSize 176
    m_Data: 87740 bytes

and the offsets add up exactly:

* types:   bytes [0, 709) -> one byte per curve, padded to 712;
* indices: [712, 3548)    -> 709 x int32 = 2836 bytes;
* static:  [3548, 4492)   -> 236 x float32 = 944 bytes;
* dynamic: [4492, 87740)  -> 473 blocks of m_FrameSize = 176 bytes = 2 x float32 + 84 x uint16.

That much is arithmetic. What the type byte and the index mean, how a uint16 sample becomes a value, and
where the 709 curves sit among the clip's other curves (dense, constant) is NOT documented. Each plausible
reading is tried and scored against the data itself: every Transform rotation binding must evaluate to a
unit quaternion in every frame. A reading is used only when it scores (almost) perfectly and clearly better
than the alternatives; the report names it. Otherwise the clip is refused.
"""

from __future__ import annotations

import itertools
import struct
from typing import Any

import numpy as np

ACCEPT_SCORE = 0.995  # fraction of rotation samples with | |q| - 1 | < tolerance
NORM_TOLERANCE = 0.02


def parse(qc: dict[str, Any]) -> dict[str, Any]:
    """Split ``m_Data`` into its sections; raises ValueError when the offsets do not fit."""
    data = bytes(int(b) & 0xFF for b in qc.get("m_Data") or [])
    n, ns, nd = int(qc["m_CurveCount"]), int(qc["m_NumStatic"]), int(qc["m_NumDynamic"])
    frames, frame_size = int(qc["m_FrameCount"]), int(qc["m_FrameSize"])
    t_off, i_off, s_off, d_off = (int(qc[k]) for k in ("m_TypeOffset", "m_IndicesOffset", "m_StaticOffset",
                                                         "m_DynamicOffset"))
    if ns + nd != n:
        raise ValueError(f"static {ns} + dynamic {nd} != curves {n}")
    if not (t_off + n <= i_off and i_off + 4 * n <= s_off and s_off + 4 * ns <= d_off):
        raise ValueError("section offsets overlap")
    if d_off + nd * frame_size != len(data):
        raise ValueError(f"dynamic section size {len(data) - d_off} != {nd} x frame size {frame_size}")
    out = {"n": n, "ns": ns, "nd": nd, "frames": frames, "rate": float(qc.get("m_SampleRate") or 0) or 30.0,
           "begin": float(qc.get("m_BeginTime") or 0),
           "types": np.frombuffer(data, dtype=np.uint8, count=n, offset=t_off).astype(np.int64),
           "indices": np.frombuffer(data, dtype="<i4", count=n, offset=i_off).astype(np.int64),
           "static": np.frombuffer(data, dtype="<f4", count=ns, offset=s_off).astype(np.float64)}
    body = data[d_off:]
    layouts = {}
    if frame_size == 8 + 2 * frames:  # per curve: two floats then one uint16 per frame
        blocks = np.frombuffer(body, dtype=np.uint8).reshape(nd, frame_size)
        ab = blocks[:, :8].copy().view("<f4").astype(np.float64)
        q = blocks[:, 8:].copy().view("<u2").astype(np.float64)
        layouts["per-curve block (a, b, samples)"] = (ab[:, 0], ab[:, 1], q)
    if len(body) == nd * 8 + nd * frames * 2:  # all headers first, then samples
        ab = np.frombuffer(body, dtype="<f4", count=2 * nd).astype(np.float64).reshape(nd, 2)
        rest = np.frombuffer(body, dtype="<u2", offset=8 * nd).astype(np.float64)
        layouts["headers then frame-major samples"] = (ab[:, 0], ab[:, 1], rest.reshape(frames, nd).T)
        layouts["headers then curve-major samples"] = (ab[:, 0], ab[:, 1], rest.reshape(nd, frames))
    if not layouts:
        raise ValueError("no known layout fits the dynamic section")
    out["layouts"] = layouts
    return out


DEQUANT = {
    "a + b * q / 65535": lambda a, b, q: a[:, None] + b[:, None] * (q / 65535.0),
    "a + b * q": lambda a, b, q: a[:, None] + b[:, None] * q,
    "b + a * q / 65535": lambda a, b, q: b[:, None] + a[:, None] * (q / 65535.0),
    "b + a * q": lambda a, b, q: b[:, None] + a[:, None] * q,
}


def _kinds(p: dict[str, Any]) -> dict[str, np.ndarray]:
    """Candidate static/dynamic flags per curve (True = dynamic) whose counts match the header."""
    out = {}
    types = p["types"]
    for value in sorted(set(types.tolist())):
        flag = types == value
        if flag.sum() == p["nd"]:
            out[f"type == {value} is dynamic"] = flag
        if (~flag).sum() == p["nd"]:
            out[f"type != {value} is dynamic"] = ~flag
    order = np.zeros(p["n"], dtype=bool)
    order[p["ns"]:] = True
    out["first NumStatic curves static"] = order
    return out


def _rows(p: dict[str, Any], dynamic: np.ndarray, slot_mode: str, values: np.ndarray) -> np.ndarray | None:
    """(n, frames) samples of the quantized curves in their own order, for one reading."""
    n, frames = p["n"], p["frames"]
    rows = np.empty((n, frames))
    s_slots = np.cumsum(~dynamic) - 1
    d_slots = np.cumsum(dynamic) - 1
    if slot_mode == "index":
        idx = p["indices"]
        if idx.min() < 0 or (idx[~dynamic] >= p["ns"]).any() or (idx[dynamic] >= p["nd"]).any():
            return None
        s_slots, d_slots = idx, idx
    rows[~dynamic] = p["static"][s_slots[~dynamic]][:, None]
    rows[dynamic] = values[d_slots[dynamic]]
    return rows


def _placements(p: dict[str, Any], before: int, after: int, total: int) -> dict[str, np.ndarray]:
    """Global curve index of each quantized curve. ``before``: streamed+dense curves, ``after``: constant curves."""
    n, idx = p["n"], p["indices"]
    out = {}
    perm = len(set(idx.tolist())) == n and idx.min() >= 0 and idx.max() < n
    if len(set(idx.tolist())) == n and idx.min() >= 0 and idx.max() < total:
        out["global index = indices[k]"] = idx
    for name, base in (("after constant", before + after), ("between dense and constant", before),
                       ("before dense", 0)):
        out[f"{name}, in order"] = base + np.arange(n)
        if perm:
            out[f"{name}, at indices[k]"] = base + idx
    return out


def search(p: dict[str, Any], known: dict[str, np.ndarray], frame_times: np.ndarray,
           rotation_rows: list[list[int]], total: int) -> dict[str, Any]:
    """Try every reading; return the best with its score and the runner-up.

    ``known``: {"streamed"|"dense"|"constant": (count, len(frame_times)) arrays}; ``rotation_rows``: global
    curve indices (x, y, z, w) of each Transform rotation binding.
    """
    own_times = p["begin"] + np.arange(p["frames"]) / p["rate"]
    results = []
    n_before = known["streamed"].shape[0] + known["dense"].shape[0]
    n_const = known["constant"].shape[0]
    for (layout, (a, b, q)), (dq_name, dq), (kind_name, dynamic), slot_mode in itertools.product(
            p["layouts"].items(), DEQUANT.items(), _kinds(p).items(), ("in order", "index")):
        with np.errstate(all="ignore"):
            values = dq(a, b, q)
        rows = _rows(p, dynamic, slot_mode, values)
        if rows is None:
            continue
        sampled = np.array([np.interp(frame_times, own_times, r) for r in rows]) if len(own_times) > 1 \
            else np.repeat(rows, len(frame_times), axis=1)
        for place_name, place in _placements(p, n_before, n_const, total).items():
            if place.max() >= total:
                continue
            full = np.full((total, len(frame_times)), np.nan)
            full[place] = sampled
            rest = np.setdiff1d(np.arange(total), place)
            others = np.vstack([known["streamed"], known["dense"], known["constant"]])
            if len(rest) != others.shape[0]:
                continue
            full[rest] = others
            score = _score(full, rotation_rows)
            results.append({"score": score, "reading": {"layout": layout, "dequantize": dq_name, "kinds": kind_name,
                                                         "slots": slot_mode, "placement": place_name},
                            "matrix": full})
    results.sort(key=lambda r: -r["score"])
    best = results[0] if results else None
    runner = results[1]["score"] if len(results) > 1 else None
    return {"best": best, "runner_up_score": runner, "tried": len(results), "candidates": results,
            "top": [{"score": round(r["score"], 4), **r["reading"]} for r in results[:5]]}


def _score(full: np.ndarray, rotation_rows: list[list[int]]) -> float:
    if not rotation_rows:
        return 0.0
    quats = full[np.array(rotation_rows)]  # (bindings, 4, frames)
    with np.errstate(all="ignore"):
        norms = np.sqrt((quats ** 2).sum(axis=1))
    ok = np.isfinite(norms) & (np.abs(norms - 1.0) < NORM_TOLERANCE)
    return float(ok.mean())


def unpack_floats(raw: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))
