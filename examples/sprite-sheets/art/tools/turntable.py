"""Render props for a sprite sheet: eight isometric directions and four flat views.

    blender -b --factory-startup --python-exit-code 1 --python tools/turntable.py -- \
        OUT SIZE SCENE [SCENE ...]

For each scene and each colour in `common.COLOURS`, builds `scene.build(colour)`
and renders two sets of frames, SIZE pixels square:

- **isometric:** rotated 0, 45, ... 315 degrees about Z, seen from 35 degrees
  up, into `OUT/<scene>_<colour>_<angle>.png`;
- **flat:** rotated 0, 90, 180, 270 degrees, seen face-on along the prop's
  thinnest axis, into `OUT/<scene>_<colour>_flat_<angle>.png`. A prop that
  stands up (thinnest across, like the lock) is seen at eye level, so the four
  frames are its front, sides and back; a prop that lies flat (thinnest in
  height, like the key) is seen from straight above, because at eye level it
  would be a line.

Everything that could make two frames differ, except the prop's rotation, is
held fixed, because a sprite sheet's frames are compared with each other:

- one orthographic camera per set, looking from the -Y side;
- one scale per scene and set, from the prop's largest extent over all its
  rotations, so a prop does not grow or shrink as it turns, and the same
  scale for every colour;
- a flat #ff00ff background, anti-aliasing off and dithering off, so the
  background keys out exactly and edges stay hard;
- Workbench studio lighting in material colour, cavity shading off: its fine
  highlights snap to scattered palette entries and read as grain at 64x64;
- specular highlights off: a face square to the camera, as every flat view
  has, catches the full highlight and washes out to a paler palette entry
  (blue landed on light blue), while the same face seen at 35 degrees does
  not. Off, a face-on face is the same shade as the isometric lit face.
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
FLAT_ANGLES = [90 * step for step in range(4)]


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
    shading.show_specular_highlight = False
    world = bpy.data.worlds.new("key")
    world.color = (1.0, 0.0, 1.0)
    scene.world = world
    return scene


def extent(obj):
    """Largest distance of any vertex from the prop's vertical axis, and its height."""
    radius = max(math.hypot(v.co.x, v.co.y) for v in obj.data.vertices)
    top = max(v.co.z for v in obj.data.vertices)
    return radius, top


def flat_elevation(obj):
    """Face-on: from above when the prop is thinnest in height, else at eye level."""
    xs, ys, zs = zip(*(v.co for v in obj.data.vertices))
    spans = [max(axis) - min(axis) for axis in (xs, ys, zs)]
    return math.radians(90) if spans[2] == min(spans) else 0.0


def aim(scene, obj, elevation):
    """One orthographic camera that fits the prop at every rotation about Z."""
    radius, top = extent(obj)
    data = bpy.data.cameras.new("turntable")
    camera = bpy.data.objects.new("turntable", data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    data.type = "ORTHO"
    # What fills the frame is the prop's projection, not its height: seen from
    # `elevation` up, a cylinder of `radius` and `top` spans 2 * radius across
    # and top * cos + 2 * radius * sin down the frame. Every rotation fits
    # inside that cylinder, so one scale serves all. Eye level gives `top`
    # down; straight above gives 2 * radius.
    across = 2 * radius
    down = top * math.cos(elevation) + 2 * radius * math.sin(elevation)
    data.ortho_scale = max(across, down) * 1.1
    centre = Vector((0.0, 0.0, top / 2))
    direction = Vector((0.0, -math.cos(elevation), math.sin(elevation)))
    camera.location = centre + direction * 10
    data.clip_end = 30
    camera.rotation_euler = (centre - camera.location).to_track_quat("-Z", "Y").to_euler()
    return camera


def render(scene, obj, angles, path):
    for angle in angles:
        obj.rotation_euler = (0.0, 0.0, math.radians(angle))
        scene.render.filepath = path(angle)
        bpy.ops.render.render(write_still=True)


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
            stem = os.path.join(out, f"{name}_{colour}")
            flat = flat_elevation(obj)
            aim(scene, obj, ELEVATION)
            render(scene, obj, ANGLES, lambda a: f"{stem}_{a:03d}.png")
            aim(scene, obj, flat)
            render(scene, obj, FLAT_ANGLES, lambda a: f"{stem}_flat_{a:03d}.png")
            view = "from above" if flat else "at eye level"
            print(f"turntable: {name} {colour}, {len(ANGLES)} isometric frames, "
                  f"{len(FLAT_ANGLES)} flat {view}")


main()
