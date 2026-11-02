"""Turn many renders on a #ff00ff key into pixel-art sprites, in one GIMP start.

Arguments: `frames`, a list of [input, output] path pairs; `size`, the sprite's
side; `palette`, a list of "#rrggbb" colours. For each frame: key out the
background (threshold 0.1, for the odd near-key pixel), scale to `size` with no
interpolation, snap to the palette with no dithering, and save.

No contrast stretch: it is computed per image, so the same red would land on a
different palette entry from one frame to the next, and a sheet's frames have
to match each other.
"""

import json
import os

from gi.repository import Gegl, Gimp, Gio

args = json.loads(os.environ["GIMP_TOOL_ARGS"])
palette = Gimp.Palette.new("sheet")
try:
    for index, colour in enumerate(args["palette"]):
        palette.add_entry(f"c{index}", Gegl.Color.new(colour))
    for source, target in args["frames"]:
        image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(source))
        layer = image.get_layers()[0]
        layer.add_alpha()
        Gimp.context_set_sample_threshold(0.1)
        Gimp.context_set_antialias(False)
        Gimp.context_set_feather(False)
        image.select_color(Gimp.ChannelOps.REPLACE, layer, Gegl.Color.new("#ff00ff"))
        layer.edit_clear()
        Gimp.Selection.none(image)
        Gimp.context_set_interpolation(Gimp.InterpolationType.NONE)
        image.scale(args["size"], args["size"])
        image.convert_indexed(
            Gimp.ConvertDitherType.NONE, Gimp.ConvertPaletteType.CUSTOM,
            0, False, False, palette.get_name(),
        )
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(target), None)
        image.delete()
finally:
    palette.delete()
