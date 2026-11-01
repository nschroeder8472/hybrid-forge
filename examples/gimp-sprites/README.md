# GIMP sprite tools

Phase 2 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md). The loop
writes Python-Fu scripts; GIMP 3.2 runs them headless. Together the three
tickets in `SPEC.md` turn a render on a `#ff00ff` key into a pixel-art sprite:
key out the background, pixelate, snap to a palette. The fixture is a Blender
render of phase 1's anvil, so this is the second half of a Blender → GIMP
pipeline.

```
tools/gimp/runner.py     runs a Python-Fu script in gimp-console from ordinary Python
tools/gimp/ops.py        shared operations, imported inside GIMP; the tickets add to it
tools/gimp/grayscale.py  the reference tool
tests/grayscale_test.py  the reference test: Pillow in, GIMP once per class, Pillow out
tools/capture.py         runs the tools an attempt wrote on fixtures/anvil.png
fixtures/anvil.png       the phase 1 anvil, rendered by Blender on #ff00ff, no anti-aliasing
```

## Requirements

- GIMP 3.2. `runner.py` finds `gimp-console-3` through `GIMP_CONSOLE`, then
  PATH, then the default Windows install location.
- Python with Pillow and flake8, for the tests and the lint step.
- The `claude` model with `"tools": "Read"`, which `loop.viewModel` names.

## Running it

Copy this directory outside the forge checkout and make it a git repository:

```
git init && git add -A && git commit -m "start"
forge ingest SPEC.md
forge go
```

By hand:

```
python -m unittest discover -s tests -p "*_test.py" -t .
python tools/capture.py          # writes capture_out/
```

## What GIMP 3.2 does that the spec has to say

- **`convert_indexed` takes a palette's name**, not the `Gimp.Palette`, and
  `Gimp.Palette.new("x")` may return `"x #1"`, so the name comes from
  `get_name()`.
- **A palette made in a batch run is saved into the user's GIMP profile** and
  stays until `delete()`. A tool that fails between creating and deleting one
  leaves it behind, so the delete goes in a `finally`.
- **An indexed PNG's transparency lives on an unused palette slot**, or on one
  GIMP adds. A transparent pixel's RGB is whatever that slot holds, so tests
  compare transparent pixels by alpha alone.
- **A batch session needs `--quit`**, or it waits for more commands after the
  script finishes. A script that raises exits 64, which `runner.py` turns into
  an exception carrying GIMP's output.
