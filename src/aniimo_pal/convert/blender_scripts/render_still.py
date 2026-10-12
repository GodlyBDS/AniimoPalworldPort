"""Blender job: render one still frame of a .blend with the Workbench engine.

Usage: blender -b scene.blend --factory-startup --python render_still.py -- out.png FRAME
Kept separate from scene building because rendering needs an OpenGL context,
which some machines (servers, VMs) lack; a failure here never affects the scene.
"""

import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
out, frame = args[0], int(args[1])
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"  # colour mode (texture/material) is stored in the scene
scene.render.resolution_x, scene.render.resolution_y = 1280, 540
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = out
scene.frame_set(frame)
bpy.ops.render.render(write_still=True)
