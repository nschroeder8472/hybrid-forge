"""Show the reviewing seat a changed prop from every view the sheet holds, per colour.

Run by forge as the capture command. For each scene the attempt wrote (from
`FORGE_CAPTURE_FILES`; every scene by hand), renders the turntable and lays
its twelve frames (eight isometric, then four flat) side by side, one strip
per colourway, into `FORGE_CAPTURE_DIR` (`capture_out/` by hand). Three
strips per scene: the reviewer sees every view and every colour the sheet
will hold.
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NOT_SCENES = {"__init__", "common", "crate"}


def scenes():
    named = os.environ.get("FORGE_CAPTURE_FILES")
    if named is None:
        paths = [f"scenes/{n}" for n in os.listdir(os.path.join(ROOT, "scenes"))]
    else:
        paths = [p.replace("\\", "/") for p in named.split(os.pathsep) if p]
    found = []
    for path in paths:
        base = os.path.basename(path)
        if path.startswith("scenes/") and base.endswith(".py") and base[:-3] not in NOT_SCENES:
            found.append(base[:-3])
    return sorted(set(found))


def main():
    import bpy  # noqa: F401 - run inside Blender; the strips are made in a child

    out = os.environ.get("FORGE_CAPTURE_DIR") or os.path.join(ROOT, "capture_out")
    os.makedirs(out, exist_ok=True)
    names = scenes()
    if not names:
        print("capture: no prop scenes changed; nothing to render")
        return
    with tempfile.TemporaryDirectory() as frames:
        subprocess.run(
            [bpy.app.binary_path, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", os.path.join(HERE, "turntable.py"), "--", frames, "128", *names],
            check=True,
        )
        strips(frames, out, names)


def strips(frames, out, names):
    """Lay each scene's frames out as one strip per colour, using Blender's images."""
    import bpy

    from scenes import common

    for name in names:
        for colour in common.COLOURS:
            files = sorted(f for f in os.listdir(frames) if f.startswith(f"{name}_{colour}_"))
            images = [bpy.data.images.load(os.path.join(frames, f)) for f in files]
            width, height = images[0].size
            strip = bpy.data.images.new(f"{name}_{colour}", width * len(images), height)
            pixels = [0.0] * (width * len(images) * height * 4)
            for index, image in enumerate(images):
                source = list(image.pixels)
                for y in range(height):
                    row = source[y * width * 4:(y + 1) * width * 4]
                    start = (y * width * len(images) + index * width) * 4
                    pixels[start:start + width * 4] = row
            strip.pixels = pixels
            strip.filepath_raw = os.path.join(out, f"{name}_{colour}_turntable.png")
            strip.file_format = "PNG"
            strip.save()
            print(f"capture: {name} {colour}")


if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
main()
