# Blender, GIMP and ComfyUI in the loop — plan

**Status:** built, phases 0-7, and each phase run live on this machine
between 2026-11-01 and 2026-11-02. Blender and GIMP enter through `commands`;
ComfyUI is an image model forge runs; image tickets render, finish, check,
review, edit and park on a stall; one backlog crosses all three tools. Not
built: the criteria gate at ingest, and multi-view sprite sheets. The phase
sections below keep the plan as written, with what each run found after it.

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

**A checkpoint in a Hugging Face cache does not look like a checkpoint.** Both
models are in `C:\AIModels\`. Qwen is in a Hugging Face cache layout:
`snapshots\<rev>\Qwen3.8-27B-UD-Q4_K_M.gguf` is a 0-byte symlink, and the
15.3 GB file it points to is under `blobs\` with no extension. A search for
large `.gguf` files finds neither, and an earlier version of this section said
Qwen was missing because of that search. A preset's `model =` should name the
snapshot path, which llama.cpp follows. Anything forge ever does to find or
size a checkpoint must resolve symlinks first, or it reports a 0-byte model.

`forge models` reports "no llama.cpp models in this config" when the models are
defined only in the profile.

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

*Run 2026-11-01, twice, on `examples/blender-props`.* Qwen3.8 executor and
tester, Nemotron planner and reviewer, Claude Opus as `viewModel`, Blender
5.2.3, forge running `llama-server`.

| | Run 1 | Run 2 |
|---|---|---|
| Outcome | 3 / 3 done in 49.6 min | SCN-001, SCN-002 done; SCN-003 failed sign-off, run stopped at 49 min to free the GPU. SCN-003 re-run alone with the spec corrected: done in 7.7 min, sign-off unanimous on the first pass |
| Attempts | SCN-001 2, SCN-002 1, SCN-003 2 | SCN-001 1, SCN-002 1, SCN-003 2 |
| The view model saw the captures | **no** | yes, 4 images per ticket |
| Review time | 24 s, 845 s, 18 s | 22 s, 25 s |

**The executor lands `bpy` against 5.2.** Every scene was right on inspection
of the captures. The predicted pile of version traps did not show up: one real
API mistake in five landed scenes. Run 1's executor assumed
`bmesh.ops.create_cube(size=1.0)` makes a 2 m cube, which is the convention of
the `bpy.ops.mesh.primitive_cube_add` operator, and scaled each box by half its
extent. The failing dimensions named the cause, and attempt 2 fixed it by
reading `crate.py`. Run 2's executor used `size=2.0` with half-extents, which
is also correct, and landed first time. The other failure was in the tester's
code, not the executor's, and it was **the one mistake that repeated**: SCN-003's
tester wrote `obj.location["x"]` in both runs, which raises
`TypeError: vector indices must be integers, not str`, and both times the next
attempt fixed it. The reference test taught it: `crate_test.py` looped
`for axis, size in zip("xyz", obj.dimensions)`, pairing a string with a vector,
and the tester reused `axis` as the index. The reference now indexes by integer
and says why. A reference file is copied more faithfully than any instruction,
including its accidents. The spec did most of the work: it named the
`bmesh.ops` calls and their arguments, and every number in it was checked
against a reference build before the run.

**Run 1's reviewer never saw a capture, and accepted all three tickets
anyway.** Three causes, all fixed in forge:

- **A view model that cannot see was accepted.** The profile's `claude` block
  had no `tools`, so claude-cli reported it could not see, and the loop sent the
  capture filenames instead, as documented. `roleNeeds` could not catch it,
  because `viewModel` is not a role. `validate()` now refuses a `viewModel` that
  cannot be shown an image.
- **claude-cli loaded the user's MCP servers.** `--tools ""` does not reach MCP,
  so the reviewer tried to open the PNGs through a claude.ai Docs connector,
  wrote that it "could not open the four capture PNGs" and that its "only file
  tools were Claude Docs tools", and spent 845 s on SCN-002's review waiting for
  reads that never returned. Forge now passes `--strict-mcp-config` unless
  `allowAllTools` is set.
- **The capture command guessed what changed from `git status`.** With
  `autoCommit` off, that is every earlier ticket's work, so SCN-002's review
  showed the anvil and SCN-003's showed all three props. The loop now names the
  attempt's files in `FORGE_CAPTURE_FILES`, and the example renders from that.

With all three fixed, run 2's reviewer read the four images and the reference
files and ruled in 22-25 s. Both its verdicts were accepts of work that was
right, so this run cannot say whether the capture *changes* a verdict, only
that it now reaches one.

**Sign-off parked SCN-003 on objections contradicted by its own reference
files.** Nemotron, as planner and reviewer, objected that `common.clear_scene()`
leaves the default cube, camera and light, so the scene could not hold exactly
three meshes. It does not: it calls `read_factory_settings(use_empty=True)`,
`common.py` was a reference file for the ticket, and every passing test relies
on it. The planner also claimed an empty `scenes/__init__.py` makes
`from scenes import …` unresolvable, which every passing test also disproves.
The model's general knowledge of Blender won over the file in front of it.
`SPEC.md` now states both as protected decisions, and with them SCN-003 signed
off unanimously on the first pass and landed in 7.7 minutes. On the loop side, a vote
whose objection a reference file or the green suite already refutes is the same
shape as the sign-off misfires in RATIFY.md, and is worth replaying there
before changing anything.

**Two defects no criterion caught, both in the reference code, not the
model's.** Exporting the landed props to `.glb` showed every material as flat
grey: `common.material()` set only `diffuse_color`, the viewport colour the
Workbench capture shows, while the glTF exporter reads the Principled BSDF. And
the barrel's UV layer held a single point, because the spec said its UVs "may
stay at their defaults". Both looked right in every capture. The helper now
sets both colours, the reference test reads the colour back out of an exported
file, the spec gives the barrel a cylindrical unwrap with criteria for it, and
`tools/export.py` writes each scene as `build/<scene>.glb` and `.blend`, so a
landed ticket leaves an asset rather than only a script. The lesson for the
image loop: a capture shows what the renderer shows, and a renderer reads
different properties from the exporter. Criteria about an asset must check
the asset.

What phase 1 did not test: Cycles captures and the GPU lease, which Workbench
did not need, and whether a seeing reviewer rejects a scene that is wrong. The
next run should carry one ticket whose geometry passes its numeric criteria
while its capture shows the mistake, which is the case the capture exists for.

**2 — GIMP through `commands`.** A Python-Fu tool ticket run live, and the
`format` path confirmed against a binary file. If `format` does not hold up on a
binary, the finding goes in here, and the fix is to `format`, not a new kind.

*Run 2026-11-01 on `examples/gimp-sprites`.* Same seats as phase 1. Three
Python-Fu tool tickets, a key-out, a palette snap and a sprite tool chaining
them, fed by a Blender render of phase 1's anvil.

**3 / 3 done in 50.0 min, one retry cycle.** SPR-001 and SPR-002 landed on the
first attempt. The executor had no trouble with GIMP 3.2's Python API: the spec
named each call, and the four traps that cost the reference implementation
attempts were written into it beforehand. Those four are a palette passed by
name, `Gimp.Palette.new` renaming on collision, palettes persisting in the
user's profile, and transparency living on a spare palette slot. No palette was
left in the profile afterwards.

**The format path holds for a binary file.** GIMP opens the paths a `format`
command appends, so a Python-Fu script over `Gimp.get_images()` is a working
`.png` formatter. Two parts of forge's format step assumed text, and both are
fixed: change detection compared files decoded with replacement, so two PNGs
differing only in invalid-UTF-8 bytes read as unchanged; and an unchanged
binary file was shown to the reviewer decoded.

**The seeing reviewer rejected work that passed every criterion, on what the
capture showed.** This is the case phase 1 could not test. SPR-003's first
attempt passed all four acceptance tests. Its capture showed opaque pink
speckles across the sprite's background, and the reviewer rejected it:
near-magenta pixels survive an exact key, and the palette step snaps them to
PICO-8 pink. It inferred the cause without reading a pixel value, and the
inference was right. The fixture, which I had called an exact `#ff00ff`
background after checking one corner, holds 8,163 pixels of `(254, 0, 254)`
and `(255, 1, 255)`, from Blender's render dithering. The synthetic test
inputs were flat, so no test could see it.

