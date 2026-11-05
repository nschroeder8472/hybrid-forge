"""Finish a rendered image into a game asset. Run by forge as the `.png` formatter.

GIMP opens every path the format command is handed; this processes each image
it opened and saves it back over the same file.

A board (any file whose name contains "board") is only scaled to 768x768: it is
the whole square, background included.

A mark (everything else — the X and the O) is cut out:

1. Clear the background by flood-filling inward from the corners and edge
   midpoints (threshold 0.25): an image model cannot paint an exact key colour,
   so the background is whatever touches the frame. Ask for a plain white
   background with a margin, and the mark must not touch the frame.
2. Crop to a square around what is left, with a 10% margin.
3. Scale to 256x256 with cubic interpolation, keeping soft edges.
"""

from gi.repository import Gegl, Gimp

MARK = 256
BOARD = 768

for image in Gimp.get_images():
    layer = image.get_layers()[0]
    name = image.get_file().get_basename().lower()
    Gimp.context_set_interpolation(Gimp.InterpolationType.CUBIC)

    if "board" in name:
        image.scale(BOARD, BOARD)
    else:
        layer.add_alpha()
        Gimp.context_set_sample_threshold(0.25)
        Gimp.context_set_antialias(True)
        Gimp.context_set_feather(False)
        w, h = image.get_width(), image.get_height()
        seeds = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
                 (w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)]
        for index, (x, y) in enumerate(seeds):
            op = Gimp.ChannelOps.REPLACE if index == 0 else Gimp.ChannelOps.ADD
            image.select_contiguous_color(op, layer, x, y)
        # Enclosed background too — the inside of an O does not touch the
        # frame, so the flood fill above never reaches it. Every near-white
        # pixel is background on a mark: the marks are coloured chalk.
        image.select_color(Gimp.ChannelOps.ADD, layer, Gegl.Color.new("#ffffff"))
        layer.edit_clear()
        Gimp.Selection.none(image)

        image.select_item(Gimp.ChannelOps.REPLACE, layer)
        _ok, _non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
        Gimp.Selection.none(image)
        side = int(max(x2 - x1, y2 - y1) * 1.2)
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        image.resize(side, side, -(cx - side // 2), -(cy - side // 2))
        layer.resize_to_image_size()
        image.scale(MARK, MARK)

    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, image.get_file(), None)
