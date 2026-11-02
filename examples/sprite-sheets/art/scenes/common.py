"""Shared helpers for scene scripts.

Every scene module in this package exposes one function, `build()`, which
creates its object in the current scene and returns it. Checks and the capture
script call `clear_scene()` first, so `build()` can assume an empty scene.
"""

import bmesh
import bpy


def clear_scene():
    """Empty the current scene: no objects, no orphaned meshes or materials.

    `--factory-startup` still loads the default cube, camera and light, so a
    scene script run without this would be measured and rendered beside them.
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)


def object_from_bmesh(name, bm):
    """Link a new mesh object called `name` built from `bm`, and free `bm`."""
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def triangle_count(obj):
    """Triangles the object's mesh would have once triangulated."""
    return sum(len(polygon.vertices) - 2 for polygon in obj.data.polygons)


def non_manifold_edges(obj):
    """How many edges of the object's mesh are not manifold."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    count = sum(1 for edge in bm.edges if not edge.is_manifold)
    bm.free()
    return count


def material(name, rgba):
    """A material called `name` with a base colour, created once and reused.

    The colour is set twice, and both are needed. `diffuse_color` is what the
    viewport and the Workbench capture show. The Principled BSDF's Base Color
    is what EEVEE, Cycles and the glTF exporter read: with only the first set,
    an exported `.glb` comes out flat grey (0.8, 0.8, 0.8) while every capture
    looks right.
    """
    found = bpy.data.materials.get(name)
    if found is not None:
        return found
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = rgba
    mat.use_nodes = True
    principled = next(
        node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    )
    principled.inputs["Base Color"].default_value = rgba
    return mat


def exported_materials(path):
    """Material name -> glTF `baseColorFactor`, read back from a `.glb` file.

    A GLB starts with a 12-byte header, then a JSON chunk: 4 bytes of length,
    4 of type, then the document. Reading it back is how a test proves a colour
    survived export rather than only being set in Blender.
    """
    import json
    import struct

    with open(path, "rb") as handle:
        data = handle.read()
    length = struct.unpack("<I", data[12:16])[0]
    document = json.loads(data[20:20 + length])
    return {
        entry["name"]: entry.get("pbrMetallicRoughness", {}).get("baseColorFactor")
        for entry in document.get("materials", [])
    }


# The three colourways every prop in this project is built in, and the two
# neutral materials props share. Names are what `build(colour)` accepts.
COLOURS = {
    "red": (0.75, 0.08, 0.08, 1.0),
    "blue": (0.10, 0.25, 0.80, 1.0),
    "green": (0.10, 0.60, 0.15, 1.0),
}
STEEL = (0.55, 0.56, 0.58, 1.0)
DARK = (0.05, 0.05, 0.06, 1.0)


def box(bm, low, high, material_index=0):
    """Add a closed box spanning `low` to `high` (x, y, z) to `bm`."""
    verts = bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)["verts"]
    bmesh.ops.scale(bm, vec=tuple(high[i] - low[i] for i in range(3)), verts=verts)
    bmesh.ops.translate(bm, vec=tuple((high[i] + low[i]) / 2 for i in range(3)), verts=verts)
    for face in {face for vert in verts for face in vert.link_faces}:
        face.material_index = material_index
