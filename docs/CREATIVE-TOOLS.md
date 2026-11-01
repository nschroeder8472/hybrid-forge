# Blender, GIMP and ComfyUI in the loop — plan

**Status:** plan; phase 0 measured 2026-11-01 (see *Phase 0 — what was
measured*). Nothing in this document is built and no ticket has run through
any of these tools. It builds on [IMAGE-LOOP.md](IMAGE-LOOP.md), which
designed an image ticket kind and left its first decision open (*which
backend*), and on the capture step from *Looking at what it built* in
[ROADMAP.md](ROADMAP.md), which is the half of that design that has evidence.

The three tools enter the loop in three different places, and most of the
plan is about not treating them as one thing:

| Tool | What it is to the loop | Writes the artifact | New loop machinery |
|---|---|---|---|
| **Blender** | the project's own build — a renderer and a checker | the executor, as a `bpy` script (text) | none |
| **GIMP** | the project's own post-processor | the executor, as a Python-Fu script (text) | none |
| **ComfyUI** | an image model, in a process forge runs | the image model, as pixels | `ImageProvider`, the image kind, a GPU lease, a managed process |

Blender and GIMP are the cheap half: the executor writes Python, which is the
artifact this loop already produces, and the tools are run through `commands`,
which is how the loop already runs a compiler. ComfyUI is the expensive half and
is IMAGE-LOOP phases 2-6 with the backend question answered.

The ordering rule is the one IMAGE-LOOP already uses: build first what is
useful whether or not the rest survives contact.

---

## What is on this machine

Recorded 2026-11-01, so later numbers can be read against it.

| | |
|---|---|
| GPU | RTX 5090, 32,607 MiB |
| RAM | 94 GB |
| Blender | 5.2, `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` |
| GIMP | 3.2, `%LOCALAPPDATA%\Programs\GIMP 3\bin\gimp-console-3.exe` |
| ComfyUI | `C:\Users\nschr\Documents\ComfyUI`, 0.43.0, ComfyUI-Manager only. Its own venv at `.venv` on Python 3.13.16 with PyTorch 2.14.1+cu130, since phase 0; before that it ran on the shared Microsoft Store Python |

ComfyUI models:

| Model | Size | What it is good for here |
|---|---:|---|
| `sd_xl_base_1.0` | 6.5 GB | fast generate at 1024²; the base for the pixel-art LoRA |
| `pixel-art-xl-v1.1` (LoRA) | 0.2 GB | sprites, tiles, icons — the mechanically checkable first ticket IMAGE-LOOP asks for |
| `flux1-dev-kontext_fp8_scaled` | 11.1 GB | **instruction editing**: "move the anvil left", given the previous image. This is what makes `supports_edit` true |
| `flux2_dev_fp8mixed` + `mistral_3_small_flux2_bf16` | 33.0 + 33.1 GB | highest quality, and larger than the card on its own; offloaded to RAM, slow. Hero art only, if at all |
| `Flux_2-Turbo-LoRA` | 2.6 GB | fewer steps for Flux.2, which is the only thing likely to make it usable in a loop |
| `ae`, `clip_l`, `t5xxl_fp8` | | Flux.1 VAE and text encoders |

What is missing and would matter later: no ControlNet, no upscaler, no
inpainting checkpoint. None is needed to start. Each is worth acquiring only
when a recorded ticket fails for its absence — a size the model cannot render
natively, a composition the prompt cannot hold, a local fault an instruction
edit keeps missing.

---

## The constraint that shapes everything: one card

The README's known-good pairing keeps one ~25 GiB llama checkpoint resident at a
time, `exclusive`, `--models-max 1`. That leaves about 7 GB.

Peak whole-card usage, measured in phase 0. The desktop alone holds about
1.7 GB, and a loaded Nemotron checkpoint brings the card to 26.9 GB:

| Consumer | Peak card usage | Fits beside a resident llama checkpoint |
|---|---|---|
| Blender Workbench capture, 4 views at 1024² | not measured; small | yes |
| Blender Cycles on GPU | not measured; scene-dependent | sometimes |
| GIMP batch | none | yes |
| ComfyUI SDXL + LoRA | 11.7 GB | no |
| ComfyUI Flux Kontext fp8 | 21.5 GB | no |
| ComfyUI Flux.2 fp8 | 31.8 GB, the whole card | no, and not beside anything |

What the table says is that the failure mode without arbitration is not an error. ComfyUI
offloads to system RAM when it cannot fit and gets an order of magnitude slower;
llama-server reloads and races. A run that is merely slow reports nothing, which
is the class of failure this repository keeps finding the expensive way.

So the GPU is a resource the loop has to lease, and the lease is phase 3 —
after the tools that do not need it, before the one that does.

---

## Blender — the project's own build

