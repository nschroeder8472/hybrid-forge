# An image ticket

Phase 5 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md): one ticket of
`**Kind:** image`, run end to end. The executor writes a prompt, SDXL with a
pixel-art LoRA renders it through ComfyUI, GIMP finishes it into a 32x32
sprite as the project's `.png` formatter, a check script the tester writes
tests what can be measured, and the reviewer looks at the sprite.

```
SPEC.md                    IMG-001: a 32x32 treasure chest icon
tools/gimp/finish.py       the formatter: clear background, crop, stretch, 32x32, palette
tests/sample_test.py       the reference test: Pillow opens a PNG and asserts on pixels
.hybridforge/config.json   imageModels.pixel, loop.imageModel, format for .png
```

## Requirements

- ComfyUI with `sd_xl_base_1.0.safetensors` and `pixel-art-xl-v1.1.safetensors`,
  and a `comfyui` block in the machine profile so forge can run it.
- GIMP 3.2 with `gimp-console-3` on PATH, or its full quoted path in the
  `format` command.
- A `claude` model with `"tools": "Read"` for `loop.viewModel`.

Then, from a copy of this directory made into a git repository:

```
forge ingest SPEC.md
forge go
```

`forge go` starts `llama-server` and ComfyUI, because the backlog has an image
ticket, and stops both at the end.

## What the first run did

13.1 minutes, three attempts, done. Attempt 1 drew a clean chest with yellow
wood; the checks passed and the reviewer rejected it for reading gold, not
brown. Attempt 2's prompt asked the chest to fill "most of the frame", SDXL
drew it edge to edge, and the background flood fill ate into it: 82 opaque
pixels against a floor of 250, caught by the check script. Attempt 3 fixed
both and was accepted. The prompt guide in this config now asks for a white
margin, because the finisher depends on one.
