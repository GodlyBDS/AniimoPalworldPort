"""Blender job: build a preview scene of a converted creature (all variants, all animations).

Runs inside Blender (``blender -b --factory-startup --python preview_scene.py -- job.json``)
or through the ``bpy`` module. Only uses bpy and the standard library.

Job JSON keys:
  fbx            converted creature FBX (rig + mesh + one action per clip)
  variants       [{"label": "Normal", "texture": "path/to/albedo.png" | null}, ...]
  output_blend   .blend file to write (open it in Blender to play/inspect)
  report         (optional) JSON report path

Scene layout: one copy of the creature per variant, side by side, each with
its own material (the variant's main texture) and a text label. All actions are
placed one after another on every copy's NLA timeline, with a timeline marker
naming each action, so pressing Play shows every animation on every variant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.armatures, bpy.data.materials,
                       bpy.data.actions, bpy.data.images, bpy.data.cameras, bpy.data.lights, bpy.data.curves):
        for item in list(collection):
            collection.remove(item)


def variant_material(label: str, texture: str | None):
    mat = bpy.data.materials.new(f"Preview_{label}")
    if mat.node_tree is None and hasattr(mat, "use_nodes"):  # Blender < 5 creates node trees on request
        mat.use_nodes = True
    tree = mat.node_tree
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if tree else None
    if texture and Path(texture).is_file():
        img = bpy.data.images.load(texture, check_existing=True)
        if tree and bsdf:
            node = tree.nodes.new("ShaderNodeTexImage")
            node.image = img
            tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
        # Viewport/material colour = average texture colour (used when a mesh has no UVs).
        px = list(img.pixels)
        n = len(px) // 4
        step = max(1, n // 4096)
        samples = [px[i * 4:i * 4 + 4] for i in range(0, n, step)]
        if samples:
            mat.diffuse_color = [sum(c[k] for c in samples) / len(samples) for k in range(3)] + [1.0]
    return mat


def bounds(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs if o.type == "MESH" for c in o.bound_box]
    if not pts:
        return Vector((0, 0, 0)), Vector((1, 1, 1))
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    return lo, hi


def run(job: dict) -> dict:
    reset_scene()
    bpy.ops.import_scene.fbx(filepath=job["fbx"])
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if arm is None or not meshes:
        raise RuntimeError("the FBX has no armature or no mesh")
    scene = bpy.context.scene

    # Sequential NLA layout with markers: every action plays one after another.
    actions = sorted(bpy.data.actions, key=lambda a: a.name)
    for act in actions:  # FBX takes import as "Armature|...|ClipName"; show the clip name
        act.name = act.name.rsplit("|", 1)[-1]
    if arm.animation_data is None:
        arm.animation_data_create()
    arm.animation_data.action = None
    track = arm.animation_data.nla_tracks.new()
    track.name = "All animations"
    frame, timeline = 1, []
    for act in actions:
        start, end = act.frame_range
        length = max(1, int(round(end - start)))
        strip = track.strips.new(act.name, int(frame), act)
        strip.action_frame_start, strip.action_frame_end = start, end
        scene.timeline_markers.new(act.name, frame=int(frame))
        timeline.append({"action": act.name, "start": int(frame), "end": int(frame + length)})
        frame += length + 10  # small gap between animations
    scene.frame_start, scene.frame_end = 1, max(2, int(frame))

    lo, hi = bounds(meshes)
    size = hi - lo
    spacing = max(size.x, size.y, 0.5) * 1.6
    variants = job.get("variants") or [{"label": "Normal", "texture": None}]
    placed = []
    holders = []
    for i, var in enumerate(variants):
        mat = variant_material(var["label"], var.get("texture"))
        if i == 0:
            a, ms = arm, meshes
        else:
            a = arm.copy()
            a.data = arm.data  # share the rig data; animation (NLA) is copied with the object
            scene.collection.objects.link(a)
            ms = []
            for m in meshes:
                c = m.copy()
                c.data = m.data.copy()
                scene.collection.objects.link(c)
                c.parent = a
                for mod in c.modifiers:
                    if mod.type == "ARMATURE":
                        mod.object = a
                ms.append(c)
        # Each variant hangs under its own offset parent: the imported animation may also move the
        # armature object itself, which would otherwise pull every copy back to the origin.
        holder = bpy.data.objects.new(f"Variant_{var['label']}", None)
        scene.collection.objects.link(holder)
        holder.location.x = spacing * i
        a.parent = holder
        holders.append(holder)
        for m in ms:
            m.data.materials.clear()
            m.data.materials.append(mat)
        curve = bpy.data.curves.new(f"Label_{var['label']}", "FONT")
        curve.body = var["label"]
        curve.size = max(size.z, 0.3) * 0.18
        curve.align_x = "CENTER"
        text = bpy.data.objects.new(f"Label_{var['label']}", curve)
        scene.collection.objects.link(text)
        text.location = Vector(((lo.x + hi.x) / 2 + spacing * i, (lo.y + hi.y) / 2, hi.z + size.z * 0.15 + 0.05))
        text.rotation_euler = (1.5708, 0, 0)
        placed.append({"label": var["label"], "texture": var.get("texture"), "armature": a.name})

    # Orthographic front camera framing every variant and its label.
    width = spacing * (len(variants) - 1) + max(size.x, size.y, 0.3)
    height = (hi.z - lo.z) * 1.35 + 0.1
    center = Vector(((lo.x + hi.x) / 2 + spacing * (len(variants) - 1) / 2, (lo.y + hi.y) / 2,
                     lo.z + height / 2))
    cam_data = bpy.data.cameras.new("PreviewCamera")
    cam_data.type = "ORTHO"
    aspect = 1280 / 540
    cam_data.ortho_scale = max(width * 1.15, height * aspect * 1.15)
    cam = bpy.data.objects.new("PreviewCamera", cam_data)
    scene.collection.objects.link(cam)
    cam.location = center + Vector((0, -max(width, height) * 3, 0))
    cam.rotation_euler = (1.5708, 0, 0)
    cam_data.clip_end = max(width, height) * 10
    scene.camera = cam
    light = bpy.data.objects.new("PreviewLight", bpy.data.lights.new("PreviewLight", "SUN"))
    scene.collection.objects.link(light)
    light.rotation_euler = (0.8, 0.2, 0.6)
    scene.render.resolution_x, scene.render.resolution_y = 1280, 540
    scene.render.engine = "BLENDER_WORKBENCH"
    has_uv = all(len(o.data.uv_layers) > 0 for o in scene.objects if o.type == "MESH")
    scene.display.shading.color_type = "TEXTURE" if has_uv else "MATERIAL"
    scene.render.fps = int(job.get("fps", 30))

    out = Path(job["output_blend"])
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), copy=True)

    # The still image is rendered by a separate Blender run (render_still.py), so a machine without
    # OpenGL cannot crash the scene build.
    hot = next((t for t in timeline if any(k in t["action"].lower() for k in ("ult", "attack", "skill"))), None)
    render_frame = (hot or (timeline[0] if timeline else {"start": 1}))["start"] + 5
    report = {"schema": "aniimo-pal/preview-report", "blend": str(out), "variants": placed, "timeline": timeline,
              "frame_end": scene.frame_end, "render_frame": render_frame, "render": None, "render_error": None,
              "blender_version": bpy.app.version_string}
    if job.get("report"):
        Path(job["report"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    try:
        report = run(job)
    except Exception as exc:
        if job.get("report"):
            Path(job["report"]).write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")
        print(f"preview failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {report['blend']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