Blender is the UI-capture shape, not the image-generation shape. The artifact is
produced by the project's build from source the executor wrote, so there is no
image provider to choose and the refinement input is an ordinary code edit.

### A ticket

```markdown
## SCN-001 — low-poly anvil prop

**Route:** delegate

## Spec

A procedural anvil built by `art/scenes/anvil.py`: a horn, a face, a waist and a
base, as one mesh object named `Anvil`, exported to `build/anvil.glb`.

## Allowed files

- art/scenes/anvil.py

## Reference files

- art/scenes/crate.py

## Acceptance criteria

- One mesh object named `Anvil`
- Under 2,000 triangles
- Bounding box 0.6 m ± 0.05 long, 0.25 m ± 0.05 tall
- Manifold: no non-manifold edges
- Has a UV map
- The horn points along +X
- Seen from the front camera, the silhouette reads as an anvil, not a box
```

Every criterion but the last two is a `bpy` assertion. The last two are for the
viewing seat, which is exactly the split IMAGE-LOOP asks for.

### Configuration, no code

Scene scripts are `.py`, which collides with the project's Python test command.
That is what [workspaces](WORKSPACES.md) exist for — a directory with its own
commands and its own `cwd`:

```json
"workspaces": [
  {
    "root": "art",
    "commands": {
      "test": {
        ".py": "blender -b --factory-startup --python-exit-code 1 --python tests/run_checks.py"
      },
      "capture": {
        ".py": "blender -b --factory-startup --python-exit-code 1 --python tools/capture.py"
      }
    }
  }
]
```

- **`--python-exit-code 1` is load-bearing.** Without it Blender exits 0 when the
  script raises, so a failing check script passes verification. This is the
  first thing phase 0 confirms on 5.2, and the first thing `/forge-spec-check`
  should look for in a Blender command.
- **`--factory-startup`** keeps the user's preferences and add-ons out of a
  verify step, the same reason a test runner is not run with a developer's shell
  profile.
- **The tester writes `tests/check_<ticket>.py`, which imports `bpy`.** It only
  runs inside Blender's bundled Python. The daemon never imports `bpy`, so
  stdlib-only holds exactly as it does for Pillow in IMAGE-LOOP.
- **`tools/capture.py` renders into `FORGE_CAPTURE_DIR`**: front, side, top and a
  three-quarter view, Workbench or EEVEE, fixed resolution, fixed seed, fixed
  colour management. Every image there is attached to the review, as for a UI
  capture. Workbench is the default because it fits beside a resident llama
  checkpoint and needs no lease.
- **Exports are build products.** `build/anvil.glb` is written by the build
  command and checked by the test, never written by the executor and never in
  `Allowed files`. Whether it is committed is the project's choice; the loop
  needs it only to exist after verify.

The UI-replay lesson about subpixel antialiasing applies here in a different
form: a capture whose colour management or sample count varies between attempts
gives the reviewer a difference nobody chose. Pin them in `capture.py`.

### What it is good for

Procedural props and levels, scene assembly for a game, turntable renders of a
model, product shots, diagrams with depth. Anything whose source can be a
script and whose correctness is partly geometric.

What it is not good for: sculpted or organic models. A `bpy` script that
produces a convincing hand is not a ticket an executor will land, and the
criteria gate below should say so before it is tried.

---

## GIMP — the project's own post-processor

GIMP 3 runs Python 3 plug-ins headless through `gimp-console-3`. It is the most
replaceable of the three tools: for checking an image, Pillow is cheaper to
start and easier for a tester to write, and the check scripts should stay
Pillow. GIMP earns its place on the jobs only it does well here:

- conversion to an indexed palette, with a chosen dither, against a project
  palette file — the step that turns an SDXL pixel-art rendering into an actual
  16-colour sprite;
- layered `.xcf` output for a human to keep editing;
- GEGL filters (seamless tiling, unsharp, colour transfer) without writing them;
- text layers in the project's own fonts.

### Two positions, both existing shapes

**As a tool the executor writes.** A ticket whose allowed file is
`tools/gimp/to_sprite.py` and whose test runs it over a fixture and asserts the
output with Pillow. This is an ordinary code ticket; GIMP is the interpreter.

**As the finishing pass on a generated image.** `commands.format` already
rewrites the files an attempt landed, runs before verification, and is never the
ticket's failure:

```json
"commands": {
  "format": { ".png": "gimp-console-3 -i --batch-interpreter=python-fu-eval -b \"exec(open('tools/gimp/finish.py').read())\" --quit" }
}
```

That is the right semantics for a quantise-to-palette pass. If GIMP fails, the
format step logs and skips, and the palette criterion in the check script fails
on its own — so the failure still surfaces, attributed to the criterion rather
than to the tool. **This needs confirming against the code** before it is
relied on: `format` was written for text formatters, and whether it appends a
binary path the same way, and whether `_written_but_unchanged` survives a binary
file it rewrote, is unchecked.

