"""Tests for `tools/gimp/grayscale.py`. The reference test.

Each test class builds its input images with Pillow, runs the tool once in GIMP
in `setUpClass` — a GIMP start costs about two seconds, so it is paid once per
class, not once per test — and asserts on the output with Pillow.
"""

import os
import tempfile
import unittest

from PIL import Image

from tools.gimp import runner

TOOL = os.path.join("tools", "gimp", "grayscale.py")


class GrayscaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        source = os.path.join(cls.tmp.name, "in.png")
        cls.output = os.path.join(cls.tmp.name, "out.png")
        image = Image.new("RGB", (8, 4), (255, 0, 0))
        image.paste((0, 0, 255), (4, 0, 8, 4))
        image.save(source)
        runner.run(TOOL, input=source, output=cls.output)
        cls.result = Image.open(cls.output)
        cls.result.load()

    @classmethod
    def tearDownClass(cls):
        cls.result.close()
        cls.tmp.cleanup()

    def test_it_keeps_the_size(self):
        self.assertEqual(self.result.size, (8, 4))

    def test_every_pixel_is_gray(self):
        rgb = self.result.convert("RGB")
        for x in range(8):
            for y in range(4):
                r, g, b = rgb.getpixel((x, y))
                self.assertEqual((r, r), (g, b), f"pixel {(x, y)}")

    def test_red_and_blue_become_different_grays(self):
        rgb = self.result.convert("RGB")
        self.assertNotEqual(rgb.getpixel((0, 0)), rgb.getpixel((7, 0)))


if __name__ == "__main__":
    unittest.main()
