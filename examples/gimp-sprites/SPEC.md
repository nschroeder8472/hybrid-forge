# GIMP sprite tools backlog

Three tickets, written for the parsed path. Each adds an operation to
`tools/gimp/ops.py`, a short tool script that applies it, and a test. Tools run
inside GIMP 3.2 through `tools/gimp/runner.py`; tests are ordinary `unittest`
files that build input images with Pillow, run a tool once per test class, and
assert on its output with Pillow. The capture shows the reviewing seat what
each new tool does to a Blender render of an anvil on a magenta key.

This is phase 2 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md):
GIMP enters the loop as a tool the executor writes and the project runs, and
nothing in forge knows it is there.

## SPR-001: Key out a background colour

### Spec

Add `key_out(image, key)` to `tools/gimp/ops.py`, and a tool
`tools/gimp/key_out.py` that applies it, following `tools/gimp/grayscale.py`.

`key_out` makes every pixel of exactly the colour `key` (a `"#rrggbb"` string)
fully transparent and leaves every other pixel as it was. In order:

1. `layer = image.get_layers()[0]`, then `layer.add_alpha()`.
2. `Gimp.context_set_sample_threshold(0.0)`,
   `Gimp.context_set_antialias(False)` and `Gimp.context_set_feather(False)`,
   so the selection takes the exact colour and nothing near it.
3. `image.select_color(Gimp.ChannelOps.REPLACE, layer, Gegl.Color.new(key))`.
4. `layer.edit_clear()`, then `Gimp.Selection.none(image)`.

Import `Gegl` beside `Gimp`: `from gi.repository import Gegl, Gimp, Gio`.

The tool takes the arguments `input`, `output` and `key`, reads them with
`ops.args()`, loads, applies `key_out`, and saves.

### Allowed files

- tools/gimp/ops.py
- tools/gimp/key_out.py
- tests/key_out_test.py

### Reference files

- tools/gimp/ops.py
- tools/gimp/grayscale.py
- tools/gimp/runner.py
- tests/grayscale_test.py

### Acceptance criteria

- For an 8×8 RGB input filled with (255, 0, 255), with pixel (1, 1) set to
  (255, 0, 254) and the block x 4–7, y 4–7 set to (200, 40, 40), run with
  `key="#ff00ff"`: the output opens in Pillow with mode `RGBA` and size (8, 8).
- In that output, pixel (0, 0) has alpha 0.
- Pixel (1, 1) is (255, 0, 254, 255): a colour one step from the key is kept.
- Pixel (5, 5) is (200, 40, 40, 255).
- Exactly 47 pixels have alpha 0: the 64 pixels less the 16 of the block and
  the one near-key pixel.

### Context

Decision: tools read their arguments with `ops.args()` and ignore any argument
they do not use, so the capture can pass one set of arguments to every tool.

Decision: a tool's tests build their inputs with Pillow in `setUpClass`, run
the tool once there with `runner.run`, and assert in the test methods, as
`grayscale_test.py` does. A GIMP start costs about two seconds.

## SPR-002: Snap to a fixed palette

### Spec

Add `to_palette(image, colours)` to `tools/gimp/ops.py`, and a tool
`tools/gimp/to_palette.py` that applies it, following
`tools/gimp/grayscale.py`.

`to_palette` converts the image to indexed colour using exactly the colours in
`colours`, a list of `"#rrggbb"` strings, with no dithering. Each pixel takes
the nearest palette colour. In order:

1. `palette = Gimp.Palette.new("sprite-tools")`.
2. Inside `try:`, for each colour `palette.add_entry(f"c{index}",
   Gegl.Color.new(colour))`, then
   `image.convert_indexed(Gimp.ConvertDitherType.NONE,
   Gimp.ConvertPaletteType.CUSTOM, 0, False, False, palette.get_name())`.
3. In `finally:`, `palette.delete()`.

