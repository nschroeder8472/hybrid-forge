# Tile pipeline backlog

Phase 7 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md): one
backlog across all three tools. A GIMP operation that turns a render into a
seamless pixel-art tile; an image ticket whose texture that operation finishes;
a Blender scene that lays the tile across a ground plane. Each ticket's output
is the next one's input.

## ART-001: Make a seamless tile

### Spec

Add `to_tile(image, size, colours)` to `tools/gimp/ops.py`, and a tool
`tools/gimp/tile.py` that applies it, following `tools/gimp/grayscale.py`.

`to_tile` turns an image into a seamless square tile of `size` x `size`
pixels on the palette `colours` (a list of `"#rrggbb"` strings). In order:

1. Make it seamless with GEGL's tile-seamless filter:
   `layer = image.get_layers()[0]`, then
   `layer.merge_filter(Gimp.DrawableFilter.new(layer, "gegl:tile-seamless", ""))`.
2. Scale to `size` x `size` with no interpolation:
   `Gimp.context_set_interpolation(Gimp.InterpolationType.NONE)`, then
   `image.scale(size, size)`.
3. Snap to the palette exactly as `ops.to_palette` would in the sprite tools:
   `palette = Gimp.Palette.new("tile")`; inside `try:`, for each colour
   `palette.add_entry(f"c{index}", Gegl.Color.new(colour))` — the first
   argument is the entry's name and must be a string — then
   `image.convert_indexed(Gimp.ConvertDitherType.NONE,
   Gimp.ConvertPaletteType.CUSTOM, 0, False, False, palette.get_name())`; in
   `finally:`, `palette.delete()`.

Import `Gegl` beside `Gimp`: `from gi.repository import Gegl, Gimp, Gio`.

The tool takes the arguments `input`, `output`, `size` and `palette`, reads
them with `ops.args()`, loads, applies `to_tile`, and saves.

`tools/gimp/finish_tile.py`, the project's `.png` formatter, already calls
`ops.to_tile(image, 64, PALETTE)`; it starts working when this lands.

### Allowed files

- tools/gimp/ops.py
- tools/gimp/tile.py
- tests/tile_test.py

### Reference files

- tools/gimp/ops.py
- tools/gimp/grayscale.py
- tools/gimp/runner.py
- tools/gimp/finish_tile.py
- tests/grayscale_test.py

### Acceptance criteria

- For a 256x256 RGB input whose pixel (x, y) is (x, x, x) — a left-to-right
  gradient, black to white — run with `size=16` and
  `palette=["#000000", "#404040", "#808080", "#c0c0c0", "#ffffff"]`: the
  output opens in Pillow with mode `P` and size (16, 16).
- Converted to RGB, every pixel of that output is one of the five palette
  colours.
- Converted to RGB, the mean over rows of the summed absolute RGB difference
  between pixel (15, y) and pixel (0, y) — the seam a tile makes with its
  right-hand neighbour — is at most 3 times the mean summed absolute RGB
  difference between horizontally adjacent pixels (x, y) and (x + 1, y) inside
  the tile. Without the seamless step this ratio is about 15; with it, about 2.

### Context

Decision: tools read their arguments with `ops.args()` and ignore any argument
they do not use.

Decision: a tool's tests build their inputs with Pillow in `setUpClass`, run the
tool once there with `runner.run`, and assert in the test methods, as
`grayscale_test.py` does.

## ART-002: A grass tile

**Kind:** image
**Needs:** ART-001

### Spec

A 64x64 seamless tile of short green grass, seen from directly above, for the
ground of a top-down game. An even meadow texture: short grass with small
tufts, a scattering of tiny flowers, no single large object, no path, no
stones larger than a tuft.

The image model renders it at 1024x1024 into `assets/grass.png`. The
project's formatter (`tools/gimp/finish_tile.py`) then makes it seamless,
scales it to 64x64 and snaps it to this palette: `#1b3a1b`, `#2a5a26`, `#3c7a32`, `#55993d`, `#76b84a`, `#a3d468`, `#e8e070`, `#c06a3a`, `#f0f0e0`. Write the prompt for
the 1024x1024 render; the formatter does the rest.

What the review should see: a grass texture that reads as grass at 64x64,
evenly spread across the whole tile, with no border, frame or darker edge
anywhere — those show as grid lines once the tile repeats. Mostly greens; the
yellow, orange and white are for flowers and should be a small minority.

### Allowed files

- assets/grass.png
- tests/grass_test.py

