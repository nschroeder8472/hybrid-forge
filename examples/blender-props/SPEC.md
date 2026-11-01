# Blender props backlog

Three tickets, written for the parsed path. Each writes a `bpy` scene script and
its tests. The tests run inside Blender, through the `art` workspace's test
command, and the capture command renders whatever scene the attempt changed for
the reviewing seat to look at.

This is phase 1 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md). The
question it answers is whether an executor lands `bpy` against Blender 5.2 at
all, and which API mistakes it repeats.

## SCN-001: An anvil

### Spec

Add `art/scenes/anvil.py`, following the shape of `art/scenes/crate.py`. It
defines `build()`, which creates one mesh object named `Anvil` in the current
scene and returns it. `build()` may assume the scene is empty.

Build the mesh with `bmesh`, from four parts joined in one `bmesh.BMesh`, then
link it with `common.object_from_bmesh("Anvil", bm)`. Each part is a closed
solid. Coordinates are in metres, with +X along the anvil's length and +Z up:

- **Base:** a box spanning x −0.20 to 0.20, y −0.10 to 0.10, z 0.00 to 0.08.
- **Waist:** a box spanning x −0.12 to 0.12, y −0.06 to 0.06, z 0.08 to 0.20.
- **Face:** a box spanning x −0.25 to 0.15, y −0.08 to 0.08, z 0.20 to 0.30.
- **Horn:** a cone with 12 segments, its axis along +X at y 0, z 0.25. Its base
  is a circle of radius 0.05 at x 0.15, and it narrows to a point at x 0.35.

Make each box with `bmesh.ops.create_cube` and then scale and translate its
vertices. Make the horn with `bmesh.ops.create_cone` (`cap_ends=True`,
`cap_tris=False`, `radius1=0.05`, `radius2=0.0`, `depth=0.20`, `segments=12`,
`calc_uvs=True`). The cone is created centred on the origin along +Z, wide end
at −Z. Rotate its vertices by +90° about Y with `bmesh.ops.rotate` and a
`mathutils.Matrix.Rotation`, which puts the point at +X, then translate them by
(0.25, 0, 0.25).

Create the UV layer with `bm.loops.layers.uv.new("UVMap")` before any part, and
pass `calc_uvs=True` to both `create_cube` and `create_cone`, as `crate.py`
does. Give the object one material, `common.material("Iron", (0.25, 0.25, 0.27,
1.0))`.

What the capture should show: from the front, a wide flat top, a narrower
waist, a wider base, and a horn tapering to a point on the right.

### Allowed files

- art/scenes/anvil.py
- art/tests/anvil_test.py

### Reference files

- art/scenes/crate.py
- art/scenes/common.py
- art/tests/crate_test.py
- art/tests/run_checks.py

### Acceptance criteria

- `anvil.build()` returns the object it created; after `common.clear_scene()`
  and `anvil.build()`, the scene's only mesh object is named `Anvil`.
- The object's dimensions are x 0.60, y 0.20 and z 0.30, each within 0.002.
- The lowest vertex has z 0.0, within 0.0001.
- The vertex with the largest x is at (0.35, 0.0, 0.25), each coordinate within
  0.002.
- The vertex with the smallest x has x −0.25, within 0.002, and z between 0.20
  and 0.30.
- `common.non_manifold_edges(obj)` returns 0.
- `common.triangle_count(obj)` returns 58: 12 for each of the three boxes, and
  22 for the horn (12 sides and a 12-sided base).
- The mesh has exactly one UV layer.
- The object's materials are exactly `["Iron"]`.
- `bpy.ops.export_scene.gltf(filepath=..., export_format="GLB")` writes a file
  larger than 0 bytes.

### Context

The tests run inside Blender 5.2's bundled Python, through
`art/tests/run_checks.py`, which discovers `tests/*_test.py` with `unittest`.
They may import `bpy`, `bmesh` and `mathutils`, and import scene modules as
`from scenes import common, anvil`. `crate_test.py` is the house shape.

Decision: every scene module exposes `build()`, which creates its objects in
the current scene and returns what it created.

## SCN-002: A barrel

### Spec

Add `art/scenes/barrel.py`, following the shape of `art/scenes/crate.py`. Its
`build()` creates one mesh object named `Barrel` in the current scene and
returns it.

Build it with `bmesh` as ten rings of 16 vertices, stacked at z = 0.0, 0.1,
0.2, … 0.9. Ring `i` sits at height `z = 0.1 * i`. Its radius is
`0.25 + 0.05 * (1 - ((z - 0.45) / 0.45) ** 2)`, so the barrel is 0.25 at both
ends and bulges towards the middle. Vertex `j` of a ring is at angle
`2 * pi * j / 16` from +X, counter-clockwise seen from above.

