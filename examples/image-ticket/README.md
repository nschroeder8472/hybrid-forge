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

## The edit loop (phase 6)

`loop.editModel` names `kontext`, a Flux Kontext workflow that edits an image
by instruction. When a rejection is about a detail, the reviewer ends it with
`MODE: edit`, and the executor may answer with `EDIT: <instruction>` instead
of a new prompt; Kontext edits the previous attempt's raw render and the
result goes through the same formatter, checks and review. Rejections also
name `ISSUES:`, and `loop.imageStall` (3) parks a ticket whose last three
rejections share one.

The current `SPEC.md` asks for a red gem on the lock plate — a detail SDXL
draws too small to survive 32x32. Its run: 11.3 minutes, four attempts. A
render on a brick floor failed the coverage check; a good render lost its gem
at 32x32 (`missing-gem`, `MODE: edit`); a Kontext edit kept the composition
pixel for pixel and fixed the wood and the lock but not the gem; a second edit
made the gem a large solid red block, which survived, and was accepted.

## What the first run did

13.1 minutes, three attempts, done. Attempt 1 drew a clean chest with yellow
wood; the checks passed and the reviewer rejected it for reading gold, not
brown. Attempt 2's prompt asked the chest to fill "most of the frame", SDXL
drew it edge to edge, and the background flood fill ate into it: 82 opaque
pixels against a floor of 250, caught by the check script. Attempt 3 fixed
both and was accepted. The prompt guide in this config now asks for a white
margin, because the finisher depends on one.
