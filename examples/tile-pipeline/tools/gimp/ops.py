"""Image operations shared by the GIMP tools. Imported inside GIMP only.

Every tool in this folder is a short script: `args()` for its inputs, `load()`
the image, call operations from here, `save()` the result. Operations take a
`Gimp.Image` and change it in place.

Notes on the GIMP 3.2 API, each one learned by getting it wrong:

- `Gimp.Image.convert_indexed` takes a palette's **name**, a string, not the
  `Gimp.Palette` object.
- `Gimp.Palette.new(name)` returns a palette whose name may differ from the one
  asked for (`"x #1"` when `"x"` exists), so use `palette.get_name()`.
- A palette made with `Gimp.Palette.new` is saved into the user's GIMP profile
  and stays there until `palette.delete()`. Delete it in a `finally`, or every
  failed run leaves one behind.
"""

import json
import os

from gi.repository import Gimp, Gio


def args():
    """The arguments `runner.run` passed, as a dict."""
    return json.loads(os.environ["GIMP_TOOL_ARGS"])


def load(path):
    """Open the image at `path`."""
    return Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))


def save(image, path):
    """Write `image` to `path`, in the format its extension names."""
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(path), None)


def grayscale(image):
    """Convert `image` to grayscale."""
    image.convert_grayscale()