The exact batch invocation is the second thing phase 0 confirms. A GIMP batch
that never receives its quit hangs, and a hung verify command is a hung run.

---

## ComfyUI — the image model

This is IMAGE-LOOP's `ImageProvider`, with the backend chosen. ComfyUI is a
good fit for the abstraction because it is a graph executor rather than one
model: the same adapter reaches SDXL, Kontext and Flux.2, and which one runs is
a workflow file the project owns.

### The adapter

Stdlib only, like every other adapter:

1. `POST /prompt` with an API-format workflow graph and a `client_id`. A reply
   carrying `node_errors` is a configuration error, not a retry.
2. Poll `GET /history/{prompt_id}` until the graph finishes.
3. Fetch each output with `GET /view?filename=…&subfolder=…&type=output`.
4. `POST /interrupt` when the run is stopped mid-render.

`health()` is `GET /system_stats`, which also reports free VRAM — what
`forge doctor` should print beside the llama-server status.

ComfyUI is started by forge, under the same rules as `llama-server`; see
*Forge runs the servers*.

### Workflows are project files

```json
"imageModels": {
  "pixel": {
    "kind": "comfyui",
    "url": "http://127.0.0.1:8188",
    "workflows": {
      "generate": ".hybridforge/comfy/sdxl-pixel.json"
    },
    "bind": {
      "prompt":   ["6", "text"],
      "negative": ["7", "text"],
      "seed":     ["3", "seed"],
      "width":    ["5", "width"],
      "height":   ["5", "height"]
    }
  },
  "kontext": {
    "kind": "comfyui",
    "url": "http://127.0.0.1:8188",
    "workflows": {
      "edit": ".hybridforge/comfy/flux-kontext-edit.json"
    },
    "bind": {
      "prompt": ["6", "text"],
      "seed":   ["25", "noise_seed"],
      "image":  ["41", "image"]
    }
  }
}
```

- A workflow is exported from ComfyUI with **Export (API)**. The UI format is a
  different schema and the adapter must refuse it by shape at `validate()`,
  rather than by a `node_errors` reply at render time.
- `bind` maps the loop's inputs to node inputs. It is the whole interface
  between forge and a graph, and it is checked at `validate()`: a bound node id
  missing from the workflow is a configuration error.
- **Capabilities are read from what is declared, never probed.**
  `supports_edit` is true when an `edit` workflow exists; `supports_mask` when
  a `mask` binding exists; `supports_seed` when `seed` is bound. The seed is
  always one forge chose and sent, so it is always known — IMAGE-LOOP's "a seed
  not reported is unknown, never zero" never arises on this backend.
- `cost_usd` is zero. The real budget on a local run is GPU minutes, which
  `usage` does not record today. Recording render seconds beside `images` is
  cheap and is the number a later budget would want.
- An image input is uploaded with `POST /upload/image` and bound by the name
  ComfyUI returns.

### Who writes the prompt

IMAGE-LOOP maps BUILD to "generate, or edit the previous attempt" without saying
who writes what the image model is given. The proposal: **the executor seat
does, as text.** On an image ticket the executor is asked for a structured
block —

```
PROMPT: pixel art, an iron anvil on a wooden workbench, warm light from the left
NEGATIVE: text, watermark, signature
SEED: 41
MODE: generate
```

— and the daemon renders it on the image model. On an edit attempt it writes an
`INSTRUCTION:` for Kontext from the reviewer's `REGION`, instead of a prompt.

That keeps the seat and swaps the model, which is how `viewModel` already works:
the executor keeps its role, its vote and its text model, and the image ticket
names which image model renders for it:

```json
"loop": { "imageModel": "pixel", "editModel": "kontext" }
```

`ROLES` stays at four. A prompt that failed review is revised by the same text
model that read the review, which is the part of the loop that already
converges.

---

## Forge runs the servers

Today forge installs `llama-server` (`forge llama install`) and writes its
preset (`forge models`), but SETUP §1.3 leaves running it to the user: "a
systemd unit, a scheduled task, `tmux`". This plan has forge start both
`llama-server` and ComfyUI. There are three reasons:

- **Forge already has everything needed to start `llama-server`.**
  `llama.resolve_server` finds the binary and `presets.write` produces the
  `--models-preset` file. The only thing left to the user is copying the
  command line out of SETUP, and getting `--models-max` or the bind address
  wrong there costs a run without raising an error.
- **The GPU lease needs a fallback that always works.** When a server will not
  give the card back, forge can only kill and restart it if forge owns the
  process.
- **One machine, two GPU servers.** Each one's VRAM use depends on the other.
  Starting them from one place, in a known order, is how the lease reasons
  about free memory at all.

