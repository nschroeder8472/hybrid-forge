"""A wooden crate: a 1 m cube with a UV map and one wood material.

The reference scene. Every scene module follows its shape: build the mesh with
`bmesh`, link it through `common.object_from_bmesh`, give it materials, and
return the object from `build()`.
"""

import bmesh

from scenes import common

SIZE = 1.0
WOOD = (0.55, 0.36, 0.18, 1.0)


def build():
    """Create the crate in the current scene and return it."""
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    bmesh.ops.create_cube(bm, size=SIZE, calc_uvs=True)
    # Sitting on the ground plane rather than centred on it.
    bmesh.ops.translate(bm, vec=(0.0, 0.0, SIZE / 2), verts=bm.verts)
    crate = common.object_from_bmesh("Crate", bm)
    crate.data.materials.append(common.material("Wood", WOOD))
    return crate
