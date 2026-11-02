"""Render the scenes an attempt changed, for the reviewing seat to look at.

Started by the workspace's capture command:

    blender -b --factory-startup --python-exit-code 1 --python tools/capture.py

forge sets `FORGE_CAPTURE_DIR` and attaches every image written there to the
review. Without it (run by hand) images go to `capture_out/`.

Which scenes: the ones this attempt wrote, because a review is about this
attempt's work, and forge sends at most four images. forge names them in
`FORGE_CAPTURE_FILES`. Run by hand, the scenes `git status` reports changed
stand in. One scene gets four views: front, side, top and three-quarter.
Several get one three-quarter view each. With none, every scene gets one view.

A scene the attempt did not touch is left out even if it is uncommitted. With
`autoCommit` off every earlier ticket's work is uncommitted, and the first
version of this script, which asked git, rendered the whole backlog into each
review.

Everything that could make two captures of the same scene differ is pinned:
Workbench engine, material colours, Standard view transform, fixed resolution,
and a scene cleared to empty first. A difference between two captures is then
a difference in the model.
"""

import importlib
import math
import os
import subprocess
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scenes import common  # noqa: E402

SCENES = os.path.join(ROOT, "scenes")
NOT_SCENES = {"__init__", "common"}
RESOLUTION = 1024
MAX_IMAGES = 4

# Direction from the object's centre to the camera, and whether the view is
# orthographic. The orthographic views give clean silhouettes to measure by
# eye; the three-quarter view is the one that reads as an object.
VIEWS = {
    "front": (Vector((0, -1, 0)), True),
    "side": (Vector((1, 0, 0)), True),
    "top": (Vector((0, 0, 1)), True),
    "three_quarter": (Vector((1, -1, 0.7)).normalized(), False),
}


def all_scenes():
    names = [name[:-3] for name in os.listdir(SCENES) if name.endswith(".py")]
    return sorted(name for name in names if name not in NOT_SCENES)


def scene_names(paths):
    """Scene module names among workspace-relative paths."""
    names = set()
    for path in paths:
        path = path.replace("\\", "/")
        base = os.path.basename(path)
        if path.startswith("scenes/") and base.endswith(".py") and base[:-3] not in NOT_SCENES:
            names.add(base[:-3])
    return sorted(name for name in names if os.path.exists(os.path.join(SCENES, name + ".py")))


def written_scenes():
    """Scenes forge says this attempt wrote, or None when forge did not say."""
    value = os.environ.get("FORGE_CAPTURE_FILES")
    if value is None:
        return None
    return scene_names(path for path in value.split(os.pathsep) if path)


def changed_scenes():
    """Scene modules git reports as modified or new, or [] if git cannot say."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", "scenes"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return scene_names(line[3:].strip().strip('"') for line in out.splitlines())


def setup_render():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = RESOLUTION
    scene.render.resolution_y = RESOLUTION
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    # Mid grey rather than the empty scene's black, so dark faces and edges
    # still read against the background.
    world = bpy.data.worlds.new("capture")
    world.color = (0.4, 0.4, 0.4)
    scene.world = world
    shading = scene.display.shading
    shading.light = "STUDIO"
    # TEXTURE shows an image texture where a material has one, and the
    # material colour where it does not.
    shading.color_type = "TEXTURE"
    shading.show_cavity = True
    return scene


def bounds(objects):
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    low = Vector([min(c[i] for c in corners) for i in range(3)])
    high = Vector([max(c[i] for c in corners) for i in range(3)])
    return low, high


def render_view(scene, objects, view, path):
    direction, ortho = VIEWS[view]
    low, high = bounds(objects)
    centre = (low + high) / 2
    extent = max((high - low).length, 0.01)

    data = bpy.data.cameras.new("capture")
    camera = bpy.data.objects.new("capture", data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    data.clip_end = extent * 20
    if ortho:
        data.type = "ORTHO"
        data.ortho_scale = extent * 1.15
        distance = extent * 3
    else:
        data.lens = 50
        distance = extent / (2 * math.tan(data.angle / 2)) * 1.25
    camera.location = centre + direction * distance
    # A camera looks down its local -Z with local +Y up. For the top view,
    # aim slightly off vertical so "up" in the frame is +Y in the world.
    aim = centre - camera.location
    if view == "top":
        aim = Vector((0, 0.0001, -1))
    camera.rotation_euler = aim.to_track_quat("-Z", "Y").to_euler()

    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(camera)
    bpy.data.cameras.remove(data)


def main():
    out = os.environ.get("FORGE_CAPTURE_DIR") or os.path.join(ROOT, "capture_out")
    os.makedirs(out, exist_ok=True)

    written = written_scenes()
    names = (written if written is not None else changed_scenes()) or all_scenes()
    if len(names) == 1:
        plan = [(names[0], view) for view in VIEWS]
    else:
        plan = [(name, "three_quarter") for name in names[:MAX_IMAGES]]

    for name, view in plan:
        common.clear_scene()
        scene = setup_render()
        module = importlib.import_module(f"scenes.{name}")
        importlib.reload(module)
        module.build()
        objects = [obj for obj in scene.objects if obj.type == "MESH"]
        if not objects:
            print(f"capture: scenes/{name}.py built no mesh; nothing to render")
            continue
        render_view(scene, objects, view, os.path.join(out, f"{name}_{view}.png"))
        print(f"capture: scenes/{name}.py, {view}")


main()
