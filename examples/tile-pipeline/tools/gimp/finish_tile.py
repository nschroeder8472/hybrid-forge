"""Finish a rendered texture into a 64x64 seamless tile. The `.png` formatter.

GIMP opens every path the format command hands it; this applies
`ops.to_tile` to each and saves it back over the same file. The operation is
ART-001's; this file only fixes the size and the palette, so the image ticket
and the Blender scene agree on both.
"""

from gi.repository import Gimp

import ops

SIZE = 64
PALETTE = [
    "#1b3a1b", "#2a5a26", "#3c7a32", "#55993d", "#76b84a",
    "#a3d468", "#e8e070", "#c06a3a", "#f0f0e0",
]

for image in Gimp.get_images():
    ops.to_tile(image, SIZE, PALETTE)
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, image.get_file(), None)
