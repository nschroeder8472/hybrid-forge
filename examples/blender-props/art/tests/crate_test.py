"""Tests for `scenes/crate.py`. Run inside Blender, through `run_checks.py`."""

import os
import tempfile
import unittest

import bpy

from scenes import common, crate


class CrateTest(unittest.TestCase):
    def setUp(self):
        common.clear_scene()
        self.obj = crate.build()

    def test_it_is_the_only_mesh_and_is_named_crate(self):
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        self.assertEqual([obj.name for obj in meshes], ["Crate"])

    def test_it_is_a_one_metre_cube_resting_on_the_ground(self):
        # A `mathutils.Vector` is indexed by integer — `dimensions[0]` or
        # `dimensions.x` — never by the string "x". Both live runs' testers
        # wrote `location["x"]` after reading a loop that paired "xyz" with a
        # vector, so this one keeps the index an integer.
        for index in range(3):
            self.assertAlmostEqual(
                self.obj.dimensions[index], 1.0, places=3, msg=f"{'xyz'[index]} dimension"
            )
        lowest = min(vertex.co.z for vertex in self.obj.data.vertices)
        self.assertAlmostEqual(lowest, 0.0, places=4)

    def test_it_has_twelve_triangles(self):
        self.assertEqual(common.triangle_count(self.obj), 12)

    def test_it_is_manifold(self):
        self.assertEqual(common.non_manifold_edges(self.obj), 0)

    def test_it_has_a_uv_map(self):
        self.assertEqual(len(self.obj.data.uv_layers), 1)

    def test_it_uses_the_wood_material(self):
        self.assertEqual([m.name for m in self.obj.data.materials], ["Wood"])

    def test_it_exports_to_gltf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "crate.glb")
            bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
            self.assertGreater(os.path.getsize(path), 0)


if __name__ == "__main__":
    unittest.main()