Join consecutive rings with quads, 16 per pair, and close the bottom and top
rings each with one 16-sided face. That is 160 vertices and 146 faces, and
every face's normal points outwards: call `bmesh.ops.recalc_face_normals` on all
faces before linking.

Add the UV layer with `bm.loops.layers.uv.new("UVMap")` before any geometry.
UV values may stay at their defaults.

Give the object two materials, in this order:
`common.material("Wood", (0.55, 0.36, 0.18, 1.0))` and
`common.material("Iron", (0.25, 0.25, 0.27, 1.0))`. Set `material_index` to 1
(Iron) on the side faces between the first two rings and between the last two
rings, and leave every other face at 0 (Wood). Those are the barrel's two iron
bands.

What the capture should show: a bulging wooden barrel, standing upright, with a
dark band at the top and the bottom.

### Allowed files

- art/scenes/barrel.py
- art/tests/barrel_test.py

### Reference files

- art/scenes/crate.py
- art/scenes/common.py
- art/tests/crate_test.py

### Acceptance criteria

- After `common.clear_scene()` and `barrel.build()`, the scene's only mesh
  object is named `Barrel`, and `build()` returned it.
- The mesh has exactly 160 vertices and 146 faces.
- The object's z dimension is 0.90 within 0.0001, and its x and y dimensions
  are each between 0.595 and 0.600.
- The lowest vertex has z 0.0, within 0.0001.
- Every vertex with z within 0.0001 of 0.0 lies at distance 0.25 from the Z
  axis, within 0.0001.
- `common.non_manifold_edges(obj)` returns 0.
- The object's materials are exactly `["Wood", "Iron"]`, in that order.
- Exactly 32 faces have `material_index` 1, and each of them has a centre z
  below 0.1 or above 0.8.
- Both 16-sided faces have `material_index` 0.
- The mesh has exactly one UV layer.

### Context

Ring heights and radii are exact by construction, so the tests can compare
them with tight tolerances. The two caps are the only faces with 16 vertices.

## SCN-003: The workshop

**Needs:** SCN-001, SCN-002

### Spec

Add `art/scenes/workshop.py`. Its `build()` calls `crate.build()`,
`anvil.build()` and `barrel.build()` from the existing scene modules, moves each
object by setting its `location`, and returns the three objects as a list in
the order `[crate, anvil, barrel]`.

Place them in a row along X, each resting on the ground: the crate with
`location` (−1.2, 0, 0), the anvil with `location` (0, 0, 0), and the barrel
with `location` (1.0, 0, 0). Import the modules as
`from scenes import anvil, barrel, crate`; the workshop builds each object by
calling its module, so a change to a prop shows up here unchanged.

What the capture should show: a crate on the left, the anvil in the middle with
its horn pointing right, and a barrel on the right, all standing on the same
ground line with clear space between them.

### Allowed files

- art/scenes/workshop.py
- art/tests/workshop_test.py

### Reference files

- art/scenes/crate.py
- art/scenes/anvil.py
- art/scenes/barrel.py
- art/scenes/common.py
- art/tests/crate_test.py

### Acceptance criteria

- After `common.clear_scene()` and `workshop.build()`, the scene holds exactly
  three mesh objects, named `Crate`, `Anvil` and `Barrel`.
- `build()` returns a list whose names are `["Crate", "Anvil", "Barrel"]`.
- The objects' locations are (−1.2, 0, 0), (0, 0, 0) and (1.0, 0, 0)
  respectively, each coordinate within 0.0001.
- For each object, the lowest world-space vertex has z 0.0, within 0.0001.
- The world-space bounding boxes of the three objects are pairwise disjoint
  along X, with at least 0.2 between the crate's largest x and the anvil's
  smallest x, and at least 0.2 between the anvil's largest x and the barrel's
  smallest x.
- Each object's triangle count from `common.triangle_count` equals the count
  for the same object built alone by its own module.

### Context

Decision: `common.clear_scene()` leaves the scene with no objects at all. It
calls `bpy.ops.wm.read_factory_settings(use_empty=True)`, which removes the
default cube, camera and light, and every passing test in `art/tests/` relies
on that.

Decision: `art/scenes/` is a package with an empty `__init__.py`, and
`from scenes import anvil, barrel, crate` resolves under the test runner,
exactly as `crate_test.py`'s `from scenes import common, crate` does.

World-space positions come from `obj.matrix_world @ vertex.co`. After setting
`location` in a script, call `bpy.context.view_layer.update()` before reading
`matrix_world`, or it still holds the old transform.

When this lands, a person sees the result by running, from `art/`:

    blender -b --factory-startup --python tools/capture.py

and opening `art/capture_out/`, or by opening Blender and running
`scenes/workshop.py`'s `build()` from the Python console after
`common.clear_scene()`.
