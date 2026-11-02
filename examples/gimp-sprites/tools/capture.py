"""Show the reviewing seat what each tool this attempt wrote does to a real image.

Started by the capture command: `python tools/capture.py`. forge sets
`FORGE_CAPTURE_DIR` (where to write) and `FORGE_CAPTURE_FILES` (what the
attempt wrote). Run by hand, every tool is captured into `capture_out/`.

For each tool script the attempt wrote under `tools/gimp/`, it runs the tool
on `fixtures/anvil.png` — a Blender render of the phase 1 anvil on a flat
`#ff00ff` key — with one fixed set of demo arguments, and writes:

- `<tool>_before.png`: the input.
- `<tool>_after.png`: the output, upscaled with nearest-neighbour to at least
  256 px so each pixel of a small sprite is visible as a block.

Tools ignore arguments they do not use, so one set serves all of them.
"""

import os
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.gimp import runner  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "anvil.png")
NOT_TOOLS = {"__init__.py", "ops.py", "runner.py"}
# A ten-step grey ramp and four accents. Pixel art reads by contrast, so a
# palette needs several shades of each material it draws: the first demo
# palette, PICO-8, has one grey near an iron anvil's shading, and every face
# of the sprite came out that one grey.
PALETTE = [
    "#000000", "#1c1c1c", "#383838", "#555555", "#717171",
    "#8d8d8d", "#aaaaaa", "#c6c6c6", "#e2e2e2", "#ffffff",
    "#8f563b", "#d9a066", "#ac3232", "#5b6ee1",
]
DEMO = {"key": "#ff00ff", "size": 64, "palette": PALETTE}
MIN_SIDE = 256


def tools():
    """Tool scripts to capture: the ones forge names, or all of them by hand."""
    named = os.environ.get("FORGE_CAPTURE_FILES")
    if named is None:
        folder = os.path.join(ROOT, "tools", "gimp")
        paths = [f"tools/gimp/{name}" for name in os.listdir(folder)]
    else:
        paths = [path.replace("\\", "/") for path in named.split(os.pathsep) if path]
    return sorted(
        path for path in paths
        if path.startswith("tools/gimp/") and path.endswith(".py")
        and os.path.basename(path) not in NOT_TOOLS
        and os.path.isfile(os.path.join(ROOT, path))
    )


def upscaled(image):
    factor = max(1, -(-MIN_SIDE // max(image.size)))
    return image.resize((image.width * factor, image.height * factor), Image.NEAREST)


def main():
    out = os.environ.get("FORGE_CAPTURE_DIR") or os.path.join(ROOT, "capture_out")
    os.makedirs(out, exist_ok=True)
    for path in tools():
        name = os.path.basename(path)[:-3]
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "out.png")
            runner.run(os.path.join(ROOT, path), input=FIXTURE, output=result, **DEMO)
            with Image.open(FIXTURE) as before:
                before.save(os.path.join(out, f"{name}_before.png"))
            with Image.open(result) as after:
                upscaled(after.convert("RGBA")).save(os.path.join(out, f"{name}_after.png"))
        print(f"capture: {path}")


main()
