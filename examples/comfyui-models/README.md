# ComfyUI image models

Two image models, as a project declares them in `.hybridforge/config.json`,
with the API-format workflows they run. Both were run end to end on the phase 4
machine (docs/CREATIVE-TOOLS.md) through forge:

| Model | Workflow | Does | Measured |
|---|---|---|---|
| `pixel` | SDXL base + pixel-art-xl LoRA, 30 steps | generate | 12.3 s, first render after start |
| `kontext` | Flux.1 Kontext dev fp8, 20 steps | edit by instruction | 15.8 s |
| `flux2` | Flux.2 dev fp8 + turbo LoRA, 8 steps | generate | 43.5 s cold, 8.1 s warm; the whole card |

```
forge comfy start
forge comfy render --model pixel --prompt "pixel art, a treasure chest" --seed 41 --out chest.png
forge comfy render --model kontext --edit chest.png --prompt "Make the iron bands gold." --out gold.png
forge comfy stop
```

## Making a workflow

1. Build it in ComfyUI's UI and run it once.
2. Export it with **Workflow → Export (API)**. The ordinary save is the UI
   format, which forge refuses by its shape.
3. Point a `bind` entry at each input forge fills: `prompt`, `negative`,
   `seed`, `width`, `height` for generating; `prompt`, `seed`, `image` (and
   optionally `mask`) for editing. Each is `["<node id>", "<input name>"]`,
   read off the exported JSON.

Every model file a workflow names must be on disk: forge checks them against
ComfyUI's loaders before it calls the server ready.

## What the three models are good for

From phase 0 and this check, on the same machine:

- **SDXL + pixel-art LoRA** is fast and draws the style, but follows a prompt
  loosely: asked for an anvil it drew something else, and asked for a chest it
  drew two stacked boxes with a floating lid. Good for tiles, textures and
  simple icons; not for a specific object.
- **Kontext** changes what it is told to and keeps the rest — the bands went
  gold, the wood did not. It also reframes slightly (its input is rescaled to
  its preferred resolution), so two edits of one image are not pixel-aligned.
- **Flux.2 with the turbo LoRA** follows a prompt closely and draws even
  textures: asked for a meadow texture with "a few tiny flowers", it drew an
  even field with a few tiny flowers, where SDXL drew a dark border frame and
  orange blobs everywhere. Use it where the prompt matters and let GIMP supply
  the pixel-art look. It needs all 32 GB, so every render is a lease.

