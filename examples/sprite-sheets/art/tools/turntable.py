"""Render props from eight directions, in each colourway, for a sprite sheet.

    blender -b --factory-startup --python-exit-code 1 --python tools/turntable.py -- \
        OUT SIZE SCENE [SCENE ...]

For each scene and each colour in `common.COLOURS`, builds `scene.build(colour)`
and renders it rotated 0, 45, ... 315 degrees about Z into
`OUT/<scene>_<colour>_<angle>.png`, SIZE pixels square.

Everything that could make two frames differ, except the prop's rotation, is
held fixed, because a sprite sheet's frames are compared with each other:

- one orthographic camera, looking down at 35 degrees from the -Y side;
- one scale per scene, from the prop's largest extent over all eight
  rotations, so a prop does not grow or shrink as it turns, and the same
  scale for every colour;
- a flat #ff00ff background, anti-aliasing off and dithering off, so the
  background keys out exactly and edges stay hard;
- Workbench studio lighting in material colour, cavity shading off: its fine
  highlights snap to scattered palette entries and read as grain at 64x64.
"""

import importlib
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scenes import common  # noqa: E402

ANGLES = [45 * step for step in range(8)]
ELEVATION = math.radians(35)


def setup(size):
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.dither_intensity = 0.0
    scene.display.render_aa = "OFF"
    scene.view_settings.view_transform = "Standard"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "MATERIAL"
    shading.show_cavity = False
    world = bpy.data.worlds.new("key")
    world.color = (1.0, 0.0, 1.0)
    scene.world = world
    return scene


def extent(obj):
    """Largest distance of any vertex from the prop's vertical axis, and its height."""
    radius = max(math.hypot(v.co.x, v.co.y) for v in obj.data.vertices)
    top = max(v.co.z for v in obj.data.vertices)
    return radius, top


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    out, size, names = args[0], int(args[1]), args[2:]
    os.makedirs(out, exist_ok=True)
    for name in names:
        module = importlib.import_module(f"scenes.{name}")
        for colour in common.COLOURS:
            common.clear_scene()
            scene = setup(size)
            obj = module.build(colour)
            radius, top = extent(obj)
            data = bpy.data.cameras.new("turntable")
            camera = bpy.data.objects.new("turntable", data)
            scene.collection.objects.link(camera)
            scene.camera = camera
            data.type = "ORTHO"
            # What fills the frame is the prop's projection, not its height:
            # seen from 35 degrees up, a cylinder of `radius` and `top` spans
            # 2 * radius across and top * cos + 2 * radius * sin down the frame.
            # Every rotation fits inside that cylinder, so one scale serves all.
            across = 2 * radius
            down = top * math.cos(ELEVATION) + 2 * radius * math.sin(ELEVATION)
            data.ortho_scale = max(across, down) * 1.1
            centre = Vector((0.0, 0.0, top / 2))
            direction = Vector((0.0, -math.cos(ELEVATION), math.sin(ELEVATION)))
            camera.location = centre + direction * 10
            data.clip_end = 30
            camera.rotation_euler = (centre - camera.location).to_track_quat("-Z", "Y").to_euler()
            for angle in ANGLES:
                obj.rotation_euler = (0.0, 0.0, math.radians(angle))
                scene.render.filepath = os.path.join(out, f"{name}_{colour}_{angle:03d}.png")
                bpy.ops.render.render(write_still=True)
            print(f"turntable: {name} {colour}, {len(ANGLES)} frames")


main()
