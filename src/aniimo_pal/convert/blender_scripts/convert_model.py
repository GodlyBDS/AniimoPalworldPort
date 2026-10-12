"""Blender job: glTF/GLB (from AssetRipper) -> FBX for Unreal Engine 5.1, with clip transfer.

Runs INSIDE Blender, either headless:

    blender -b --factory-startup --python convert_model.py -- job.json

or in-process through the ``bpy`` module (tests). It only uses bpy, mathutils,
numpy (bundled with Blender) and the standard library, so it does not need the
toolkit to be installed inside Blender.

Job JSON keys:
  input_model      path to .glb/.gltf
  output_fbx       path to write
  report           path for the JSON report
  armature_name    (optional) object name for the armature, default "Armature" (UE then drops it as a root)
  bone_map         (optional) {old_bone_name: new_bone_name}
  unity_skeleton   (optional) {unity_path: {"position": [x,y,z], "rotation": [x,y,z,w], "scale": [x,y,z]}}
                   rest local transforms in Unity space ("" = prefab root). Needed for clips.
  clips            (optional) list of aniimo-pal/clip documents (Unity local space)
  fbx              (optional) overrides for bpy.ops.export_scene.fbx keyword arguments

Clip transfer (no hard-coded axis convention):
  1. Unity rest model matrices U_rest come from the skeleton's local TRS chain.
  2. The Unity->armature basis A (similarity, reflection allowed) is fitted
     from bone rest positions: Unity joint positions vs Blender bone heads.
     The expected AssetRipper/glTF/Blender basis is tried first; a
     least-squares fit (Kabsch) is the fallback. The residual is reported.
  3. Per frame: P_b = A · U_anim_b · U_rest_b^-1 · A^-1 · B_rest_b, where B_rest
     is the bone's rest matrix in armature space. At rest this reproduces
     B_rest exactly, and it is independent of how the importer oriented bones.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

UNITY_TO_BLENDER_EXPECTED = np.array([[-1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])

DEFAULT_FBX = {
    "use_selection": False,
    "object_types": {"ARMATURE", "MESH"},
    "apply_unit_scale": True,
    "apply_scale_options": "FBX_SCALE_UNITS",
    "axis_forward": "-Z",
    "axis_up": "Y",
    "add_leaf_bones": False,
    "primary_bone_axis": "Y",
    "secondary_bone_axis": "X",
    "armature_nodetype": "NULL",
    "mesh_smooth_type": "FACE",
    "use_mesh_modifiers": True,
    "bake_anim": True,
    "bake_anim_use_all_bones": True,
    "bake_anim_use_nla_strips": False,
    "bake_anim_use_all_actions": True,
    "bake_anim_force_startend_keying": True,
    "bake_anim_simplify_factor": 0.0,
    "path_mode": "AUTO",
}


# --- Unity-space math (column vectors, same as Unity's Matrix4x4) --------------

def unity_trs(position, rotation, scale) -> np.ndarray:
    x, y, z, w = rotation
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    r = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m = np.eye(4)
    m[:3, :3] = r * np.array(scale, dtype=float)[None, :]
    m[:3, 3] = position
    return m


def unity_euler_to_quat(euler_deg, order: int | None) -> list[float]:
    """Unity euler (degrees) -> quaternion. Unity's default order applies Z, then X, then Y."""
    ex, ey, ez = (math.radians(a) / 2 for a in euler_deg)
    qx = np.array([math.sin(ex), 0, 0, math.cos(ex)])
    qy = np.array([0, math.sin(ey), 0, math.cos(ey)])
    qz = np.array([0, 0, math.sin(ez), math.cos(ez)])

    def mul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return np.array([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                         aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz])

    # RotationOrder enum: 0 XYZ, 1 XZY, 2 YZX, 3 YXZ, 4 ZXY (default), 5 ZYX; first-applied axis listed first.
    seq = {0: "xyz", 1: "xzy", 2: "yzx", 3: "yxz", 4: "zxy", 5: "zyx"}.get(order if order is not None else 4, "zxy")
    parts = {"x": qx, "y": qy, "z": qz}
    q = np.array([0.0, 0.0, 0.0, 1.0])
    for axis in seq:  # q = q_last * ... * q_first
        q = mul(parts[axis], q)
    return list(q)


