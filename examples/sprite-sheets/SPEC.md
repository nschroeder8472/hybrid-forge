# Sprite sheets: a key and its lock

Three tickets. Two Blender props built in three colourways — a key and the
padlock it opens — and the packer that lays rendered frames out as a sheet.
Once they land, `python tools/make_sheets.py` renders each prop in red, blue
and green from eight isometric directions and four flat, face-on views,
finishes every frame into a 64x64 sprite in GIMP, and writes `sheets/key.png`
and `sheets/lock.png`: one row per colour, one column per view, with a JSON
file beside each describing its frames.

## SHT-001: A key in three colours

### Spec

Add `art/scenes/key.py`, following `art/scenes/crate.py`. It defines
`build(colour)`, which creates one mesh object named `Key` in the current
scene, coloured `colour`, and returns it. `colour` is one of the keys of
`common.COLOURS` — `"red"`, `"blue"`, `"green"`. Any other value raises
`ValueError` with the message `unknown colour '<colour>'; expected one of red,
blue, green`.

The key lies flat on the ground with its length along X. Build it in one
`bmesh.BMesh`, creating the UV layer first with
`bm.loops.layers.uv.new("UVMap")`, from these parts, every one a closed solid:

- **Bow:** a disc of radius 0.10 and thickness 0.04, centred at
  (-0.20, 0, 0.02).
- **Bow hole:** a disc of radius 0.04 and thickness 0.05, centred at
  (-0.20, 0, 0.025), in the dark material — it shows as a hole from above.
- **Shaft:** `common.box(bm, (-0.11, -0.02, 0.0), (0.30, 0.02, 0.04), 0)`.
- **Teeth:** `common.box(bm, (0.18, 0.02, 0.0), (0.22, 0.08, 0.04), 0)` and
  `common.box(bm, (0.25, 0.02, 0.0), (0.30, 0.06, 0.04), 0)`.

Make each disc with `bmesh.ops.create_cone(bm, cap_ends=True,
cap_tris=False, segments=24, radius1=r, radius2=r, depth=d, calc_uvs=True)`,
which is centred on the origin along Z, then translate its vertices with
`bmesh.ops.translate`. Set the bow hole's faces to `material_index = 1`:
the faces of a part are the faces of its vertices,
`{face for vert in verts for face in vert.link_faces}`.

Link it with `common.object_from_bmesh("Key", bm)` and give it two materials,
in this order: `common.material(f"Key-{colour}", common.COLOURS[colour])` and
`common.material("Dark", common.DARK)`.

What the capture should show: the same key in red, blue and green, from eight
directions — a round bow with a dark hole, a straight shaft, two teeth at the
end — reading as a key at every angle.

### Allowed files

- art/scenes/key.py
- art/tests/key_test.py

### Reference files

- art/scenes/crate.py
- art/scenes/common.py
- art/tests/crate_test.py
- art/tests/run_checks.py

### Acceptance criteria

- For each of `"red"`, `"blue"` and `"green"`: after `common.clear_scene()`
  and `key.build(colour)`, the scene's only mesh object is named `Key`, and
  `build()` returned it.
- Its dimensions are x 0.60, y 0.20 and z 0.05, each within 0.002, and its
  lowest vertex has z 0.0 within 0.0001.
- `common.non_manifold_edges(obj)` returns 0.
- Its materials are exactly `[f"Key-{colour}", "Dark"]`.
- Exported with `bpy.ops.export_scene.gltf(..., export_format="GLB")` and read
  back with `common.exported_materials`, `Key-{colour}` has base colour
  `common.COLOURS[colour]`, each component within 0.001.
- Every face with the `Dark` material has its centre within 0.045 of
  (-0.20, 0) in x and y: the hole is in the bow.
- `key.build("purple")` raises `ValueError` whose message contains `red`,
  `blue` and `green`.

### Context

Decision: the key is 0.60 x 0.20 x 0.05 m, from x -0.30 to 0.30; these were
measured from a reference build in Blender 5.2.

Decision: `bmesh.ops.create_cone` with `depth=d` spans z -d/2 to d/2 before
translation, and `common.box` spans exactly the corners it is given.

The tests run inside Blender 5.2 through `art/tests/run_checks.py`, as
`crate_test.py` does. Read a vector's components as `v.x` or `v[0]`, never
`v["x"]`.

## SHT-002: The lock it opens

**Needs:** SHT-001

### Spec

Add `art/scenes/lock.py`, following `art/scenes/key.py`. Its `build(colour)`
creates one mesh object named `Lock` — a padlock in the same colourway as the
key it matches — and returns it, with the same `ValueError` for an unknown
colour.

The padlock stands upright, its front facing -Y. Build it in one
`bmesh.BMesh`, creating the UV layer first, from these parts:

- **Body:** `common.box(bm, (-0.15, -0.07, 0.0), (0.15, 0.07, 0.24), 0)`.
- **Shackle**, in steel (material index 1): two posts,
  `common.box(bm, (-0.11, -0.025, 0.24), (-0.07, 0.025, 0.40), 1)` and
  `common.box(bm, (0.07, -0.025, 0.24), (0.11, 0.025, 0.40), 1)`, and a top
  bar, `common.box(bm, (-0.11, -0.025, 0.36), (0.11, 0.025, 0.40), 1)`.
