"""The reference test: open a PNG with Pillow and assert on its pixels."""

import unittest

from PIL import Image

SAMPLE = "fixtures/sample.png"


class SampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.open(SAMPLE) as image:
            cls.rgba = image.convert("RGBA")

    def test_it_is_8_by_8(self):
        self.assertEqual(self.rgba.size, (8, 8))

    def test_the_corners_are_transparent(self):
        for corner in [(0, 0), (7, 0), (0, 7), (7, 7)]:
            self.assertEqual(self.rgba.getpixel(corner)[3], 0)

    def test_the_centre_is_opaque_brown(self):
        self.assertEqual(self.rgba.getpixel((4, 4)), (148, 96, 58, 255))


if __name__ == "__main__":
    unittest.main()
