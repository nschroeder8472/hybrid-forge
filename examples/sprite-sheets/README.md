# Sprite sheets: a key and its lock

Eight-direction sprite sheets for two props, each in three colourways, built
the way games build sprites from 3D: one model, one fixed camera, rendered at
every angle and finished identically, so every frame matches every other.

```
art/scenes/common.py     helpers, COLOURS (red, blue, green), STEEL, DARK, box()
art/scenes/crate.py      the reference scene
art/tools/turntable.py   renders a scene at 0, 45 ... 315 degrees, per colour, on #ff00ff
art/tools/capture.py     one strip of all eight directions per colour, for the reviewer
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
| `sheets/key.png` | 512 x 192: rows red, blue, green; columns 0, 45, ... 315 degrees; 64 x 64 frames |
| `sheets/key.json` | `frame_size`, `rows`, `columns`, and `frames["blue_45"] = [x, y, w, h]` for every frame |
| `sheets/lock.png`, `sheets/lock.json` | the same, for the padlock |

The JSON is what a game engine reads to cut the sheet into frames; the frame
name is `<colour>_<degrees>`.

## Choices that keep frames consistent

- **One scale per prop, from its projection.** Seen from 35 degrees up, a prop
  fills `2 * radius` across and `top * cos + 2 * radius * sin` down; every
  rotation fits inside that, so the prop never grows or shrinks as it turns.
  The first version scaled by height alone and clipped the lock's shackle.
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