This is the first time forge starts long-lived processes, so the rules are
shared and written once. `processes.py` gains a small supervisor, and both
servers use it.

### The shared rules

**Opt-in, by machine.** A server is forge's to run when the machine profile
has a block for it. With no block, nothing changes from today: forge talks to
whatever answers at the configured address.

**Address decides ownership.** A model whose address is loopback at the
managed port, or is absent, uses the server forge runs. A model with any other
address — the GPU host in SETUP's split-machine layout, or Part 1b's container
— is external. Forge only probes an external server, and refuses the run if it
does not answer. `llamacpp` already defaults an absent `baseUrl` to
`127.0.0.1:8080`, so an existing config that omits it keeps working unchanged.

**Bind to loopback, always.** A server forge starts listens on `127.0.0.1`.
Neither server authenticates, and SETUP's warning about binding `llama-server`
to `0.0.0.0` applies to both. A machine that has to serve another host runs its
servers itself and is configured as external.

**Ready means serving what the project asked for.** A server that listens is
not ready yet. `llama-server` is ready when `/v1/models` lists every model id
the project's `llamacpp` roles name. ComfyUI is ready when `/object_info` shows
every node and model file the project's workflows use. A missing id or file
fails the run at startup with its name, before any model call.

**Adopt, never steal.** If the port already answers as the right kind of server
serving the right models, forge uses it. If it answers with the wrong models,
for example another project's preset, the run refuses and names the models
that are missing. Forge does not restart a server it did not start. If the
port is held by something else entirely, the run refuses and names the port.

**Ownership is recorded, so a resumed daemon keeps it.** The daemon is built to
survive being killed, and on Windows its children outlive it. A forge-started
`llama-server` that the next daemon sees only as "something answering on
8080" would be adopted, never stopped, and would hold 25 GiB until someone
noticed. So each start writes the pid, executable path and start time to
`.hybridforge/servers.json`. A process that matches the record is still
forge's after a restart. Keeping that 25 GiB checkpoint loaded across a daemon
crash also saves a full reload on resume.

**Logs and crashes.** Each server's stdout and stderr go to
`.hybridforge/logs/<server>.log`. If a server dies mid-run, forge restarts it
once. For `llama-server`, a second death blocks the run with the log's tail
attached, because every later ticket would fail the same way. ComfyUI is the
exception, below.

**Stopping.** At run end, on `forge stop`, on a blocked run and on daemon
exit, forge kills the process tree of every server it started, using
`processes._kill_tree`, and deletes the record. Adopted servers are never
stopped.

### `llama-server`

The profile block:

```json
"llamaServer": {
  "port": 8080,
  "modelsMax": 1,
  "args": []
}
```

Forge builds the command line from what it already knows: the binary from
`llama.resolve_server`, then `--host 127.0.0.1 --port <port>
--models-preset <the preset forge models writes> --models-max <modelsMax>`.
`args` holds anything else the machine needs. An `args` entry that repeats a
flag forge owns is refused at `validate()`, because two `--models-max` values
on one command line is the "silently wrong" case all over again.

Two rules specific to this server:

- **The preset is rewritten before every start.** The preset and the config
  are already kept from drifting by `forge models`. Starting a server from a
  preset older than the config would bring that drift back.
- **It starts whenever a role is `llamacpp`.** Ratify calls every role before
  the first ticket, so unlike ComfyUI there is no backlog on which it would sit
  unused.

`modelsMax` defaults to 1 rather than llama.cpp's 4, for the reason SETUP gives:
the default is fatal on a card that cannot hold every checkpoint at once, and
nothing reports it. A machine with room raises it on purpose.

`forge llama start | stop` join the existing `status | install | list`.
`status` also reports whether a server is running, whether forge started it,
and which preset it was started from.

### ComfyUI

How to start ComfyUI is a fact about the machine, so it goes in the machine
profile, not the project config — the same split `forge-setup` already draws
for model paths:

```json
"comfyui": {
  "root": "C:/Users/nschr/Documents/ComfyUI",
  "python": "C:/Users/nschr/Documents/ComfyUI/.venv/Scripts/python.exe",
  "port": 8188,
  "args": []
}
```

The project's `imageModels` entries name `kind: comfyui` and leave `url` out,
which means *the ComfyUI forge runs*. An entry that sets `url` is a ComfyUI
running somewhere else, such as the GPU host in SETUP's split-machine layout.
Forge never starts that one; it only probes it, and refuses the run if it does
not answer.

**The command line forge owns.** Forge always appends
`--listen 127.0.0.1 --port <port> --disable-auto-launch`. The bind keeps an
unauthenticated server off the network, for the same reason SETUP gives for
`llama-server`. `--disable-auto-launch` stops a daemon opening a browser tab
every time it starts ComfyUI. `args` holds anything else the machine needs,
such as `--lowvram` or `--disable-all-custom-nodes`, and an `args` entry that
repeats a flag forge owns is refused at `validate()`.

