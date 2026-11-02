"""Build the sprite sheets: render, finish, pack.

    python tools/make_sheets.py [SCENE ...]        (default: key lock)

1. Blender renders each scene in each colourway from eight isometric
   directions and four flat, face-on views (`art/tools/turntable.py`),
   256 px, on a #ff00ff key.
2. GIMP turns every render into a 64x64 sprite on PALETTE, in one start
   (`tools/gimp/sprites.py`).
3. `tools/pack_sheet.py` lays each scene's sprites out as `sheets/<scene>.png`,
   one row per colour and one column per view (`0` ... `315`, then `flat_0`
   ... `flat_270`), with
   `sheets/<scene>.json` describing every frame.

Blender is found through BLENDER, then PATH, then the default Windows
install; GIMP through `tools/gimp/runner.py`.
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import pack_sheet  # noqa: E402
from tools.gimp import runner  # noqa: E402

FRAME = 64
RENDER = 256
COLOURS = ["red", "blue", "green"]
ANGLES = [45 * step for step in range(8)]
FLAT_ANGLES = [90 * step for step in range(4)]
# (column name, file suffix) for every frame in a row, in sheet order.
VIEWS = ([(str(a), f"{a:03d}") for a in ANGLES]
         + [(f"flat_{a}", f"flat_{a:03d}") for a in FLAT_ANGLES])
PALETTE = [
    "#000000", "#2a2a2e", "#5a5c62", "#8e9096", "#c8cad0",
    "#3d0a0a", "#7a1414", "#b82424", "#e85a4a", "#0a1740",
    "#1a3a8a", "#2f63d0", "#6f9cf0", "#0a3010", "#1a6624",
    "#2fa03a", "#7ad36a",
]


def blender():
    found = os.environ.get("BLENDER") or shutil.which("blender")
    default = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
    return found or default


def main(names):
    os.makedirs(os.path.join(ROOT, "sheets"), exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        renders, sprites = os.path.join(work, "renders"), os.path.join(work, "sprites")
        os.makedirs(sprites)
        subprocess.run(
            [blender(), "-b", "--factory-startup", "--python-exit-code", "1", "--python",
             os.path.join(ROOT, "art", "tools", "turntable.py"),
             "--", renders, str(RENDER), *names],
            check=True, cwd=os.path.join(ROOT, "art"),
        )
        frames = [
            [os.path.join(renders, f), os.path.join(sprites, f)]
            for f in sorted(os.listdir(renders))
        ]
        runner.run(os.path.join(ROOT, "tools", "gimp", "sprites.py"),
                   frames=frames, size=FRAME, palette=PALETTE)
        for name in names:
            grid = [
                [os.path.join(sprites, f"{name}_{colour}_{suffix}.png") for _, suffix in VIEWS]
                for colour in COLOURS
            ]
            out = os.path.join(ROOT, "sheets", f"{name}.png")
            pack_sheet.pack(grid, out, rows=COLOURS, columns=[column for column, _ in VIEWS])
            print(f"sheets/{name}.png and sheets/{name}.json")


if __name__ == "__main__":
    main(sys.argv[1:] or ["key", "lock"])
