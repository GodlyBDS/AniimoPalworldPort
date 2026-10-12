"""Unity AnimationClip YAML (``.anim``) -> neutral clip JSON, plus a reference evaluator.

Input: ``.anim`` files written by AssetRipper's Unity-project export. AssetRipper
rebuilds editor curves from a build's compiled (muscle) clip data. Output: an
``aniimo-pal/clip`` document that stays in **Unity local space** (left-handed,
Y-up, metres, parent-relative transforms). Converting to another engine's space
is a separate, testable step (``convert/blender_scripts``), so this module has no
axis conventions baked in.

Curves are cubic Hermite per component, using the keys' in/out slopes, as in
Unity for ``weightedMode: 0``. Quaternion samples are normalised after
interpolation. Weighted tangents are recorded but evaluated as unweighted (a
warning is added).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ToolkitError

SCHEMA_ID = "aniimo-pal/clip"
SCHEMA_VERSION = "0.1.0"

CHANNELS = {
    "m_PositionCurves": ("position", ("x", "y", "z")),
    "m_RotationCurves": ("rotation", ("x", "y", "z", "w")),
    "m_EulerCurves": ("euler", ("x", "y", "z")),
    "m_ScaleCurves": ("scale", ("x", "y", "z")),
}


def _yaml():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ToolkitError("PyYAML is required: pip install 'aniimo-palworld-toolkit[convert]'") from exc

    class UnityLoader(yaml.SafeLoader):
        pass

    def unity_tag(loader, _suffix, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    UnityLoader.add_multi_constructor("tag:unity3d.com,2011:", unity_tag)
    return yaml, UnityLoader


def load_unity_yaml(text: str) -> list[dict[str, Any]]:
    """All documents of a Unity YAML file (``--- !u!<classID> &<fileID>`` sections)."""
    yaml, loader = _yaml()
    # Unity writes "--- !u!1001 &123 stripped" for stripped objects; drop the suffix.
    cleaned = "\n".join(line[:-9] if line.startswith("--- !u!") and line.endswith(" stripped") else line
                        for line in text.splitlines())
    return [d for d in yaml.load_all(cleaned, Loader=loader) if d]


def _vec(value: Any, comps: tuple[str, ...]) -> list[float]:
    if isinstance(value, dict):
        return [float(value.get(c, 0.0)) for c in comps]
    return [float(value)] * len(comps)  # infinite slopes are written as scalars in some exports


def parse_anim(text: str, source: str | None = None) -> dict[str, Any]:
    docs = load_unity_yaml(text)
    clip = next((d["AnimationClip"] for d in docs if "AnimationClip" in d), None)
    if clip is None:
        raise ToolkitError(f"{source or 'input'} contains no AnimationClip")
    warnings: list[str] = []
    tracks = []
    for field, (channel, comps) in CHANNELS.items():
        for entry in clip.get(field) or []:
            curve = (entry.get("curve") or {})
            keys = []
            for k in curve.get("m_Curve") or []:
                if int(k.get("weightedMode", 0) or 0) != 0:
                    warnings.append(f"{entry.get('path')}/{channel}: weighted tangents evaluated as unweighted")
                keys.append({"t": float(k["time"]), "v": _vec(k["value"], comps),
                             "in": _vec(k.get("inSlope", 0.0), comps), "out": _vec(k.get("outSlope", 0.0), comps)})
            keys.sort(key=lambda k: k["t"])
            track = {"path": str(entry.get("path") or ""), "channel": channel, "keys": keys,
                     "pre_infinity": curve.get("m_PreInfinity"), "post_infinity": curve.get("m_PostInfinity")}
            if channel == "euler":
                track["rotation_order"] = curve.get("m_RotationOrder")
            tracks.append(track)
    if clip.get("m_CompressedRotationCurves"):
        warnings.append("m_CompressedRotationCurves present but not decoded")
    float_tracks = []
    for entry in clip.get("m_FloatCurves") or []:
        keys = [{"t": float(k["time"]), "v": [float(k["value"])], "in": [float(k.get("inSlope", 0) or 0)],
                 "out": [float(k.get("outSlope", 0) or 0)]} for k in (entry.get("curve") or {}).get("m_Curve") or []]
        float_tracks.append({"path": str(entry.get("path") or ""), "attribute": entry.get("attribute"),
                             "class_id": entry.get("classID"), "keys": sorted(keys, key=lambda k: k["t"])})
    settings = clip.get("m_AnimationClipSettings") or {}
    times = [k["t"] for t in tracks for k in t["keys"]]
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "name": clip.get("m_Name"),
        "space": "unity-local",
        "sample_rate": float(clip.get("m_SampleRate") or 30.0),
        "start_time": float(settings.get("m_StartTime", min(times, default=0.0))),
        "stop_time": float(settings.get("m_StopTime", max(times, default=0.0))),
        "loop": bool(settings.get("m_LoopTime", 0)),
        "legacy": bool(clip.get("m_Legacy", 0)),
        "tracks": tracks,
        "float_tracks": float_tracks,
        # Events are verified source data (hit frames, VFX/SFX triggers) used by combat translation.
        "events": [{"time": float(e.get("time", 0)), "function": e.get("functionName"), "data": e.get("data"),
                    "float": e.get("floatParameter"), "int": e.get("intParameter")}
                   for e in clip.get("m_Events") or []],
        "warnings": sorted(set(warnings)),
        "source": source,
    }


def parse_anim_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return parse_anim(p.read_text(encoding="utf-8"), source=p.name)


def _hermite(k0: dict[str, Any], k1: dict[str, Any], t: float, i: int) -> float:
    dt = k1["t"] - k0["t"]
    if dt <= 0:
        return k1["v"][i]
    s = (t - k0["t"]) / dt
    m0, m1 = k0["out"][i] * dt, k1["in"][i] * dt
    if not (math.isfinite(m0) and math.isfinite(m1)):  # stepped (infinite) tangents
        return k0["v"][i]
    s2, s3 = s * s, s * s * s
    return ((2 * s3 - 3 * s2 + 1) * k0["v"][i] + (s3 - 2 * s2 + s) * m0
            + (-2 * s3 + 3 * s2) * k1["v"][i] + (s3 - s2) * m1)


def evaluate_track(track: dict[str, Any], t: float) -> list[float]:
    """Value of a track at time ``t`` (clamped outside the key range)."""
    keys = track["keys"]
    if not keys:
        raise ValueError("track has no keys")
    if t <= keys[0]["t"]:
        out = list(keys[0]["v"])
    elif t >= keys[-1]["t"]:
        out = list(keys[-1]["v"])
    else:
        hi = next(i for i, k in enumerate(keys) if k["t"] > t)
        k0, k1 = keys[hi - 1], keys[hi]
        out = [_hermite(k0, k1, t, i) for i in range(len(k0["v"]))]
    if track.get("channel") == "rotation":
        n = math.sqrt(sum(c * c for c in out)) or 1.0
        out = [c / n for c in out]
    return out


def sample_times(clip: dict[str, Any]) -> list[float]:
    """Frame times from start to stop at the clip's sample rate (inclusive)."""
    rate = clip["sample_rate"] or 30.0
    start, stop = clip["start_time"], clip["stop_time"]
    n = max(0, int(round((stop - start) * rate)))
    return [start + i / rate for i in range(n + 1)]