**When it starts.** At run start, if the ingested backlog contains an image
ticket, and not otherwise. A backlog of code tickets pays nothing. Starting
before the first ticket rather than lazily at the first image ticket means a
broken install fails the run before any model call has been paid for. That is
better than finding out four tickets in.

**Ready means more than listening.** Forge polls `GET /system_stats` until it
answers, within a startup timeout that phase 0 sets. The last log shows
ComfyUI-Manager alone spending 7.6 s before startup. Once ComfyUI answers,
forge checks every configured workflow against `GET /object_info`: each node
class must exist, and each checkpoint, LoRA, VAE and encoder a workflow names
must be among the files the loader node lists. A workflow naming a model that
is not on disk fails here, at startup, rather than as `node_errors` on the
first render.

**A second crash parks, it does not block.** An exception to the shared rule:
code tickets do not need ComfyUI, so a ComfyUI that dies twice parks the image
tickets with the log's tail attached, and the code tickets carry on.

**While waiting on budget it keeps running.** In `waiting_budget`, after the
lease's `/free`, ComfyUI holds only its CUDA context. Phase 0 measures how large
that is.

`forge comfy start | stop | status` runs the same code by hand, for building a
workflow in the ComfyUI UI against the configuration the loop will use.

---

## The GPU lease

The one piece of genuinely new infrastructure, needed by ComfyUI always and by
Blender only for a Cycles capture.

**Before** an image call, or a command the config marks as needing the card:
unload every resident llama checkpoint through the router's
`/models/unload`, and wait for the slot to free — `llamacpp.py` already does
both, with the measured 0-2.3 s unload and the wait it needs.

**After:** `POST /free` with `{"unload_models": true, "free_memory": true}` to
ComfyUI, so the next llama load finds the card empty.

Marking a command:

```json
"loop": { "exclusiveGpu": ["capture", "image"] }
```

`image` is the image-model call; any `commands` kind can be listed beside it.
Empty by default, which is every run before this existed.

Two consequences worth knowing before a run:

- **Every lease costs a llama reload**, about 25 GiB, after it. A backlog that
  alternates code and image tickets pays it twice per image ticket. Grouping
  image tickets together is worth what grouping `ratifyOrder` by model is worth,
  and for the same reason.
- **The lease must be released on every path out**, including a stopped run and
  a render that times out. A lease that is never released is a run whose next
  llama call finds ComfyUI still holding the card — the silent-slow failure
  again. The release belongs in a `finally`, and `forge doctor` should report a
  ComfyUI holding models while no run is active.

Because forge owns the ComfyUI process, the release has a fallback that is
always available. If `/free` fails or times out, or free VRAM afterwards stays
above what `system_stats` reported before the lease, forge kills ComfyUI and
starts it again. A restart takes seconds; letting the next llama load run into
a full card can cost the rest of the run.

---

## Pipelines across the tools

A ticket still writes one artifact (IMAGE-LOOP decision 5). A pipeline is a
backlog whose later tickets reference what earlier tickets wrote, the same way
the platformer backlog's PT-006 references the test PT-005 creates. For a game
tile set:

| Ticket | Kind | Writes | Checked by |
|---|---|---|---|
| ART-001 | feature | `tools/gimp/to_tile.py` — quantise, make seamless, downscale | Pillow test over a fixture |
| ART-002 | image | `assets/src/grass.png`, via `pixel`; `format` runs ART-001's script over it | check script: 64×64, ≤ 16 colours, edges tile; viewing seat: reads as grass |
| ART-003 | feature | `art/scenes/meadow.py`, tiling ART-002's texture over a plane | `bpy` check; capture reviewed by the viewing seat |

Each step is verified by the cheapest thing that can verify it, and only the
criteria no script can assert reach a vision call. That ordering is the cost
control on a local run as much as on a paid one: a vision review on a local
multimodal checkpoint costs a llama swap.

---

## Who looks

Already configurable, and nothing here changes it. `loop.viewRole` and
`loop.viewModel` name the seat and the model that judge a capture; image tickets
use the same pair. On this machine the candidates are the Nemotron Omni
checkpoint with its projector turned on (`multimodal: true`, preset
regenerated), or `claude-cli`, which can now see. The UI replay found the
stronger seat changed the answer, so for image tickets — where the judge is
nearly the whole decision — the default worth trying first is the strongest
vision model available, through `viewModel`.

Declare `roleNeeds: { "<viewRole>": ["images"] }`. IMAGE-LOOP's invariant §3
(the generating model never judges) is satisfied structurally here — a
diffusion model cannot be seated as a reviewer — but the `validate()` check
should exist anyway, for the day an image model and a vision model are one
endpoint.

