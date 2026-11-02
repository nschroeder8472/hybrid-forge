"""Write every scene as a game asset and as an editable Blender file.

    blender -b --factory-startup --python-exit-code 1 --python tools/export.py

For each `scenes/<name>.py` this writes:

- `build/<name>.glb` — glTF binary: mesh, UVs and materials in one file. What a
  game engine imports (Godot natively; Unreal, Unity, three.js through glTF).
- `build/<name>.blend` — the built scene, to open and keep working on in
  Blender: texture it, light it, render it.

`build/` is regenerated output, ignored by git. The scene scripts are the
source; these are what the source builds.

Pass scene names after `--` to export only those:

    blender -b --factory-startup --python tools/export.py -- anvil barrel
"""

import importlib
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scenes import common  # noqa: E402

SCENES = os.path.join(ROOT, "scenes")
BUILD = os.path.join(ROOT, "build")
NOT_SCENES = {"__init__", "common"}


def all_scenes():
    names = [name[:-3] for name in os.listdir(SCENES) if name.endswith(".py")]
    return sorted(name for name in names if name not in NOT_SCENES)


def main():
    asked = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    names = asked or all_scenes()
    os.makedirs(BUILD, exist_ok=True)
    for name in names:
        common.clear_scene()
        module = importlib.import_module(f"scenes.{name}")
        importlib.reload(module)
        module.build()
        glb = os.path.join(BUILD, f"{name}.glb")
        blend = os.path.join(BUILD, f"{name}.blend")
        bpy.ops.export_scene.gltf(filepath=glb, export_format="GLB")
        bpy.ops.wm.save_as_mainfile(filepath=blend, check_existing=False)
        print(f"export: scenes/{name}.py -> build/{name}.glb, build/{name}.blend")


main()