The executor's response was the right one, and it took a respec to get there.
It found that the reviewer's suggested fix contradicted SPR-001's landed spec
and test, which pin the key to an exact match, and it said so rather than
breaking a landed ticket. The resolution kept SPR-001's behaviour as the
default: `key_out` gained an optional threshold, and `sprite` passes 0.1. The
spec now carries that, with the dithering as its reason and a criterion over a
noisy background.

**The sprite it accepted was a flat blob, and a person caught it.** Opened in
GIMP afterwards, the accepted sprite had a clean background and one colour: 985
pixels of PICO-8's `#5f574f` and ten black. The anvil's faces were greys 48 to
90, and the palette's nearest grey to every one of them was the same entry, so
top, front and side merged. Nothing failed. The criteria checked that colours
landed in the palette, and the spec told the reviewer to expect "greys snapped
to the PICO-8 palette", which is what it got. Three changes, verified on the
real fixture:

- `sprite` stretches contrast (`levels_stretch()`, which ignores transparent
  pixels) between key and pixelate, and a criterion now holds three faces of
  greys 60, 75 and 90 apart as black, grey and white. That test fails without
  the stretch.
- The capture's demo palette is a ten-step grey ramp plus four accents, not
  PICO-8.
- The spec's visual bar says what matters: the faces stay different greys and
  each face is flat. "Every pixel is a palette colour" was a bar a blob passes.

