"""Finish a rendered image into a 32x32 sprite. Run by forge as the `.png` formatter.

GIMP opens every path the format command is handed; this processes each image
it opened and saves it back over the same file:

1. Clear the background by flood-filling inward from the corners and edge
   midpoints (threshold 0.25): an image model cannot paint an exact key
   colour, so the background is whatever touches the frame.
2. Crop to a square around what is left, so the object fills the sprite.
3. Stretch contrast, so the shading spans the palette.
4. Scale to 32x32 with no interpolation.
5. Snap to PALETTE with no dithering.
"""

from gi.repository import Gegl, Gimp

SIZE = 32
PALETTE = [
    "#000000", "#2b1d0e", "#4a2f1a", "#6e4526", "#94603a", "#c08552", "#e0b07a",
    "#3a3a3a", "#6b6b6b", "#a0a0a0", "#d4d4d4", "#ffffff", "#ffd23f", "#b8860b",
]

for image in Gimp.get_images():
    layer = image.get_layers()[0]
    layer.add_alpha()
    Gimp.context_set_sample_threshold(0.25)
    Gimp.context_set_antialias(False)
    Gimp.context_set_feather(False)
    w, h = image.get_width(), image.get_height()
    seeds = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
             (w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)]
    for index, (x, y) in enumerate(seeds):
        op = Gimp.ChannelOps.REPLACE if index == 0 else Gimp.ChannelOps.ADD
        image.select_contiguous_color(op, layer, x, y)
    layer.edit_clear()
    Gimp.Selection.none(image)

    image.select_item(Gimp.ChannelOps.REPLACE, layer)
    _ok, _non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
    Gimp.Selection.none(image)
    side = int(max(x2 - x1, y2 - y1) * 1.07)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    image.resize(side, side, -(cx - side // 2), -(cy - side // 2))
    layer.resize_to_image_size()

    layer.levels_stretch()
    Gimp.context_set_interpolation(Gimp.InterpolationType.NONE)
    image.scale(SIZE, SIZE)

    palette = Gimp.Palette.new("finish")
    try:
        for index, colour in enumerate(PALETTE):
            palette.add_entry(f"c{index}", Gegl.Color.new(colour))
        image.convert_indexed(
            Gimp.ConvertDitherType.NONE, Gimp.ConvertPaletteType.CUSTOM,
            0, False, False, palette.get_name(),
        )
    finally:
        palette.delete()
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, image.get_file(), None)