### Reference files

- tests/grayscale_test.py
- tools/gimp/finish_tile.py

### Acceptance criteria

- `assets/grass.png` opens with Pillow at size (64, 64).
- Converted to RGBA, every pixel has alpha 255.
- Converted to RGB, every pixel is one of the palette colours listed in the
  spec.
- Converted to RGB, at least 4 distinct colours are used.
- Converted to RGB, at least 80% of the pixels are one of the six greens
  `#1b3a1b`, `#2a5a26`, `#3c7a32`, `#55993d`, `#76b84a`, `#a3d468`.
- Converted to RGB, the horizontal seam — the mean over rows of the summed
  absolute RGB difference between pixel (63, y) and (0, y) — is at most 3
  times the mean summed absolute RGB difference between horizontally adjacent
  pixels inside the tile, and the vertical seam, measured the same way between
  (x, 63) and (x, 0) against vertically adjacent pixels, is too.

### Context

The tests open `assets/grass.png` with Pillow. The formatter has already run
when they do: they test the 64x64 tile, never the render.

## ART-003: A meadow in Blender

**Needs:** ART-002

### Spec

Add `art/scenes/meadow.py`, following `art/scenes/crate.py`. Its `build()`
creates one mesh object named `Meadow` — a flat square ground plane, 4 m on
each side, lying in the XY plane at z 0 and centred on the origin — textured
with `assets/grass.png` repeated 4 times in each direction, and returns it.

Build the plane with `bmesh`: create the UV layer with
`uv = bm.loops.layers.uv.new("UVMap")` first, then
`bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=2.0, calc_uvs=True)`
(a 4 m square). Then set each loop's UV to `(x + 2, y + 2)` from its vertex's x and y, so
the UVs run from 0 to 4 and the texture repeats 4 times. Link it with `common.object_from_bmesh("Meadow", bm)`.

Give it one material named `Grass` that uses the tile as its base colour:

    mat = bpy.data.materials.new("Grass")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = bpy.data.images.load(path, check_existing=True)
    texture.interpolation = "Closest"
    principled = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    mat.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])

where `path` is the absolute path of `assets/grass.png`, built from this file's
location: `os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
"assets", "grass.png")`, normalised with `os.path.normpath`. `Closest`
interpolation keeps the pixel art's hard edges when it is magnified.

What the capture should show: a square of grass seen at an angle, the tile
repeated in a 4 x 4 grid with no visible seams between repeats, the pixels of
the tile hard-edged rather than blurred.

### Allowed files

- art/scenes/meadow.py
- art/tests/meadow_test.py

### Reference files

- art/scenes/crate.py
- art/scenes/common.py
- art/tests/crate_test.py
- art/tests/run_checks.py

### Acceptance criteria

- After `common.clear_scene()` and `meadow.build()`, the scene's only mesh
  object is named `Meadow`, and `build()` returned it.
- Its dimensions are x 4.0 and y 4.0, each within 0.001, and every vertex has
  z 0.0 within 0.0001.
- Its materials are exactly `["Grass"]`.
- The `Grass` material's node tree has exactly one `ShaderNodeTexImage` node,
  its `interpolation` is `"Closest"`, its image's filepath ends with
  `grass.png`, and its `Color` output is linked to the Principled BSDF's
  `Base Color` input.
- The mesh has exactly one UV layer, and across all loops the smallest UV
  coordinate is 0.0 and the largest is 4.0, each within 0.001, in both u and v.

### Context

Decision: `bmesh.ops.create_grid`'s `size` is the half-width. `size=2.0`
makes a square from -2 to 2, 4 m on a side; measured in Blender 5.2.

Decision: each loop's UV is `(x + 2, y + 2)` from its vertex, so the UVs run
from 0 to 4.

Both are marked because the first run's sign-off revision rewrote them — to
`size=4.0` (an 8 m plane) and `(x, y)` (UVs from -2) — and unmarked prose is
what a revision may change. The executor caught one contradiction, built the
other, and a retry cycle recovered.

The tests run inside Blender 5.2 through `art/tests/run_checks.py`, as
`crate_test.py` does; they may import `bpy`, `bmesh` and `mathutils`. Read a
vector's components as `v.x` or `v[0]`, never `v["x"]`.

When this lands, a person sees it by running, from `art/`:

    blender -b --factory-startup --python tools/capture.py

and opening `art/capture_out/`, or `tools/export.py` for `build/meadow.glb`.
