# Sprite sheets: a key and its lock

Sprite sheets for two props — eight isometric directions and four flat,
face-on views —, each in three colourways, built
the way games build sprites from 3D: one model, one fixed camera, rendered at
every angle and finished identically, so every frame matches every other.

```
art/scenes/common.py     helpers, COLOURS (red, blue, green), STEEL, DARK, box()
art/scenes/crate.py      the reference scene
art/tools/turntable.py   renders a scene at 0, 45 ... 315 degrees and four flat views, per colour, on #ff00ff
art/tools/capture.py     one strip of all twelve frames per colour, for the reviewer
tools/gimp/sprites.py    keys out, pixelates to 64x64 and palettes many frames in one GIMP start
tools/make_sheets.py     turntable -> sprites -> pack_sheet, into sheets/
SPEC.md                  SHT-001 key, SHT-002 lock, SHT-003 the packer
```

## Making the sheets

Once the backlog has landed:

```
python tools/make_sheets.py
```

writes, in about 30 seconds:

| File | What it is |
|---|---|
| `sheets/key.png` | 768 x 192: rows red, blue, green; columns 0, 45, ... 315 degrees, then flat_0, flat_90, flat_180, flat_270; 64 x 64 frames |
| `sheets/key.json` | `frame_size`, `rows`, `columns`, and `frames["blue_45"] = [x, y, w, h]` for every frame |
| `sheets/lock.png`, `sheets/lock.json` | the same, for the padlock |

The JSON is what a game engine reads to cut the sheet into frames; the frame
name is `<colour>_<degrees>` (`blue_45`) or `<colour>_flat_<degrees>`
(`blue_flat_90`).

## The two kinds of view

- **Isometric** (columns `0` ... `315`): seen from 35 degrees up, the usual
  angle for an isometric or 3/4 game, turned in eight 45-degree steps.
- **Flat** (columns `flat_0` ... `flat_270`): seen face-on, with no
  perspective tilt, for side-on or top-down games and for inventory icons,
  turned in four 90-degree steps. "Face-on" means looking along the prop's
  thinnest axis, which the renderer reads from the geometry: the lock stands
  up, so it is seen at eye level (front, side, back, side); the key lies flat,
  so it is seen from straight above, since at eye level it is a thin line.

Each kind has its own scale, so the flat frames are larger than the
isometric ones: each set is fitted to fill its frame.

## Choices that keep frames consistent

- **One scale per prop, from its projection.** Seen from 35 degrees up, a prop
  fills `2 * radius` across and `top * cos + 2 * radius * sin` down; every
  rotation fits inside that, so the prop never grows or shrinks as it turns.
  The first version scaled by height alone and clipped the lock's shackle.
- **No specular highlights.** A face square to the camera catches the whole
  highlight and turns pale; the flat views are all square faces.
- **No cavity shading.** Its fine highlights snap to scattered palette entries
  and read as grain at 64 x 64.
- **No contrast stretch.** It is computed per image, so the same red would land
  on a different palette entry from frame to frame. Studio lighting and a
  four-shade ramp per colour do that job consistently.
- **Anti-aliasing and dithering off**, on a flat `#ff00ff`, so the background
  keys out exactly.

Requirements: Blender 5.2 (`BLENDER`, PATH, or the default install), GIMP 3.2
(`GIMP_CONSOLE`, PATH, or the default install), Python with Pillow and flake8,
and a `claude` model with `"tools": "Read"` for `loop.viewModel`.