Pass `palette.get_name()`, not the palette and not the string
`"sprite-tools"`: `convert_indexed` takes a name, and `Gimp.Palette.new`
returns `"sprite-tools #1"` when a palette by that name already exists. The
`finally` matters because a palette made with `Gimp.Palette.new` is saved into
the user's GIMP profile and stays there until deleted.

The tool takes the arguments `input`, `output` and `palette`, reads them with
`ops.args()`, loads, applies `to_palette`, and saves.

### Allowed files

- tools/gimp/ops.py
- tools/gimp/to_palette.py
- tests/to_palette_test.py

### Reference files

- tools/gimp/ops.py
- tools/gimp/grayscale.py
- tools/gimp/runner.py
- tests/grayscale_test.py

### Acceptance criteria

- For a 4×1 RGB input whose pixels are (204, 34, 34), (200, 40, 40),
  (10, 10, 10) and (250, 250, 240), run with
  `palette=["#000000", "#cc2222", "#2222cc", "#ffffff"]`: the output opens in
  Pillow with mode `P` and size (4, 1).
- Converted to RGB, its pixels are (204, 34, 34), (204, 34, 34), (0, 0, 0) and
  (255, 255, 255), in that order.
- Converted to RGB, every pixel of the output is one of the four palette
  colours.

## SPR-003: Make a sprite

**Needs:** SPR-001, SPR-002

### Spec

Add `pixelate(image, size)` to `tools/gimp/ops.py`, and a tool
`tools/gimp/sprite.py` that turns a render on a key colour into a pixel-art
sprite.

`pixelate` scales the image to `size` × `size` pixels with no interpolation:
`Gimp.context_set_interpolation(Gimp.InterpolationType.NONE)`, then
`image.scale(size, size)`.

`sprite.py` takes the arguments `input`, `output`, `key`, `size` and
`palette`, reads them with `ops.args()`, loads the image, and applies, in this
order, `ops.key_out(image, key)`, `ops.pixelate(image, size)` and
`ops.to_palette(image, palette)`, then saves. Keying out first matters: scaling
first would blend the key colour into the edges, and the blended pixels would
no longer match it.

What the capture should show: the anvil, small and blocky, its greys snapped
to the PICO-8 palette, on a transparent background with no magenta left
anywhere, including at its edges.

### Allowed files

- tools/gimp/ops.py
- tools/gimp/sprite.py
- tests/sprite_test.py

### Reference files

- tools/gimp/ops.py
- tools/gimp/key_out.py
- tools/gimp/to_palette.py
- tools/gimp/runner.py
- tests/grayscale_test.py

### Acceptance criteria

- For a 64×64 RGB input filled with (255, 0, 255) with four vertical bars, each
  8 px wide and spanning y 16–47, at x 16–23 (5, 5, 5), x 24–31
  (200, 40, 40), x 32–39 (40, 40, 210) and x 40–47 (250, 250, 250), run with
  `key="#ff00ff"`, `size=8` and
  `palette=["#000000", "#cc2222", "#2222cc", "#ffffff"]`: the output opens in
  Pillow with mode `P` and size (8, 8).
- Converted to RGBA, row y 3 reads: alpha 0 at x 0 and 1; (0, 0, 0, 255) at
  x 2; (204, 34, 34, 255) at x 3; (34, 34, 204, 255) at x 4;
  (255, 255, 255, 255) at x 5; alpha 0 at x 6 and 7.
- Converted to RGBA, all four corner pixels have alpha 0.
- Converted to RGBA, every pixel with alpha 255 is one of the four palette
  colours.

### Context

Transparent pixels are compared by alpha only. GIMP's PNG export stores
transparency on whichever palette slot the image leaves unused, or adds one, so
the RGB of a transparent pixel is not a fact about the sprite.

When this lands, a person makes a sprite from any render on a `#ff00ff`
background with:

    python -c "from tools.gimp import runner; runner.run('tools/gimp/sprite.py', input='in.png', output='out.png', key='#ff00ff', size=64, palette=['#000000', '#ffffff'])"

or sees all three tools on the anvil with `python tools/capture.py`, which
writes `capture_out/`.