---

## Phase 0 — what was measured

Measured on this machine on 2026-11-01, with Blender 5.2.3 LTS, GIMP 3.2,
ComfyUI 0.43.0 on PyTorch 2.14.1+cu130 in its own venv, and llama.cpp b10687.
Each render is 1024², and "warm" means the model was already loaded.

### ComfyUI renders

| Model | Cold | Warm | Peak card usage |
|---|---:|---:|---:|
| SDXL + pixel-art LoRA, 30 steps | 34.0 s | 3.0 s | 11.7 GB |
| Flux.1 Kontext fp8 edit, 20 steps | 28.4 s | 11.9 s | 21.5 GB |
| Flux.2 dev fp8 + turbo LoRA, 8 steps | 60.6 s | 8.1 s | 31.8 GB |
| Flux.2 dev fp8, 20 steps | | 19.8 s | 31.6 GB |

ComfyUI took 18-35 s from launch to answering `/system_stats`; the 35 s start
was the first one after an update. `POST /free` returned the card to 2.2-2.3 GB
within 3 s every time, which leaves about 0.6 GB for ComfyUI's own CUDA context
while idle.

**Flux.2 is usable in a loop, which the plan doubted.** With the turbo LoRA,
a warm render takes 8.1 s. The cost is that it needs the whole card, so every
Flux.2 render is a lease.

What the pictures showed matters more than the times:

- **SDXL with the pixel-art LoRA did not draw an anvil.** The prompt asked for
  an anvil on a workbench, and the result is a workbench with an unidentifiable
  object on it. A visual criterion naming the subject would fail, correctly.
  This model is for tiles, textures and icons whose subject is simple, not for
  depicting a specific object.
- **Kontext edits locally and keeps everything else identical.** Asked to
  paint the anvil red, it changed the object and left the rest of the frame
  untouched. That is the property the edit loop depends on. It also painted a
  saw beside the object red, because it took the object it was told about to
  include the saw. An edit instruction inherits whatever the previous image got
  wrong.
- **Flux.2 drew a convincing anvil, and put text on it.** It stamped "O-F" into
  the anvil's face without being asked. Any image ticket with a "no glyphs"
  criterion needs the OCR assertion in its check script, because this is
  exactly the failure a vision reviewer would likely miss.

### llama-server, pinned build

Router start: 0.5 s. Nemotron Q4_K_M (22.8 GB on disk), three rounds each:
**load 8.7-8.8 s**, unload **2.2 s** until the router reports `unloaded`.

**The lease must wait for the router's status, not for VRAM.** VRAM drops
0.2 s after `/models/unload`, while the router still reports the model as
`loaded`. Loading again inside that window returns
`400 model is already running`. `llamacpp._unload` already waits on status;
the lease must use it rather than a VRAM check of its own.

One lease therefore costs about 11 s of llama time — 2.2 s unload plus 8.8 s
reload — on top of the image model's cold load, which phase 0 measured at
28-61 s from launch. Grouping image tickets is worth roughly 40-70 s per lease
avoided.

### Blender and GIMP

| Check | Result |
|---|---|
| Blender, script raises, no flag | exit **0** — confirmed: a failing check passes |
| Blender, script raises, `--python-exit-code 1` | exit 1 |
| Blender, 4-view Workbench capture at 1024², from launch | 3.8 s |
| GIMP batch, Python-Fu, indexed conversion and PNG export | 21.4 s first run, 2.3 s warm, exits by itself with `--quit` |
| GIMP batch, script raises | exit 64 |

The working GIMP invocation:

```
gimp-console-3 -i -n --quit --batch-interpreter=python-fu-eval -b "exec(open('<script>').read())"
```

**`--factory-startup` does not mean an empty scene.** It still loads the default
cube, camera and light. In the measured capture, the default cube hid the
object being captured, so the reviewer would have been shown a box. A capture
script must clear the scene first, with
`bpy.ops.wm.read_factory_settings(use_empty=True)`, or delete what it did not
create. The reference capture script in phase 1 starts from an empty scene.

### What phase 0 found that was not about measurement

**ComfyUI would not start.** Its system Python had `huggingface-hub` 2.1.1,
which `transformers` 5.10.2 refuses. This was the shared-interpreter risk
listed below, live before a single render. The fix applied: ComfyUI now has its
own venv at `ComfyUI\.venv` on Python 3.13.16, and the profile's
`comfyui.python` points there. ComfyUI itself was 862 commits behind and was
updated to 0.43.0.

**Installing CUDA PyTorch needs `--index-url`, not `--extra-index-url`.** With
the extra index, pip picked PyPI's CPU-only `torch` 2.15.0, because its
version number is higher than the newest cu130 build, 2.14.1. Nothing failed
until a CUDA call raised `Torch not compiled with CUDA enabled`.