def _hermite(k0, k1, t, i):
    dt = k1["t"] - k0["t"]
    if dt <= 0:
        return k1["v"][i]
    s = (t - k0["t"]) / dt
    m0, m1 = k0["out"][i] * dt, k1["in"][i] * dt
    if not (math.isfinite(m0) and math.isfinite(m1)):
        return k0["v"][i]
    s2, s3 = s * s, s * s * s
    return (2 * s3 - 3 * s2 + 1) * k0["v"][i] + (s3 - 2 * s2 + s) * m0 + (-2 * s3 + 3 * s2) * k1["v"][i] + (s3 - s2) * m1


def evaluate_track(track, t):
    keys = track["keys"]
    if t <= keys[0]["t"]:
        out = list(keys[0]["v"])
    elif t >= keys[-1]["t"]:
        out = list(keys[-1]["v"])
    else:
        hi = next(i for i, k in enumerate(keys) if k["t"] > t)
        out = [_hermite(keys[hi - 1], keys[hi], t, i) for i in range(len(keys[0]["v"]))]
    if track["channel"] == "rotation":
        n = math.sqrt(sum(c * c for c in out)) or 1.0
        out = [c / n for c in out]
    return out


def unity_model_matrices(rest: dict, overrides: dict) -> dict[str, np.ndarray]:
    """Model-space matrices for every skeleton path, applying per-path local overrides."""
    out: dict[str, np.ndarray] = {}
    for path in sorted(rest, key=lambda p: (p.count("/") + (1 if p else 0), p)):
        local = dict(rest[path])
        local.update(overrides.get(path, {}))
        m = unity_trs(local["position"], local["rotation"], local["scale"])
        parent = path.rsplit("/", 1)[0] if "/" in path else ("" if path else None)
        if parent is not None and parent in out:
            m = out[parent] @ m
        out[path] = m
    return out


# --- basis fitting ---------------------------------------------------------------

def fit_basis(src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, float, np.ndarray, str, float]:
    """Similarity dst ≈ s·R·src + t, reflection allowed. Returns (R, s, t, method, rms residual)."""
    cs, cd = src.mean(axis=0), dst.mean(axis=0)
    a, b = src - cs, dst - cd
    var = float((a * a).sum())

    def solve(r):
        s = float((b * (a @ r.T)).sum()) / var if var > 1e-12 else 1.0
        t = cd - s * (r @ cs)
        res = dst - (s * (src @ r.T) + t)
        return s, t, float(np.sqrt((res * res).sum(axis=1).mean()))

    s, t, rms = solve(UNITY_TO_BLENDER_EXPECTED)
    scale_ref = math.sqrt(var / len(src)) if var > 0 else 1.0
    if rms <= 1e-4 * max(scale_ref, 1e-6) or len(src) < 3:
        return UNITY_TO_BLENDER_EXPECTED.copy(), s, t, "expected-assetripper-basis", rms
    u, _sv, vt = np.linalg.svd(b.T @ a)
    r = u @ vt  # reflection allowed (handedness change)
    s2, t2, rms2 = solve(r)
    if rms2 < rms:
        return r, s2, t2, "kabsch-fit", rms2
    return UNITY_TO_BLENDER_EXPECTED.copy(), s, t, "expected-assetripper-basis", rms


def _to_matrix(m: np.ndarray) -> Matrix:
    return Matrix([list(row) for row in m])


# --- scene helpers -----------------------------------------------------------------

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Some builds (e.g. the bpy module) still start with default objects: remove everything.
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.armatures, bpy.data.materials,
                       bpy.data.actions, bpy.data.images, bpy.data.cameras, bpy.data.lights):
        for item in list(collection):
            collection.remove(item)


