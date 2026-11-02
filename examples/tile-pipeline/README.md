# Tile pipeline

Phase 7 of [docs/CREATIVE-TOOLS.md](../../docs/CREATIVE-TOOLS.md): one backlog
across all three tools, each ticket's output the next one's input.

| Ticket | Tool | Makes |
|---|---|---|
| ART-001 | GIMP (Python-Fu) | `ops.to_tile`: seamless (`gegl:tile-seamless`), 64x64, palette |
| ART-002 | ComfyUI (Flux.2 turbo), then GIMP as the `.png` formatter | `assets/grass.png`, a seamless 64x64 grass tile |
| ART-003 | Blender | `art/scenes/meadow.py`, a 4 m plane with the tile repeated 4x4 |

Two workspaces: the repository root (GIMP tools, Pillow tests, the `.png`
formatter) and `art/` (Blender tests and capture). `forge go` starts
`llama-server` and ComfyUI, because ART-002 is an image ticket.

Requirements: everything in `examples/blender-props` and
`examples/gimp-sprites`, plus ComfyUI with the Flux.2 dev fp8 checkpoint, its
Mistral text encoder, its VAE and the turbo LoRA, and a `comfyui` block in the
machine profile. `gimp-console-3` and `blender` on PATH, or their full quoted
paths in `.hybridforge/config.json`.

```
forge ingest SPEC.md
forge go
```

## What the first run did

3/3 done in 88.8 minutes, about 25 of them lost to a GPU that was not clear at
the start (see the plan). ART-001 took four attempts on GIMP's palette API,
which the spec now states exactly. ART-002 took two: a dark render fell short
of the greens criterion, which now counts all six greens. ART-003 recovered,
on a retry cycle, from a sign-off revision that had rewritten two measured
facts; they are now protected decisions.
