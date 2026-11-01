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
    """A material called `name` with a base colour, created once and reused."""
    found = bpy.data.materials.get(name)
    if found is not None:
        return found
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = rgba
    return mat