def import_model(path: str):
    ext = Path(path).suffix.lower()
    if ext in (".glb", ".gltf"):
        options = {}
        props = {p.identifier for p in bpy.ops.import_scene.gltf.get_rna_type().properties}
        if "disable_bone_shape" in props:  # newer importers add an Icosphere bone-display mesh
            options["disable_bone_shape"] = True
        bpy.ops.import_scene.gltf(filepath=path, **options)
        return options
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
        return {}
    raise ValueError(f"unsupported model format {ext}")


def model_report(warnings: list[str]) -> dict:
    meshes, armatures, materials = [], [], {}
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            me = obj.data
            groups = len(obj.vertex_groups)
            weighted = set()
            for v in me.vertices:
                if any(g.weight > 0 for g in v.groups):
                    weighted.add(v.index)
            arm_mod = any(m.type == "ARMATURE" for m in obj.modifiers)
            meshes.append({"name": obj.name, "vertices": len(me.vertices), "polygons": len(me.polygons),
                           "vertex_groups": groups, "skinned": arm_mod,
                           "unweighted_vertices": len(me.vertices) - len(weighted) if groups else None,
                           "materials": [m.name for m in me.materials if m]})
            if arm_mod and groups and len(weighted) < len(me.vertices):
                warnings.append(f"mesh {obj.name}: {len(me.vertices) - len(weighted)} vertices have no skin weight")
            for m in me.materials:
                if m and m.name not in materials:
                    images = []
                    if getattr(m, "node_tree", None) is not None:
                        images = sorted({n.image.name for n in m.node_tree.nodes
                                         if n.type == "TEX_IMAGE" and n.image is not None})
                    materials[m.name] = {"images": images}
        elif obj.type == "ARMATURE":
            armatures.append({"name": obj.name, "bones": [{"name": b.name, "parent": b.parent.name if b.parent else None}
                                                          for b in obj.data.bones]})
    corners = []
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            corners += [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    bounds = None
    if corners:
        lo = [min(c[i] for c in corners) for i in range(3)]
        hi = [max(c[i] for c in corners) for i in range(3)]
        bounds = {"min": lo, "max": hi, "size_m": [hi[i] - lo[i] for i in range(3)]}
        if max(bounds["size_m"]) > 50 or max(bounds["size_m"]) < 0.01:
            warnings.append(f"model size {bounds['size_m']} m looks wrong (expected 0.01-50 m); check unit scale")
    if not armatures:
        warnings.append("no armature found: the model will import into Unreal as a static mesh")
    return {"meshes": meshes, "armatures": armatures, "materials": materials, "bounds": bounds}


# --- clip transfer -------------------------------------------------------------------

def transfer_clip(arm_obj, clip: dict, rest: dict, path_to_bone: dict[str, str], basis) -> dict:
    r, s, t = basis
    a4 = np.eye(4)
    a4[:3, :3] = s * r
    a4[:3, 3] = t
    a4_inv = np.linalg.inv(a4)
    rest_model = unity_model_matrices(rest, {})
    bones = arm_obj.data.bones
    pose_bones = arm_obj.pose.bones

    tracks_by_path: dict[str, list] = {}
    for tr in clip["tracks"]:
        tracks_by_path.setdefault(tr["path"], []).append(tr)
    unknown_paths = sorted(p for p in tracks_by_path if p not in rest)
    unmapped = sorted(p for p in tracks_by_path if p in rest and p not in path_to_bone and p != "")

    rate = clip.get("sample_rate") or 30.0
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    start, stop = clip["start_time"], clip["stop_time"]
    n_frames = max(0, int(round((stop - start) * rate)))
    action = bpy.data.actions.new(name=clip.get("name") or "clip")
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action
    for pb in pose_bones:
        pb.rotation_mode = "QUATERNION"
    order = sorted(path_to_bone, key=lambda p: p.count("/"))
    max_err = 0.0
    for f in range(n_frames + 1):
        time = start + f / rate
        overrides: dict[str, dict] = {}
        for path, trs in tracks_by_path.items():
            o = overrides.setdefault(path, {})
            for tr in trs:
                v = evaluate_track(tr, time)
                if tr["channel"] == "euler":
                    o["rotation"] = unity_euler_to_quat(v, tr.get("rotation_order"))
                else:
                    o[tr["channel"]] = v
        anim_model = unity_model_matrices(rest, overrides)
        pose_arm: dict[str, Matrix] = {}
        frame = 1 + (time - start) * fps
        for path in order:
            bname = path_to_bone[path]
            bone = bones[bname]
            delta = a4 @ anim_model[path] @ np.linalg.inv(rest_model[path]) @ a4_inv
            target = _to_matrix(delta) @ bone.matrix_local
            parent = bone.parent
            if parent is not None and parent.name in pose_arm:
                rest_rel = parent.matrix_local.inverted() @ bone.matrix_local
                basis_m = rest_rel.inverted() @ pose_arm[parent.name].inverted() @ target
            elif parent is not None:
                basis_m = bone.matrix_local.inverted() @ target  # parent not driven: treat as rest
            else:
                basis_m = bone.matrix_local.inverted() @ target
            pose_arm[bname] = target
            loc, rot, scl = basis_m.decompose()
            pb = pose_bones[bname]
            pb.location, pb.rotation_quaternion, pb.scale = loc, rot, scl
            for prop in ("location", "rotation_quaternion", "scale"):
                pb.keyframe_insert(prop, frame=frame, group=bname)
            expected = (a4 @ anim_model[path])[:3, 3]
            head = (target @ Vector((0, 0, 0, 1)))
            max_err = max(max_err, float(np.linalg.norm(np.array(head[:3]) - expected)))
    evaluated = _evaluated_check(arm_obj, clip, rest, tracks_by_path, path_to_bone, order, a4, start, rate, fps,
                                 n_frames)
    # keep each clip as its own action for FBX export (bake_anim_use_all_actions)
    action.use_fake_user = True
    arm_obj.animation_data.action = None
    return {"name": action.name, "frames": n_frames + 1, "fps": fps, "sample_rate": rate,
            "bones_driven": len(order), "unknown_paths": unknown_paths, "unmapped_paths": unmapped,
            "events": clip.get("events", []),
            # Internal consistency only: computed from the same target matrices that were keyed.
            "max_head_error_m": max_err,
            # Independent: Blender evaluates the keyed action; bone heads in WORLD space vs the Unity pose mapped
            # by the fitted basis and the armature object's world matrix.
            "evaluated_check": evaluated}


def _evaluated_check(arm_obj, clip, rest, tracks_by_path, path_to_bone, order, a4, start, rate, fps, n_frames,
                     samples: int = 8) -> dict:
    scene = bpy.context.scene
    frames = sorted({round(i * n_frames / max(1, samples - 1)) for i in range(samples)}) if n_frames else [0]
    arm_world = np.array(arm_obj.matrix_world)
    worst, worst_bone = 0.0, None
    for f in frames:
        time = start + f / rate
        overrides: dict[str, dict] = {}
        for path, trs in tracks_by_path.items():
            o = overrides.setdefault(path, {})
            for tr in trs:
                v = evaluate_track(tr, time)
                o["rotation" if tr["channel"] == "euler" else tr["channel"]] = (
                    unity_euler_to_quat(v, tr.get("rotation_order")) if tr["channel"] == "euler" else v)
        anim_model = unity_model_matrices(rest, overrides)
        scene.frame_set(int(round(1 + (time - start) * fps)))
        bpy.context.view_layer.update()
        for path in order:
            pb = arm_obj.pose.bones[path_to_bone[path]]
            got = np.array(arm_obj.matrix_world @ pb.head)
            expected = (arm_world @ (a4 @ anim_model[path]))[:3, 3]
            err = float(np.linalg.norm(got - expected))
            if err > worst:
                worst, worst_bone = err, path
    return {"frames_checked": len(frames), "max_bone_head_error_m": worst, "worst_bone": worst_bone,
            "armature_world_is_identity": bool(np.allclose(arm_world, np.eye(4), atol=1e-6)),
            "space": "Blender world (armature object matrix applied)"}


def run(job: dict) -> dict:
    warnings: list[str] = []
    reset_scene()
    import_options = import_model(job["input_model"])
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    arm = arms[0] if arms else None
    if len(arms) > 1:
        warnings.append(f"{len(arms)} armatures found; only {arm.name!r} is processed")
    renamed = {}
    if arm is not None:
        arm.name = job.get("armature_name", "Armature")
        for old, new in (job.get("bone_map") or {}).items():
            if old in arm.data.bones:
                arm.data.bones[old].name = new
                renamed[old] = arm.data.bones[new].name
            else:
                warnings.append(f"bone_map: no bone named {old!r}")

    clips_report = []
    basis_report = None
    rest = {p: {"position": v["position"], "rotation": v["rotation"], "scale": v.get("scale", [1, 1, 1])}
            for p, v in (job.get("unity_skeleton") or {}).items()}
    if job.get("clips"):
        if arm is None or not rest:
            raise RuntimeError("clips need an armature in the model and 'unity_skeleton' rest data")
        if "" not in rest:
            rest[""] = {"position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]}
        bone_names = {b.name for b in arm.data.bones}
        path_to_bone = {}
        for path in rest:
            if not path:
                continue
            leaf = path.rsplit("/", 1)[-1]
            name = renamed.get(leaf, (job.get("bone_map") or {}).get(leaf, leaf))
            if name in bone_names:
                path_to_bone[path] = name
        if len(path_to_bone) < 1:
            raise RuntimeError("no skeleton path matches an armature bone")
        rest_model = unity_model_matrices(rest, {})
        paths = sorted(path_to_bone)
        src = np.array([rest_model[p][:3, 3] for p in paths])
        dst = np.array([list(arm.data.bones[path_to_bone[p]].head_local) for p in paths])
        r, s, t, method, rms = fit_basis(src, dst)
        basis_report = {"method": method, "scale": s, "rms_residual_m": rms, "matched_bones": len(paths),
                        "matrix": r.tolist()}
        if rms > 1e-3:
            warnings.append(f"rest pose mismatch between Unity skeleton and model (rms {rms:.4g} m); "
                            "animation transfer may be inaccurate")
        rates = {round(c.get("sample_rate") or 30.0) for c in job["clips"]}
        scene = bpy.context.scene
        scene.render.fps, scene.render.fps_base = int(max(rates)), 1.0
        if len(rates) > 1:
            warnings.append(f"clips have different sample rates {sorted(rates)}; scene uses {max(rates)} fps")
        for clip in job["clips"]:
            clips_report.append(transfer_clip(arm, clip, rest, path_to_bone, (r, s, t)))

    report = model_report(warnings)
    out = Path(job["output_fbx"])
    out.parent.mkdir(parents=True, exist_ok=True)
    fbx_args = dict(DEFAULT_FBX)
    fbx_args.update(job.get("fbx") or {})
    if isinstance(fbx_args.get("object_types"), list):
        fbx_args["object_types"] = set(fbx_args["object_types"])
    fbx_args["bake_anim"] = bool(clips_report) and fbx_args.get("bake_anim", True)
    bpy.ops.export_scene.fbx(filepath=str(out), **fbx_args)
    report.update({
        "schema": "aniimo-pal/convert-report", "schema_version": "0.1.0",
        "blender_version": bpy.app.version_string, "input_model": job["input_model"], "output_fbx": str(out),
        "import_options": import_options, "renamed_bones": renamed, "basis": basis_report, "clips": clips_report,
        "fbx_settings": {k: (sorted(v) if isinstance(v, set) else v) for k, v in fbx_args.items()},
        "warnings": warnings,
    })
    if job.get("report"):
        Path(job["report"]).parent.mkdir(parents=True, exist_ok=True)
        Path(job["report"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    if len(args) != 1:
        print("usage: blender -b --factory-startup --python convert_model.py -- job.json", file=sys.stderr)
        return 2
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    try:
        report = run(job)
    except Exception as exc:  # report failures to the caller via the report file
        if job.get("report"):
            Path(job["report"]).write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")
        print(f"conversion failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {report['output_fbx']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