The stretch also magnified Blender's ±1 dither noise into grain on every face,
so the fixture is now rendered with `dither_intensity = 0`. The resulting
sprite uses five tones, with distinct top, front and shadow faces and a bright
horn.

This is phase 1's export finding again, from the other side. There a capture
looked right while the asset was wrong; here the asset met every criterion and
looked wrong. Either way the bar was in my head and not in the spec, and the
reviewer can only hold work to the bar it is given.

**Forge lost the evidence of that rejection.** `_capture` named its directory
from `ticket.attempts`, which restarts at 1 on every retry cycle, so attempt
three's accepted capture was written into `attempt-1/capture`, over the image
the rejection was about. `_record` already read `attempt_number` for exactly
this reason, with a comment saying so. `_capture` now takes the number from
the ticket, so no call site can pass the wrong one.

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

*Built 2026-11-01.* `forge/providers/image.py` (the family),
`forge/providers/comfyui.py` (the adapter), `forge/comfy_server.py` (the
managed process, on phase 3's supervisor), `forge comfy start | stop | status |
render`, `usage.images` and `usage.render_seconds`, `image` added to
`loop.exclusiveGpu`, and image models in `forge doctor`. Verified live: forge
started ComfyUI in 24 s, its readiness check passed every node class and model
file of two real workflows, an SDXL pixel-art render took 12.3 s, a Kontext
edit of it 15.8 s, the lease's `/free` brought the card from 18.4 GB to 1.9 GB
with ComfyUI holding 64 MiB, and `forge comfy stop` ended it.

Two findings from the live check:

- **`/free` answers with an empty body.** The stub the adapter was first tested
  against returned `{}`, so every test passed and the live release failed
  parsing it. The stub now answers as the server does. A stub built from
  assumptions tests the assumptions.
- **Kontext reframes.** The edit changed only what it was told to, but its
  input is rescaled to its preferred resolution, so the chest moved and grew
  slightly between the two images. Phase 6's stall detection and any
  before-and-after comparison must not assume two attempts are pixel-aligned.

Not built, because there is nothing to call them yet: the `image` ticket kind
that would start ComfyUI from `forge go`, and the lease around an image call.
Both are phase 5. Until then the gate in `forge go` asks whether any queued
ticket has kind `image`, and none can.

**5 — The image kind, first ticket on SDXL pixel art.** IMAGE-LOOP phases 3-4
with the executor writing the prompt block. The first ticket is the one
IMAGE-LOOP's risks section asks for — an icon or tile at fixed size on a fixed
palette — with GIMP's quantise pass as `format`. Almost every criterion is
mechanical, so it tests the loop rather than the judge.

*Built and run 2026-11-01.* `**Kind:** image`, `loop.imageModel`, a kind gate
that parks a kind the loop cannot run before any model is called, and an image
attempt that replaces only build and apply: the executor writes a `PROMPT:` /
`NEGATIVE:` / `SEED:` block (with the image model's `promptGuide`, and the
last attempt's prompt on a retry), the model renders it under the GPU lease
into the ticket's one `.png` path, and the rendering is recorded in images and
seconds before anything judges it. Tests, format, verify and review are the
code pipeline unchanged; the review is shown the image first, on the seat that
can see. `examples/image-ticket` is the project it ran on.

**IMG-001, a 32x32 treasure chest icon: done in 13.1 min, three attempts.**
Forge started `llama-server` and ComfyUI, leased the card for each render
(unloading Qwen, then releasing), rendered in 5.1-5.6 s, and stopped both at
the end. SDXL rendered, GIMP finished each render as the `.png` formatter
(clear the background by flood fill from the edges, crop, stretch, 32x32,
fourteen-colour palette), the tester's Pillow script checked size,
transparency, coverage and palette, and the reviewer looked.

- **Attempt 1 passed every check and was rejected for the right reason.** The
  reviewer said the chest read as gold, not brown wood. I guessed the
  formatter's contrast stretch had pushed browns toward the palette's golds;
  the raw render shows SDXL drew yellow wood. The reviewer, which only saw the
  sprite, was right about the image, and the executor fixed it in the prompt.
- **Attempt 2 failed a mechanical check, which caught a real defect.** Asked
  to fill "most of the frame", SDXL drew the chest edge to edge, and the flood
  fill from the edges ate into it: 82 opaque pixels against a floor of 250.
  The executor's next prompt asked for a uniform background and an outline and
  fixed the seed; the example's prompt guide now asks for a white margin.
- **Attempt 3 was accepted, with one leniency.** The spec asked for a gold lock
  plate and got a silver padlock; the reviewer called it "a small light lock
  plate" and let it pass. A visual bar the spec states is still a bar the
  reviewer weighs, not one it enforces.
- **Ingest read `**Kind:** image` as `feature`.** Its regex matched only `bug`
  and `feature`, so the first launch began down the code path; it was stopped
  during preflight, before any model call. IMAGE-LOOP assumed ingest stored any
  kind; it did not. It does now, and the kind gate parks what the loop cannot
  run, so a typo is a parked ticket rather than a silent feature.

Not built yet, and not needed by this run: the criteria gate (mechanical /
visual / unverifiable classification at ingest) and stall detection. The
visual bar went in the spec text, as in phases 1 and 2, and three attempts
converged without a stall.

**6 — Review and the Kontext edit loop.** IMAGE-LOOP phases 5-6. The reviewer's
`MODE: edit` and `REGION` become a Kontext `INSTRUCTION`; a backend without an
edit workflow degrades to regenerate. Stall detection is not optional here.

*Built and run 2026-11-02.* `loop.editModel` (an `imageModels` entry with an
`edit` workflow, refused otherwise), `loop.imageStall` (default 3), an
`EDIT:` reply the executor may give instead of a prompt — offered only when an
edit model exists and an earlier raw render does, since a prompt is a promise —
and, on image tickets, a reviewer asked to end a rejection with `ISSUES:`
short names and `MODE: edit | regenerate`. An edit starts from the newest
earlier attempt's raw render, read from the artifacts, never from the finished
file. The stall detector parks a ticket whose last `imageStall` rejections
share an `ISSUES` name, before another render is paid for. The per-criterion
verdict IMAGE-LOOP sketched became issue names: they are what the stall
detector compares, and they need no mapping from prose to criteria.

**The first run of it stopped at sign-off, correctly.** The spec asked for a
red gem visible at 32x32, and the formatter's palette had no red. Planner,
executor and reviewer each named the missing colour and the two remedies, and
the ticket parked before a render was paid for. My spec's defect; the palette
gained two reds and sign-off was unanimous on the first pass.

**Then: 11.3 minutes, four attempts, two renders and two edits.**

| Attempt | What happened |
|---|---|
| 1 | SDXL drew the chest small on a brick floor despite "plain white background"; the coverage check failed |
| 2 | A good render; the gem was lost at 32x32. Rejected: `missing-gem, silver-lock, gold-wood`, `MODE: edit` |
| 3 | `EDIT:` — Kontext kept the composition pixel for pixel, browned the wood, golded the lock frame; the gem was still too small. Rejected: `missing-gem` again |
| 4 | `EDIT:` — "a large, solid, true-red gem ... sized to fill most of the plate so it reads as a distinct red block when scaled down". Accepted |

- **The edit loop did what it is for.** A good composition was kept and fixed
  rather than thrown away, twice, and the edits chained: attempt 4 edited
  attempt 3's render, not attempt 2's.
- **Kontext did not reframe here.** Phase 4's chest moved between edits; these
  did not move at all. Its rescaling depends on the input's aspect and size, so
  the phase 6 rule stands: never assume two attempts are pixel-aligned.
- **The fix that worked is a pixel artist's fix.** A detail that has to read
  at 32x32 has to be drawn far larger than life at 1024. The executor found
  that on its second edit, from the reviewer's words alone; a prompt guide
  could say it up front.
- **The stall detector did not fire, and should not have.** `missing-gem`
  appeared in two consecutive rejections and the third attempt fixed it. The
  reviewer reused the name unprompted by anything but the note, which is the
  behaviour the detector depends on. It has not yet seen a real stall.

**7 — Pipelines and Flux.2.** The tile-set backlog end to end. Flux.2 only once
phase 0 has measured whether a turbo-LoRA render is fast enough to sit in a loop
at all.

*Run 2026-11-02 on `examples/tile-pipeline`.* Flux.2 first.

**Flux.2 earns its place for textures.** On one texture prompt, SDXL with the
pixel-art LoRA rendered in 5.9 s and drew a dark border frame — the opposite
of seamless — and orange blobs where the prompt asked for a few tiny flowers.
Flux.2 with the turbo LoRA rendered in 43.5 s cold and 8.1 s warm and drew an
even field with a few tiny flowers. A tile's pixel-art look comes from GIMP
anyway, so the model that follows the prompt is the one to use. The cost is
the whole card: every render is a lease, and in the run a cold Flux.2 load
after a lease took 25-39 s.

**3/3 done in 88.8 min across all three tools.**

| Ticket | Tool | Attempts | What it took |
|---|---|---:|---|
| ART-001 seamless tile op | GIMP | 4 + cycle | GIMP's `Palette.add_entry`: the spec named the call but not its first argument; the executor guessed an int, then a `to_string()` that does not exist |
| ART-002 grass tile | Flux.2 + GIMP formatter | 2 | the first render was darker, and the greens criterion had left out the darkest green |
| ART-003 meadow | Blender | 4 + 1 | a sign-off revision rewrote two measured facts; the executor caught one, the tests caught the other, and the retry cycle's respec learned "to obtain a 4 m square the size must be 2.0" |

The seamless tile tiles with no visible grid, and the meadow renders it 4x4
with hard pixel edges. Every handoff worked: ART-001's operation became
ART-002's formatter the moment it landed, and ART-003 loaded ART-002's tile.

**A sign-off revision corrupted a verified spec.** I had measured
`create_grid(size=2.0)` at -2 to 2 m and written UVs as `(x + 2, y + 2)`.
The planner's revision during sign-off changed them to `size=4.0` and
`(x, y)` — an 8 m plane with UVs from -2 — and nothing stopped it, because
neither was marked as a decision. The executor flagged the UV contradiction as
`IMPOSSIBLE` and trusted the wrong size; the tests caught it; the run
recovered only because a retry cycle was left. The example now marks both as
protected decisions. The rule it argues for is broader than Blender: **any
number a spec author measured belongs under `Decision:`**, because a planner
revising from general knowledge will "correct" it.

**Run start did not clear the card, and it cost 25 minutes.** The run adopted
a ComfyUI that still held 28 GB of Flux.2 from my comparison renders. Qwen
loaded beside it, the Windows driver put what did not fit in system RAM, and
sign-off took 1,184 s instead of about 200 with nothing reporting why — the
silent slowdown this plan opened with. Freeing ComfyUI did not help, because
Qwen's spilled memory stayed where it was; unloading Qwen so it reloaded into
free VRAM did, at the cost of one attempt. `RunServers.start` now asks every
image backend to release the card before any text model loads, whenever the
backlog has image work.

---

## Sprite sheets — built 2026-11-02

Eight-direction sheets for a key and its padlock, each in red, blue and green:
`examples/sprite-sheets`. Rendered from 3D, the way games build sprites from
models — one prop, one fixed isometric camera, every angle and colour finished
identically, so frames match. The loop wrote the art and the packer (SHT-001
key, SHT-002 lock, SHT-003 packer); the project supplies the turntable, the
batch GIMP sprite tool and `tools/make_sheets.py`, which builds both sheets in
about 25 s: 512 x 192 PNGs, rows by colour, columns 0-315 degrees, 64x64
frames, and a JSON frame map beside each.

**Flat views, added after.** Each sheet now also has four face-on columns,
`flat_0` to `flat_270`, for side-on and top-down games and inventory icons,
so the sheets are 768 x 192. "Face-on" means looking along the prop's
thinnest axis, which `turntable.flat_elevation` reads from the mesh: the lock
stands up and is seen at eye level (front, side, back, side); the key lies
flat and is seen from straight above, since at eye level it is a line. No
scene changed. The first build showed blue's face-on frames on the light-blue
palette entry while red and green kept their shade: a face square to the
camera catches Workbench's whole specular highlight. With specular off, a
face-on face renders the same shade as the isometric lit face, (58, 93, 160)
against (56, 90, 156) for blue.

Choices that keep frames consistent, each from a defect in the first
reference build: one scale per prop from its *projected* size (scaling by
height clipped the lock's shackle), no cavity shading (its highlights became
grain at 64x64), and no contrast stretch (computed per image, it would put
the same red on different palette entries frame to frame).

The key and the packer each landed first time. **The lock took four runs, and
none of it was Blender.**

1. **A sign-off revision moved measured numbers again** — the keyhole from y
   -0.075 to -0.06, then -0.065 — and every role then objected, correctly, to
   the revision's numbers. Parked twice. This was the third ticket in two runs
   broken that way, so it became a loop rule: **a revision may not change a
   number the human's spec states.** `respec._moved_numbers` compares the
   multiset of numbers in a revised spec with the human's `original_spec`;
   a revision that drops or moves one is refused in sign-off and in respec,
   and the ticket keeps its text. Reformatting (`0.10` as `0.1`) and adding
   numbers are allowed. If a human number really is wrong, the objection
   stands and the ticket parks for a person.
2. **With the guard on, it refused the planner's next attempt** to move the
   body's -0.07 and 0.07 — and the planner and reviewer, both Nemotron, then
   objected that the body is 0.14 deep so the lock cannot be 0.155. The
   keyhole is what makes it 0.155. A protected decision saying exactly that,
   body 0.14 plus keyhole to -0.085, did not change their vote: the same
   objection came back word for word.
3. **The tester's objection in that same vote was right.** Floating point puts
   the posts' bottom faces at 0.23999999, so "centre z of 0.24 or more" would
   fail correct geometry. The criteria now carry margins.
4. **With sign-off off for that one rerun** (`loop.ratifyPasses: 0`, the test
   project only), the lock landed on its first attempt and the reviewer
   accepted it, matching the spec coordinate for coordinate.

What the lock says about sign-off: it caught a real defect (the float
margins) and invented a false one that no amount of spec text dislodged, from
the same model, in the same vote. The guard stops the false objection from
rewriting the spec; it cannot stop it from blocking the vote. A planner that
cannot add three intervals is a weak seat for geometry — the Claude seat
already reviews captures, and seating it as the sign-off planner on tickets
like this is the obvious next experiment, rather than another rule.

**The experiment, run 2026-11-02.** Same ticket, same spec, sign-off back on
at the default passes, the lock deleted, and one line of project config:
`"roles": {"planner": "claude"}` — the per-project override, so the machine
profile still seats Nemotron everywhere else. Result: **ratified unanimous on
the first pass, landed on the first attempt, review passed**, and the sheet
built from it matches the earlier lock frame for frame. Every role voted,
including Nemotron as reviewer, which this time raised no depth objection.
The two Claude calls (planner vote, capture review) cost $0.29; Qwen and
Nemotron are local. Evidence: `sheet_forge_evidence/run-claude-signoff.db`.

One run cannot say whether Claude's vote moved Nemotron's or the earlier
objection was luck of sampling; it does say a Claude planner adds no false
objection of its own on geometry, where Nemotron raised the same false one in
every guarded vote before. For geometry tickets the recommended seat is Claude as planner, set per
project; the guard on human numbers stays either way.

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