**`forge llama install` reports a path no other process can open.** Forge runs
on the Microsoft Store Python, which silently redirects writes under
`%LOCALAPPDATA%` into the package's private `LocalCache`. The pinned build is
really at
`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\Local\hybrid-forge\llama\b10687\llama-server.exe`.
The command line `forge llama install` printed does not resolve from a shell.
`cmd_doctor` already notes the same redirect for the profile under
`%APPDATA%`. The install message also printed `installed b10666`, which is the
build of the copy on PATH, not the one just installed. When forge starts
`llama-server` itself (phase 3), the path resolves inside forge's own process,
so the supervisor is unaffected. But the printed path and the build number
are both bugs, and running forge from a venv would remove the redirect
entirely.

**The profile's Qwen checkpoint is not on this machine.** The profile names
`qwen3.8` as executor and tester, and no Qwen GGUF exists on any drive. The
only checkpoint present is Nemotron at `C:\AIModels\`. A run today would fail
at its first executor call. `forge models` also reports "no llama.cpp models
in this config" when the models are defined only in the profile.

---

## Phases

Each lands on its own. The first three need no ComfyUI and are worth doing if
the rest is never built.

**0 — Bring-up and measurements. No code. Done 2026-11-01**; see *Phase 0 —
what was measured*.

**1 — Blender through `commands`.** A small example project with a Blender
workspace, `tools/capture.py` and one reference scene, and a three-ticket
backlog run live: SCN-001 above and two like it. *The evidence it produces:
whether an executor lands `bpy` at all, which API mistakes it repeats (Blender's
API changes between versions and models were trained on older ones — this is the
likeliest finding), and whether the viewing seat's rulings on a render are
useful.* The likely loop change from this run is a `/forge-spec-check` rule for
the missing exit-code flag, and an EXECUTOR-CEILINGS-style list of `bpy`
version traps fed through memory.

**2 — GIMP through `commands`.** A Python-Fu tool ticket run live, and the
`format` path confirmed against a binary file. If `format` does not hold up on a
binary, the finding goes in here, and the fix is to `format`, not a new kind.

**3 — Forge runs `llama-server`, and the GPU lease.** First the supervisor in
`processes.py` with the shared rules: profile opt-in, ownership by address,
loopback bind, readiness by model id, adopt-never-steal, `servers.json`, one
restart, stop on every exit. Then `llamaServer` on top of it, with the preset
rewritten before each start and `forge llama start | stop`. Then
`loop.exclusiveGpu`: unload before, `/free` after, released in a `finally`, and
`forge doctor` reporting both servers. *Tests: a profile with no block starts
nothing, so today's runs are unchanged; a port answering with another project's
preset is refused by name; an adopted server is left running at run end; a
server forge started is still forge's after a simulated daemon restart; a
second `--models-max` in `args` fails `validate()`; a leased step unloads
before it starts; a step that raises still releases; an empty
`exclusiveGpu` changes nothing.* The supervisor does not depend on anything
image-related, so it can land before phases 1 and 2 if starting `llama-server`
by hand is costing time now. SETUP §1.3 and `forge-setup`'s workspace-layout
skill both say to run `llama-server` yourself, and both change in this
phase.

*Built 2026-11-01.* Where the build differs from the paragraph above:

- The supervisor is its own module, `forge/servers.py`, not part of
  `processes.py`, which stays about bounded commands. `processes.kill_tree`
  now takes a pid, so a server a dead daemon started can be stopped by the one
  that resumes. `forge/run_servers.py` is what `forge go` starts and stops.
- A server forge owns is restarted when its preset changes. The record carries
  a fingerprint of the argv and the preset, so one started from an older
  config is not trusted.
- `servers.json` records an owner. A server `forge llama start` started is
  used by runs and stopped only by `forge llama stop`.
- `loop.exclusiveGpu` accepts only `capture` until phase 4 adds `image`. Any
  other kind is refused, because a lease nobody applies reads as configured.
- **Building it found a worse version of phase 0's install bug.** Launched
  through the Store Python's virtual `%LOCALAPPDATA%` path, the pinned b10687
  `llama-server` loaded the b10666 DLLs from the winget copy on PATH, and with
  nothing on PATH it did not start at all (`0xC0000135`). A forge that started
  the server itself would have run a mix of two builds and reported the wrong
  one. `llama.server_binary` now returns the resolved path, which fixes the
  launch, the printed path and the reported build together.
- Verified live: `forge llama start` brought up b10687 with a generated
  preset, and `forge doctor` got an answer from both Nemotron and Qwen. Qwen was
  served through its Hugging Face snapshot symlink. `forge llama stop` freed
  the card.

**4 — `ImageProvider` with a ComfyUI adapter, and forge running ComfyUI.**
IMAGE-LOOP phase 2, plus the workflow-and-binding config, API-format refusal,
and render seconds in `usage`. Also the managed process: the `comfyui` profile
block, startup at run start when the backlog has an image ticket, the
`/object_info` model check, adopting an instance that is already running, a
single restart on a crash, the kill-and-restart fallback for the lease, and
`forge comfy start | stop | status`. *Tests: a bound node missing from the
workflow fails `validate()`; a UI-format workflow is refused by shape; a
rendering is recorded before it is judged; a backlog with no image ticket
starts nothing; a workflow naming a checkpoint that is not on disk fails at
startup; an instance forge adopted is left running at run end; a process that
dies twice parks the ticket.* The process tests use a stub server, not
ComfyUI.

**5 — The image kind, first ticket on SDXL pixel art.** IMAGE-LOOP phases 3-4
with the executor writing the prompt block. The first ticket is the one
IMAGE-LOOP's risks section asks for — an icon or tile at fixed size on a fixed
palette — with GIMP's quantise pass as `format`. Almost every criterion is
mechanical, so it tests the loop rather than the judge.

**6 — Review and the Kontext edit loop.** IMAGE-LOOP phases 5-6. The reviewer's
`MODE: edit` and `REGION` become a Kontext `INSTRUCTION`; a backend without an
edit workflow degrades to regenerate. Stall detection is not optional here.

**7 — Pipelines and Flux.2.** The tile-set backlog end to end. Flux.2 only once
phase 0 has measured whether a turbo-LoRA render is fast enough to sit in a loop
at all.

---

## Decisions to confirm

1. **ComfyUI as the image backend.** Recommended: it reaches every model on this
   machine through one adapter, it is local, and Kontext gives the edit loop a
   real `supports_edit`. The alternative — one adapter per model through
   diffusers — puts torch in a dependency list that is currently empty.
2. **The executor writes the prompt, an image model renders it.** Keeps four
   roles and puts the revision on a text model. The alternative is an image
   ticket with no text model in BUILD at all, which leaves nothing to read a
   rejection.
3. **Blender and GIMP enter only through `commands`.** No adapter, no driver,
   nothing in the daemon that knows either exists. This is the position the loop
   already takes on compilers, and it is what lets a project without them pay
   nothing.
4. **Forge runs `llama-server` and ComfyUI — decided 2026-11-01.** Each one
   is opted into by a machine profile block, bound to loopback, adopted rather
   than replaced when it is already running, recorded so a resumed daemon still
   owns it, and stopped only when forge started it. A server given by a
   non-loopback address is external, and forge only probes it. See *Forge runs
   the servers*.
5. **The GPU lease is configured, not inferred.** The loop cannot know which
   command touches the card, so the project says so. Empty is the default.
6. **Workbench capture by default for Blender.** Fits beside a resident llama
   checkpoint and needs no lease. Cycles is a project's opt-in, with
   `capture` in `exclusiveGpu`.

---

## Risks

**Blender's API moves faster than training data.** Executors will write `bpy`
for an older Blender, and 5.2 will refuse some of it. That is a ceiling to
measure in phase 1, not a reason to skip it; the mitigation is the existing one,
learnings recorded by respec so the loop stops rediscovering the same rename.

**The card is shared and contention is silent.** Covered above. Until phase 3
lands, a run that includes any ComfyUI call should be run with llama-server
stopped or on cloud roles.

**ComfyUI's Python must be its own.** Phase 0 found this live: on the shared
Store Python, an unrelated upgrade left ComfyUI unable to start. It now has a
venv. ComfyUI-Manager still installs node dependencies into that venv, so a
Manager update can break ComfyUI between runs. The `/object_info` check catches
a missing model but not a broken import, which is why a failed start has to
report the log's tail rather than just an exit code.

**ComfyUI custom nodes are arbitrary code.** A workflow that needs a custom node
is a workflow that runs that node's Python on this machine. Workflows are project
files and should be reviewed like any other code the project runs; the adapter
should only talk to `127.0.0.1` unless the URL is set otherwise on purpose.

**Every IMAGE-LOOP risk still applies.** The judge is most of the decision, a
vision model is confident and inconsistent, and nothing here has met a model.
Phases 1 and 2 exist first partly because they produce evidence about the
viewing seat on renders the project's own build made, before any ticket depends
on judging pixels a model invented.

**Scope creep.** IMAGE-LOOP's line holds: no new step, no new role, no new
command. This plan adds a few config keys (`imageModels`, `loop.exclusiveGpu`,
and `imageModel`/`editModel` beside `viewModel`), two profile blocks
(`llamaServer`, `comfyui`), one adapter family and one process supervisor. If a
phase needs more than that, that is the evidence the claim was wrong.
