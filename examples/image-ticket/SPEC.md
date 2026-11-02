# Image ticket: a treasure chest icon

Phase 5 of docs/CREATIVE-TOOLS.md: the first image ticket. The executor writes
a prompt, SDXL with a pixel-art LoRA renders it, GIMP finishes it into a 32x32
sprite as the `.png` formatter, the tester checks what a script can measure,
and the reviewer looks at it.

## IMG-001: A treasure chest icon

**Kind:** image

### Spec

A 32x32 pixel-art icon of a single closed wooden treasure chest, seen from the
front and slightly above, for a game inventory. Brown wooden planks, dark metal
bands across the lid and the body, and a gold lock plate on the front with a
single red gem set in its centre. One
chest, whole, with nothing else in the frame.

The image model renders at 1024x1024 into `assets/chest.png`. The project's
formatter (`tools/gimp/finish.py`) then clears the background, crops to the
chest, stretches contrast, scales to 32x32 and snaps to this palette:
`#000000`, `#2b1d0e`, `#4a2f1a`, `#6e4526`, `#94603a`, `#c08552`, `#e0b07a`, `#3a3a3a`, `#6b6b6b`, `#a0a0a0`, `#d4d4d4`, `#ffffff`, `#ffd23f`, `#b8860b`, `#d02a2a`, `#7a1414`. Write the prompt for the 1024x1024 render; the formatter does the
rest.

What the review should see: one closed chest that reads as a treasure chest at
32x32 — a lid, a body, bands and a lock — centred, on a transparent background.
The lock plate must be gold, not silver or grey, and the red gem must be
visible at 32x32: the palette's reds, `#d02a2a` and `#7a1414`, are there for it.
Two boxes stacked, an open lid, coins spilling out, or more than one object is
a failure, even if every criterion below passes.

### Allowed files

- assets/chest.png
- tests/chest_test.py

### Reference files

- tests/sample_test.py
- tools/gimp/finish.py

### Acceptance criteria

- `assets/chest.png` opens with Pillow at size (32, 32).
- Converted to RGBA, the four corner pixels have alpha 0.
- Converted to RGBA, every pixel has alpha 0 or 255.
- Converted to RGBA, between 250 and 900 pixels have alpha 255.
- Converted to RGBA, the RGB of every pixel with alpha 255 is one of the
  palette colours listed in the spec.
- Converted to RGBA, the pixels with alpha 255 use at least 5 distinct colours.

### Context

The tests open `assets/chest.png` with Pillow, as `tests/sample_test.py` opens
its fixture. The formatter has already run when they do: they test the 32x32
sprite, never the 1024x1024 render.