- **Keyhole**, dark (material index 2), on the front face: a disc made with
  `bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=24,
  radius1=0.025, radius2=0.025, depth=0.02, calc_uvs=True)`, rotated by +90
  degrees about X with `bmesh.ops.rotate` and a `mathutils.Matrix.Rotation` so
  its axis runs along Y, then translated by (0, -0.075, 0.15); and a slot,
  `common.box(bm, (-0.012, -0.08, 0.07), (0.012, -0.065, 0.14), 2)`.

Link it with `common.object_from_bmesh("Lock", bm)` and give it three
materials, in this order: `common.material(f"Lock-{colour}",
common.COLOURS[colour])`, `common.material("Steel", common.STEEL)` and
`common.material("Dark", common.DARK)`.

What the capture should show: a padlock in each colour, from eight
directions — a coloured body, a grey shackle above it, and a dark keyhole on
the front, visible whenever the front faces the camera.

### Allowed files

- art/scenes/lock.py
- art/tests/lock_test.py

### Reference files

- art/scenes/key.py
- art/scenes/common.py
- art/tests/key_test.py
- art/tests/run_checks.py

### Acceptance criteria

- For each of `"red"`, `"blue"` and `"green"`: after `common.clear_scene()`
  and `lock.build(colour)`, the scene's only mesh object is named `Lock`, and
  `build()` returned it.
- Its dimensions are x 0.30, y 0.155 and z 0.40, each within 0.002, and its
  lowest vertex has z 0.0 within 0.0001.
- `common.non_manifold_edges(obj)` returns 0.
- Its materials are exactly `[f"Lock-{colour}", "Steel", "Dark"]`.
- Exported to GLB and read back with `common.exported_materials`,
  `Lock-{colour}` has base colour `common.COLOURS[colour]`, each component
  within 0.001.
- Every face with the `Dark` material has a centre y of -0.0649 or less: the
  keyhole is on the front. (The disc's back cap sits at -0.065; the margin
  allows for floating point.)
- Every face with the `Steel` material has a centre z of 0.2399 or more: the
  shackle is above the body. (The posts' bottom faces sit at 0.24, which
  floating point can report as 0.23999999.)
- `lock.build("purple")` raises `ValueError` whose message contains `red`,
  `blue` and `green`.

### Context

Decision: the lock is 0.30 x 0.155 x 0.40 m; the keyhole disc reaches y
-0.085, in front of the body's -0.07. Measured from a reference build.

Decision: the dimensions are of all the parts together, not of the body. The
body alone is 0.30 x 0.14 x 0.24; the keyhole disc extends the y range from
the body's -0.07 to -0.085, giving 0.155, and the shackle's top bar extends
the z range from the body's 0.24 to 0.40.

## SHT-003: Pack frames into a sheet

### Spec

Add `tools/pack_sheet.py` with `pack(grid, out, rows, columns)`.

`grid` is a list of rows, each a list of paths to PNG frames; `rows` names each
row and `columns` names each column, as strings. Every frame must have the
same size, `w` x `h`. `pack` writes to `out` one RGBA PNG of
`w * len(columns)` by `h * len(rows)`, transparent where no frame is opaque,
with frame `grid[r][c]` pasted at (`c * w`, `r * h`). It also writes, beside
`out` with the extension `.json`, this object:

    {"frame_size": [w, h], "rows": rows, "columns": columns,
     "frames": {"<row>_<column>": [x, y, w, h], ...}}

with one entry per frame. It raises `ValueError` — before writing anything —
when the grid has a different number of rows than `rows`, a row has a
different number of frames than `columns`, or any frame's size differs from
the first's. Use Pillow, and open each frame with `with Image.open(...)` so no
file stays open.

`tools/make_sheets.py` already calls `pack_sheet.pack(grid, out, rows=COLOURS,
columns=[...])`; it starts working when this lands.

### Allowed files

- tools/pack_sheet.py
- tests/pack_sheet_test.py

### Reference files

- tools/make_sheets.py
- tests/sample_test.py

### Acceptance criteria

- For a 3 x 8 grid of 64x64 RGBA frames, each transparent except pixel (0, 0),
  which is a colour unique to that frame, packed with
  `rows=["red", "blue", "green"]` and `columns=["0", "45", ..., "315"]`: the
  sheet opens in Pillow at size (512, 192) with mode `RGBA`.
- In that sheet, pixel (`c * 64`, `r * 64`) is frame `[r][c]`'s unique colour
  for every row and column, and pixel (1, 1) has alpha 0.
- The JSON beside it has `frame_size` `[64, 64]`, the given `rows` and
  `columns`, 24 entries in `frames`, and `frames["blue_45"]` equal to
  `[64, 64, 64, 64]`.
- A grid with a row of 7 frames raises `ValueError`, and no sheet is written.
- A grid in which one frame is 32x32 raises `ValueError`.

### Context

The tests build their frames with Pillow in a temporary directory, as
`tests/sample_test.py` builds its fixture.

When all three land, a person makes the sheets with:

    python tools/make_sheets.py

which writes `sheets/key.png`, `sheets/key.json`, `sheets/lock.png` and
`sheets/lock.json`.
