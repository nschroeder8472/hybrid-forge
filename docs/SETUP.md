# Hybrid Forge — setup

End-to-end setup for the plan-and-execute pipeline: Claude plans and reviews,
a locally hosted model on llama.cpp executes, MemPalace carries project decisions
across sessions.

If this is your first time, [QUICKSTART.md](QUICKSTART.md) walks the same
ground in order — one recommended path, a narrated first `forge init`, and
pointers back into this document at each decision point. This one is the
reference; that one is the tour.

There are three distinct installs, and conflating them is the most common way
this goes wrong:

| | What it is | Where it runs | How often |
|---|---|---|---|
| **Host services** | Model weights, inference server, MemPalace store | 5090 desktop only | Once |
| **Daemon + plugins** | The `forge` CLI, plus the setup and spec plugins | Every machine you code from — it must sit with the repo | Once per machine |
| **Project config** | `.hybridforge/` — models, roles, commands, tickets | Per repository | Once per repo |

The plugins are a few kilobytes of text. They do not contain the model, and they
do not run the loop. They get you configured and get a spec written; the daemon
does the rest from a terminal.

---

## Part 0 — Where each piece runs

Decide this before installing anything; it governs every step below.

**The daemon must sit with the project.** It is not a compute service. It writes
the executor's output to disk, runs your lint/type-check/test commands, and
shells out to `git` to build the diff the reviewer reads — all against a local
working tree:

```
loop.py   apply_edits(config.root, ...)      writes the executor's files
loop.py   subprocess(cwd=config.root)        runs lint / typecheck / test
loop.py   git diff / git commit in root      builds the review diff
```

Put the daemon on a machine that does not hold the repo and it has nothing to
write to, nothing to build, and no diff to review.

So split by weight, not by role:

| Machine | Runs | Why |
|---|---|---|
| **GPU host** (the 5090) | `llama-server` + MemPalace, in Docker | Needs the GPU and holds the palace |
| **Workstation** (your Mac) | repo, daemon, Claude Code, toolchain | Needs the files |

Only model calls and memory reads cross the network. Files, git, and builds stay
local and fast.

### A worked example: Mac + 5090

Nothing new on the GPU host — the existing `docker-compose.yml` is already
exactly right. On the Mac, everything is local except two URLs:

```json
{
  "models": {
    "local":  { "kind": "llamacpp", "baseUrl": "http://forge-host:8080/v1",
                "model": "qwen3.8", "contextWindow": 65536 },
    "claude": { "kind": "claude-cli", "model": "opus" }
  },
  "roles": { "planner": "claude", "executor": "local",
             "tester": "local",   "reviewer": "claude" },
  "commands": {
    "test": "xcodebuild test -scheme MyApp -destination 'platform=iOS Simulator,name=iPhone 16'"
  },
  "memory": { "url": "http://forge-host:8765/mcp" }
}
```

`claude-cli` shells out to the `claude` you are already signed into on that Mac,
so planning and review run on your existing subscription with no API key.

**It runs without tools by default.** `claude -p` is an agent, not a completion
endpoint: left with its tools it runs a multi-turn session, reads files, and
bills for every turn. One measured reviewer call spent 208k cache-read tokens
and $0.34 judging a diff that was already in its prompt; disabling tools cut a
comparable call from $0.106 to $0.046.

It also matters for what the adapter guarantees. The loop decides what each
role may see and what it may write; a role holding tools reads and writes
whatever it likes, so the same `roles` block means different things depending
on which adapter is behind it. Without tools this adapter behaves the way the
loop already assumes — one call, one completion.

Set `"tools"` to get the agent back, either wholesale or by name:

```json
"claude": { "kind": "claude-cli", "model": "opus", "tools": "default" }
"claude": { "kind": "claude-cli", "model": "opus", "tools": "Read,Grep" }
```

A role that benefits from exploring the repo — a planner writing tickets
against a codebase it has not seen — is worth the tokens. A reviewer handed the
diff usually is not.

**Your `CLAUDE.md` reaches these roles.** The CLI loads `~/.claude/CLAUDE.md`
and any project `CLAUDE.md` under the run root, and both land in front of the
planner and reviewer. Project instructions are usually welcome there; personal
style preferences are not, and they will show up in verdict text.

The memory entry uses `url` here because this is the split case: the palace is
on the GPU host, so it runs `mempalace serve` and the Mac connects over HTTP.
That listener wants a bearer token — see §1.4. Running the palace on the Mac
instead replaces the whole entry with `"command": ["mempalace-mcp"]`: no
listener, no port, no token.

**A sleeping laptop pauses the run.** For an overnight run use
`caffeinate -i forge go --no-ui`, or run the daemon on the GPU host against a
checkout that lives *there* — a second working copy, not your Xcode tree.

### Should the daemon itself be containerized?

Usually not. It is a single dependency-free Python process; `nohup forge go`
already covers unattended operation, and containerizing it drags in problems it
does not otherwise have — mounting Claude credentials into a container, and
SQLite WAL across a Windows→WSL2 bind mount.

**Containerize the verify step instead.** That is where the real exposure is:
`commands` currently run model-authored code directly on your host. Because they
are ordinary shell strings, isolating them needs no support from this project:

```json
"commands": {
  "lint": "",
  "test": "docker run --rm --network none -v \"/abs/path/repo\":/w -w /w python:3.12-slim python -m pytest -q"
}
```

`--network none` is worth keeping: the point is isolating code a model just
wrote, and most test suites do not need egress. Drop it only if yours does.

Three things that will bite you, all verified the hard way:

- **Use absolute paths.** `subprocess(shell=True)` uses `cmd.exe` on Windows, so
  `$PWD` passes through literally and Docker rejects it as an invalid volume
  name. `%CD%` on Windows, or just hardcode the path.
- **Testing the command in Git Bash first will fail confusingly.** MSYS rewrites
  container-side paths, so `-w /w` becomes `-w W:/` and Docker reports an
  invalid working directory. Prefix with `MSYS_NO_PATHCONV=1` when trying it by
  hand. The daemon itself is unaffected — it runs commands through `cmd.exe`,
  which does no such rewriting.
- **Each step is a container start.** Three verify commands means three
  `docker run`s. Fine for a test suite; for a fast lint, keep a long-lived
  container and use `docker exec`.

**This does not work for Xcode.** `xcodebuild` and the simulators are macOS-only
and cannot run in a Linux container — Apple toolchains verify natively or not at
all.

---

## Part 1 — Host setup (5090 desktop)

### 1.1 Prerequisites

`llama-server` is the inference server, and it is the only local backend forge
speaks to. Install the daemon first (Part 2) and let it fetch one:

```bash
forge llama install
```

That reads the GPU's compute capability, picks the matching build, verifies the
download against the SHA-256 GitHub published for it, and unpacks it — including
the separate `cudart-*` archive a Windows CUDA build needs and fails without.
Builds are pinned rather than tracked, because llama.cpp publishes several a day
and a throughput measured on one does not carry to another. See
[LLAMA-PACKAGING.md](LLAMA-PACKAGING.md).

To do it by hand instead, take a release binary from
[the releases page](https://github.com/ggml-org/llama.cpp/releases) and put
`llama-server` on PATH; forge uses that when it has fetched nothing.

**Take CUDA over Vulkan if you have the choice.** Measured on a 5090 with a 30B
A3B MoE at Q4_K_M: 16 tok/s on the Vulkan build, 353 tok/s on CUDA. That is not
a tuning difference, it is the difference between a run that finishes overnight
and one that does not. Blackwell (sm_120) needs CUDA 12.8 or newer.

You also need a network path from your workstation to this host. Anything that
gives the two machines a stable address for each other works — a LAN, a VPN, an
overlay network, an SSH tunnel. Pick one now and note the address, because
§1.3 binds the services to it. What matters is not which you choose but that
the resulting address is reachable by your workstation and *not* by anything
else: neither service below has authentication.

Confirm the GPU is visible:

```bash
nvidia-smi
llama-server --version
```

### 1.2 Get the executor model

`llama-server` serves GGUF files directly — there is no separate pull step and
no model store. Download the quantization you want and note the path:

```bash
# e.g. from a HuggingFace GGUF repo
curl -fsSLO https://huggingface.co/<repo>/resolve/main/Qwen3.8-27B-UD-Q4_K_M.gguf
```

At Q4 a 27-30B lands comfortably inside 32GB with room for a working context.
If you want more headroom for long specs, or more quality, benchmark Q4 against
Q6 on your own code before committing — published benchmark deltas will not tell
you how it does on your Rust or Swift.

Note the KV cache is not free and scales with `ctx-size`. Measured at
`--ctx-size 131072`: a 15.3 GiB Qwen at Q4 occupied 24.6 GiB resident, so 9.3 GiB
of that was cache. Setting a window you will not use costs VRAM you could have
spent on weights.

### 1.3 Serve the models in router mode

**Router mode is what lets forge swap checkpoints.** `--models-preset` reads an
INI where each section is a model id; the router spawns one child server per
model on an ephemeral port and proxies `/v1/chat/completions` by that id. The
loop alternates roles, so this is how a planner on one checkpoint and an
executor on another share one GPU and one endpoint.

Forge writes that preset for you — see [§3.1](#31-declaring-models-and-roles)
and `forge models`. It looks like this:

```ini
[qwen3.8]
model = /models/Qwen3.8-27B-UD-Q4_K_M.gguf
jinja = true
ctx-size = 65536
reasoning-budget = 2048
mmproj-auto = false
```

**`llama-server` ships with no authentication.** Binding it to `0.0.0.0`
publishes an endpoint that will happily execute inference for anything that can
reach the port — every network this host is attached to, including whatever your
router is doing. Bind it to a single address instead, and make it the narrowest
one that your workstation can still reach.

List the candidates and pick one:

```bash
ip -4 -o addr show scope global      # interface name and address, one per line
```

```bash
BIND_ADDR=<the address you picked>
llama-server \
  --host "${BIND_ADDR}" --port 8080 \
  --models-preset /path/to/llamacpp.ini \
  --models-max 1
```

**`--models-max 1` unless every model in the preset fits in VRAM together.**
The default is 4, which is right on a box with the room and fatal on one
without: the router keeps the previous role's checkpoint resident and the next
role's child server exits trying to allocate. Forge's `exclusive` setting does
the same thing per model; either is enough, both is fine.

Run it under whatever keeps services up on this host — a systemd unit, a
scheduled task, `tmux`. It owns the GPU and should outlive any one forge run.

Verify from the host, then from your workstation:

```bash
curl http://${BIND_ADDR}:8080/v1/models       # on host
curl http://<host-address>:8080/v1/models     # on the workstation
```

Both checks matter, and the second is the one that counts — an endpoint
answering on the machine it runs on proves nothing about reachability. If it
fails, the problem is the bind address, a firewall rule, or your VPN's access
controls. Fix it here before going further; everything downstream assumes this
works.

The reply lists every model id in the preset with its status. Those ids are
exactly what `model` in `config.json` must name:

```json
{"data": [{"id": "qwen3.8", "status": {"value": "unloaded"}}]}
```

If your network gives the host a stable name, prefer the name over the literal
address in client config — a machine that changes address then costs you one
DNS record instead of an edit in every repo's `config.json`.

### 1.4 MemPalace

MemPalace speaks MCP two ways, and which one you want follows from where the
palace sits relative to the daemon. Verified against MemPalace 3.6.0.

| | transport | when |
|---|---|---|
| `mempalace-mcp` | stdio (what the image's `mcp` CMD runs) | palace and daemon on one machine |
| `mempalace serve` | Streamable HTTP, bearer token, optional TLS | palace on another machine |

Its published image is `ghcr.io/mempalace/mempalace:latest`, so there is
nothing to build.

#### Same machine as the daemon — the simple case

A stdio server is spoken to through its stdin, so if the palace and the repo
share a machine there is no server to stand up and no endpoint to secure. Point
`memory.command` at it and the daemon runs it as a child process:

```json
"memory": {
  "command": ["mempalace-mcp", "--palace", "/path/to/palace"],
  "room": "your-project"
}
```

Or against the published image, with the palace in a named volume:

```json
"memory": {
  "command": ["docker", "run", "--rm", "-i",
              "-v", "mempalace-data:/data",
              "ghcr.io/mempalace/mempalace:latest"],
  "room": "your-project"
}
```

`-i` is load-bearing — JSON-RPC needs stdin held open, and without it the
server exits before the handshake completes.

`forge doctor` confirms it: it starts the process, reports the tools it found,
or fails with the server's own stderr.

#### Different machines — `mempalace serve`

MemPalace serves MCP over Streamable HTTP itself. No proxy:

```bash
mempalace serve --host <bind-addr> --port 8765
```

The endpoint is `/mcp`, which is the transport the daemon speaks:

```json
"memory": { "url": "http://forge-host:8765/mcp" }
```

Unlike `llama-server`, this listener authenticates. A non-loopback bind auto-generates
a bearer token and stores it `0600` under `~/.mempalace/server/`; `--token`
sets one explicitly. Give it to the daemon by naming the environment variable
that holds it — never by pasting it into `config.json`, which this project
tells you to commit:

```json
"memory": {
  "url": "http://forge-host:8765/mcp",
  "tokenEnv": "MEMPALACE_TOKEN"
}
```

`memory.headers` takes arbitrary headers if you need something else. Two flags
worth knowing:

- `--read-only` hides and refuses every mutating tool. If the daemon is only
  retrieving — the default, since write-back is opt-in — this is the right way
  to serve it, and it removes `mempalace_delete_drawer` from the surface
  entirely rather than trusting tool selection to avoid it.
- `--allow-insecure` permits a non-loopback bind with no token. Only behind a
  proxy that terminates auth. Traffic is plaintext without `--tls-cert` /
  `--tls-key`.

Same bind rule as `llama-server`: the narrowest interface your daemon still
reaches, never `0.0.0.0` out of convenience.

The palace database stays on one machine, permanently. One authoritative copy.
Do not sync it, do not check it into a repo, do not keep a second copy on the
client.

### 1.5 Verify the host as a whole

```bash
./scripts/setup-host.sh
```

The script is idempotent and checks the pieces above rather than reinstalling
them. It prints the two URLs you will need for client config.

---

## Part 1b — Containerized host (alternative to 1.1–1.5)

If you would rather treat the compute layer as disposable, `docker-compose.yml`
runs both services with host networking and named volumes. GPU compute overhead
is negligible — with the NVIDIA Container Toolkit, CUDA kernels execute natively
and there is no virtualization layer between the runtime and the hardware.

### Prerequisites

```bash
# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
# (follow the current NVIDIA install docs for the repo line for your distro)
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify the GPU is visible inside a container
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

### Bring it up

```bash
cp .env.example .env
ip -4 -o addr show scope global   # pick one; put it in BIND_ADDR
# MODELS_DIR: where your .gguf files are
# PRESET_DIR: the .hybridforge/models/ of the project, holding llamacpp.ini
docker compose up -d
curl "http://${BIND_ADDR}:8080/v1/models"
```

There is no pull step — `llama-server` serves `.gguf` files off the mounted
directory. Put the files in `MODELS_DIR` yourself and let `forge models` write
the preset that names them.

**The paths in the preset are container paths.** `MODELS_DIR` mounts at
`/models`, so `modelPath` in `config.json` should read `/models/<file>.gguf`
rather than the host path. A section pointing at a path that does not exist
inside the container fails at load with a message about the file.

### Teardown semantics

```bash
docker compose down          # destroys containers, keeps volumes — what you want
docker compose down -v       # ALSO destroys the volumes: the palace is gone
```

The compute layer is disposable; the state is not. `-v` is the footgun. Weights
are a bind mount rather than a volume here, so they survive either way — the
thing `-v` takes is the palace.

### Two things that behave differently under containers

**Host networking is doing real work here.** A container on the default bridge
network has its own network namespace and cannot see the host's interfaces, so
`--host` pointed at a host address would fail to bind — this bites hardest with
VPN and overlay interfaces, which exist only on the host. `network_mode: host`
makes the container's bind address the host's bind address, which is why the
security posture from 1.3 carries over unchanged. Note that `ports:` is ignored
in this mode — `BIND_ADDR` is what controls exposure, so a wrong value there is
not contained by Docker.

**Restarts evict VRAM, and a changed preset needs one.** The router reads the
preset at startup, so `forge models` writing a new one does nothing until the
container restarts. Swaps *within* a run are cheap — measured at 6-10s for a
15-23 GiB checkpoint with a warm page cache — but a restart re-reads from disk
cold. Leaving the container up and restarting only on preset changes gets you
the disposable runtime without paying that on every session.

### Windows caveat

If the 5090 sits in a Windows workstation, run `llama-server` natively and
containerize only MemPalace. Docker Desktop routes through WSL2, where GPU
passthrough works but adds a filesystem translation layer that is genuinely slow
for large sequential reads — exactly the access pattern of loading 20GB of
weights — and is more fragile across driver updates.

---


## Part 2 — Client setup (each machine you code from)

Do this on the 5090 itself as well — the plugins are per-machine even when the
services are local, because the 5090 running Claude Code is just another client
that happens to have a very short network path.

### 2.1 Install the plugins

Two, and they are independent. `forge-setup` gets a machine and a repository
configured; `forge-spec` is where features get designed into documents the loop
executes. Neither runs the loop — that stays in a terminal, where it outlives
the session.

```bash
claude plugin marketplace add <your-github-user>/hybrid-forge
claude plugin install forge-setup@nms-forge
claude plugin install forge-spec@nms-forge
```

For local development, skip the marketplace and load a directory directly:

```bash
claude --plugin-dir /path/to/hybrid-forge/plugins/forge-spec
```

### 2.2 Configure endpoints

`forge-spec` declares one setting in `userConfig`, so Claude Code prompts for it
at enable time rather than making you hand-edit settings:

- `memPalaceUrl` — your MemPalace endpoint from step 1.4

Model endpoints are not plugin settings. They belong to the daemon, and
`/forge-setup` writes them to the machine profile in step 2.3 below.

### 2.3 Install the daemon

```bash
pip install -e /path/to/hybrid-forge      # provides the `forge` command
```

The daemon and CLI have **no runtime dependencies** — stdlib Python 3.10+ only.
That is deliberate: a failed `pip install` is a bad way to discover that an
overnight run never started.

Working *on* hybrid-forge needs two more:

```bash
pip install -e "/path/to/hybrid-forge[dev]"   # adds flake8 and pytest
```

Both are required rather than suggested. `tests/test_lint.py` runs `flake8` over
the tree and fails when the tool is absent instead of skipping — an enforcement
that disappears on the machine that lacks it enforces nothing — and the fixture
under `examples/sample-project` now grades generated code with the same linter,
which is what makes a run there able to fail the way real runs do.

If you want the loop to drive Claude for planning and review, Claude Code must
be installed **on whatever machine runs the daemon** — the `claude-cli` adapter
shells out to it. That machine is usually your workstation, not the GPU host.

### 2.4 Verify

```bash
forge --help
```

Then, inside Claude Code with `forge-spec` enabled:

```bash
claude
> /mcp
```

You should see `mempalace` connected. That is the only server either plugin
ships — the loop itself speaks to models directly and does not use MCP at all.

If it fails to connect, `claude --debug` shows connection attempts and tool
discovery, which is considerably more informative than the summary view.

---

## Part 3 — Per-project setup

In each repository you want to use the pipeline in:

```bash
cd ~/code/image-marquee
forge init          # or, inside Claude Code: /forge-setup project
```

`forge init` asks five short questions — executor endpoint, who plans and
reviews, memory URL, this repo's room and verify commands, then a review of what
it will write. **Every endpoint is probed as you answer it**, with one real
completion rather than a socket check: an endpoint that is up but 401s, or that
serves a model name with a typo in it, both look fine to anything cheaper and
fail on the first ticket.

For the verify commands it asks a model rather than guessing from file names.
With your permission it collects the repo's CI workflow, Makefile or Justfile,
build manifest, and contributing guide, and asks the **planner** model what this
project actually runs. It reports which files it read and where each command
came from.

The difference is not cosmetic. A `Cargo.toml` does not mean `cargo test` — the
project may run `cargo nextest run`, need `--workspace`, gate tests behind a
feature, or drive everything through `just`. CI already states the real answer.

What it will not do is invent one. A repo with no CI, no Makefile, and nothing
in its docs gets empty fields, because an empty command is skipped by the loop
and a plausible wrong one parks the backlog. Detection can be declined, and
every failure — no planner yet, endpoint down, unreadable reply — leaves the
fields blank for you to fill in rather than substituting a guess.

Commands are also sanity-checked before being offered: unexpanded CI
interpolation (`${{ matrix.flags }}`), a bare `cd`, an invented `<placeholder>`,
or a two-line procedure are all discarded rather than pre-filled.

Nothing is written until the last question. Ctrl-C anywhere leaves the repo
exactly as it was.

```bash
forge init --defaults   # skip the questions, write a config to edit by hand
```

With no terminal attached — piped, redirected, run from a script or from Claude
Code — it takes every default, prints what it chose, and exits. It never blocks
waiting on input nobody is there to give.

This creates:

```
.hybridforge/
├── config.json        # models, roles, commands, never-delegate globs
├── run.db             # run state — gitignored, not a reviewable artifact
└── tickets/           # one markdown file per unit of work
```

Commit everything except `run.db` (the generated `.gitignore` handles it). The
tickets in particular benefit from being reviewable — they are what a human
reads to decide whether the plan is right *before* the loop spends hours acting
on it.

### 3.0 The machine profile — why the second repo is faster

Your endpoints do not change per repo, so `forge init` saves them once:

| Platform | Path |
|---|---|
| Linux / BSD | `$XDG_CONFIG_HOME/hybrid-forge/profile.json`, else `~/.config/hybrid-forge/profile.json` |
| macOS | `~/.config/hybrid-forge/profile.json` |
| Windows | `%APPDATA%\hybrid-forge\profile.json` |
| Any | `$FORGE_PROFILE`, which overrides all of the above |

It holds `models`, `roles`, `memory`, and the UI port. Every one of those
questions arrives pre-filled on the next repo, so setup #2 is Enter-through
until it asks about that repo specifically.

It deliberately does **not** hold `commands`, `room`, or `neverDelegate`. Those
differ per repo, and a `cargo test` carried into a Python project does not fail
loudly — it fails `maxAttempts` times per ticket and parks the entire backlog,
which looks like a model problem and is not.

**No credentials are stored there, ever.** Providers resolve keys through
`apiKeyEnv` — the *name* of an environment variable — so the name is what gets
saved. An inline `apiKey` typed into a config by hand is stripped on the way in
rather than quietly copied into a second file you did not know existed.

The file is plain JSON with a comment field at the top. Edit it, or delete it to
start fresh; a corrupt one is treated as absent, because re-asking four questions
beats refusing to initialize a repo.

### 3.1 Declaring models and roles

`forge init` writes this block for you; this section is what the questions were
actually asking, and what to edit if you skipped them with `--defaults`.

The config's two important blocks are `models` (what you have) and `roles` (who
does what). Any declared model can play any role:

```json
{
  "room": "image-marquee",
  "models": {
    "local":  { "kind": "llamacpp", "baseUrl": "http://forge-host:8080/v1",
                "model": "qwen3.8", "contextWindow": 65536 },
    "claude": { "kind": "claude-cli", "model": "opus" }
  },
  "roles": {
    "planner":  "claude",
    "executor": "local",
    "tester":   "local",
    "reviewer": "claude"
  },
  "commands": {
    "lint":      "cargo clippy -- -D warnings",
    "typecheck": "",
    "test":      "cargo test"
  },
  "neverDelegate": ["src/auth/**", "src/wasm_bridge.rs"],
  "loop": { "maxAttempts": 5, "autoCommit": false, "stopOnBlocked": false,
            "retryCycles": -1, "respecOnRetry": true, "respecCriteria": false },
  "ui":   { "host": "127.0.0.1", "port": 8799 }
}
```

Every key this file understands is documented in
[CONFIG.md](CONFIG.md), and a fully populated example — two local endpoints, a
Claude reviewer with a spend cap, memory, and every `loop` knob spelled out —
lives at [templates/config.sample.json](../templates/config.sample.json).

Two choices worth making deliberately rather than by default:

**Do not put the executor and reviewer on the same model.** A model reviewing
its own diff against a spec it just implemented will accept it. The review step
is what keeps a cheap executor honest, and it only works if something else is
doing the reviewing.

**Get `commands` right before the first run.** The loop treats a failing check
as a reason to re-delegate the ticket, so a wrong test command means every
ticket fails `maxAttempts` times and gets parked. An empty string skips that
check entirely, which is better than a command that does not work.

`room` scopes every memory read and write to this project. Without it, queries
pull decisions from unrelated repos and present them as authoritative, which is
worse than having no memory at all.

### Context window and output reserve

`contextWindow` and `maxOutputTokens` are per model, and config always wins.
Two things about them are worth getting right before the first run, because
neither fails a health probe.

**Set `contextWindow` to what your server is serving, not what the model can
do.** These are different numbers and they routinely disagree: a checkpoint
trained for 262,144 serves whatever `--ctx-size` its child server was spawned
with. Forge reads that argv back out of the router's catalogue rather than
reading the trained maximum out of the GGUF, for exactly this reason. Believing
the larger figure defeats the budget gate — it approves a 90k prompt for a 32k
server, which truncates from the *front*, taking the system prompt and the spec
with it. What comes back then reads as a weak model rather than a truncated
request.

### Sampling

Five knobs are settable per model block, and an unset one is left off the
request entirely — a model's own shipped recipe survives rather than being
overwritten with a default nobody chose:

```json
"local-code": {
  "kind": "openai", "baseUrl": "...", "model": "forge-code",
  "temperature": 0.7, "topP": 0.8,
  "topK": 20, "minP": 0, "presencePenalty": 1.5
}
```

`temperature` here overrides the per-role value the loop asks for, for every
role this model plays. That is coarser than the loop's own defaults, which
differ by role — worth knowing before setting it on a model that plays three.

**The loop's temperatures are low on purpose, and that has a cost.** Retries
assume the next attempt samples differently; at 0.0–0.2 with an unchanged
prompt, it does not. One run had the tester reproduce the same unused variable
across 36 attempts, and the automatic retry cycle stops precisely when it
detects that nothing is varying. Models that ship a recommended sampling
recipe — most current open-weights families do — are usually better run near
it. `qwen3-coder` ships `temperature 0.7, top_p 0.8`; `gpt-oss` ships
`temperature 1.0`. The model card is where these are published; `forge doctor`
does not invent one for you.

Keep the reviewer tighter than the rest. Its verdict has to parse, and a
verdict that does not is treated as a rejection.

A bare `"temperature": 0.6` overrides *every* call, including the ones the loop
asks determinism for — sign-off votes and sampling comparisons stop being
reproducible. `{"default": 0.6, "deterministic": 0.0}` follows the recipe and
lets a requested 0.0 through, which is what you almost always want.

**State the window rather than leaving it to be read.** Forge can read
`--ctx-size` back from the router, but only for a model the router already knows
about, and not at all while the router is down — in which case it falls back to
8192 and the budget gate reports 1-3k-token tickets as too large. Setting
`contextWindow` explicitly per model removes that failure mode entirely.

Raise `ctx-size` in the preset if you want the rest of the window. A larger
window allocates a larger KV cache — measured at `--ctx-size 131072`, a 15.3 GiB
Qwen at Q4 occupied 24.6 GiB resident — so check it still fits alongside the
weights. `cache-type-k`/`cache-type-v` at `q8_0` roughly halve the cache if it
does not.

**`maxOutputTokens` comes straight off the prompt budget.** `input_budget =
contextWindow − maxOutputTokens − margin`, so reserving the whole window for
output leaves nothing to put a prompt in and every ticket overflows before it
starts. A config setting both to 32768 produced a prompt budget of −512.

Reasoning models need more of it than you would guess: gpt-oss:20b spends about
50 tokens thinking before it emits `OK`, and a whole-file response needs the
thinking plus the file. 8192 is a reasonable starting point for a 32k window.

`forge doctor` prints the resulting `prompt_budget` next to both, and warns when
it drops below a third of the window.

### Where each setting has to live

Two files, and the split is not arbitrary: a setting that decides how the child
server is *spawned* cannot be sent per request, and a setting that varies per
role cannot live in the preset.

| Setting | preset (`.ini`) | `config.json` | Why |
|---|---|---|---|
| `ctx-size` / `contextWindow` | **allocates the KV cache** | **what the gate plans against** | Both. They must agree — see below. |
| `reasoning-budget` | **wins** | written from `reasoningBudget` | Decided when the server starts. |
| `n-gpu-layers`, `flash-attn`, `cache-type-k/v` | **only place they work** | written from config | Spawn-time. |
| `temperature` | ignored by forge | **wins** | Forge sends one per role on every call. |
| `top_p`, `top_k`, `min_p` | ignored by forge | **wins** | Sent per request, and unlike the OpenAI-shim path forge used to carry, they genuinely arrive. |
| `maxOutputTokens` | — | **only here** | Per request, and per role: a planner emitting a backlog needs more than an executor emitting one file. |

Three consequences worth stating plainly.

**`ctx-size` and `contextWindow` are one number in two files.** Forge proves a
prompt fits against config; the server truncates against the preset. When the
config number is larger, the gate approves a prompt the server then cuts *from
the front* — the system message and the spec — and what comes back reads as a
weak model rather than a truncated request. `forge doctor` compares them and
says so:

```
  plan: ok name=plan kind=llamacpp model=nemo-a reply='OK'
      contextWindow is 131,072 but the router starts 'nemo-a' with -c 32,768.
```

Write both from one source with `forge models` and they cannot drift.

**A thinking model with no `reasoning-budget` spends its whole answer
thinking.** Measured on a 30B A3B MoE: every one of 32,768 output tokens went
to hidden reasoning and the reply came back empty, on every call, until the
budget was set. On another run 81 of 86 calls hit the budget exactly — 6,144
tokens of reasoning in front of a 450-token answer, which was 62% of the run's
wall clock. It is the first thing to tune and the last thing anyone thinks to
look at.

**`--models-max` is a global the preset cannot override.** It caps how many
child servers stay resident at once. Set it to 1 unless every checkpoint in the
preset fits in VRAM together; `exclusive: true` per model does the same job from
the other side. The role that discovers you got this wrong is the one whose
child server exits mid-run.

### Generating the preset

`forge models` writes `.hybridforge/models/llamacpp.ini` from the local models
in `config.json`, so the numbers in the two files come from one source:

```
$ forge models
Wrote a llama.cpp preset with 2 model(s): .hybridforge/models/llamacpp.ini
  plan         nemotron-3-nano          C:\AIModels\Nemotron-3-Nano-...Q4_K_M.gguf
  code         qwen3.8                  C:\AIModels\Qwen3.8-27B-UD-Q4_K_M.gguf

Serve it with:
  llama-server --models-preset ".hybridforge/models/llamacpp.ini" --models-max 1
```

`forge init` runs it too, so a new project starts with the right file rather
than a copied one.

A model needs `modelPath` to appear — the `.gguf` is not derivable from a
router id, and a section pointing at the wrong file fails at load with a message
about the file rather than about the config that named it. Cloud models are
skipped: a preset means nothing to an endpoint forge does not start.

Two roles naming the same `model` collapse into one section, because they are
one child server. A planner and an executor sharing a checkpoint and differing
only in `maxOutputTokens` is the ordinary case — that number is per request and
stays in `config.json`.

The file is written; `llama-server` is not started or restarted. It owns the GPU
and outlives any one forge command, so picking up a changed preset is a restart
you choose.

### Thinking models answer last

A thinking model writes its reasoning before a single character of its answer,
and over the OpenAI-compatible shape that reasoning does not arrive in
`content` — llama.cpp and DeepSeek return it as `reasoning_content`, others as
`reasoning`, none of which are in the spec, and all of which still count against
`maxOutputTokens`. Run out of budget mid-thought and the reply is an *empty
string* with `finish_reason: length`, which reads downstream as "planner did not
return usable JSON:" followed by nothing at all.

This bites hardest where replies are longest — respec and whole-file builds — so
a model can pass `forge doctor`, plan a backlog, and still fail every ticket.
Forge names the case rather than passing the empty string on, and retries once
with `reasoning_effort: none` so the call is not simply lost. The fix is still
config.

**Sometimes it arrives in `content` after all.** Depending on the chat template
and how `llama-server` was started, the whole `<think>…</think>` block comes
back inline instead of in a sibling field — and then every parser downstream
reads the model's deliberation as its answer. One sign-off pass was recorded as
blocking a ticket on the strings `...` and `(one line each, or NONE)`, which are
the prompt's own placeholders, quoted back while the model was still deciding
what to say. Forge strips reasoning at the provider boundary now: the answer is
what follows the last closing tag, and an opening tag the model never closed
yields an empty string, because a reply that never finished thinking has no
answer in it. Nothing to configure — it is worth knowing only because a reply
that looks truncated in the logs may have been trimmed here rather than by the
server.

**On llama.cpp, cap the thinking in the preset.** `reasoning-budget` is a hard
ceiling on reasoning tokens, after which the model must begin its answer:

```json
"local": { "kind": "llamacpp", "baseUrl": "http://127.0.0.1:8080/v1",
           "model": "qwen3.8", "contextWindow": 65536,
           "maxOutputTokens": 8192, "reasoningBudget": 2048 }
```

`forge models` writes that into the preset as `reasoning-budget = 2048`. Without
it, a 30B A3B MoE measured here spent all 32,768 of its output budget reasoning
and returned empty content on *every* call.

**Then check it is not merely capped but sensible.** On a later run with the
budget set to 6,144, 81 of 86 executor calls came back at 6,000+ completion
tokens — the model was spending the entire budget every time, in front of a
median 450-token answer. That was 62% of the run's wall clock. A budget that is
always hit is a budget that is too large, not a budget that is working.

Turning thinking off entirely reclaims the budget for the answer:

```json
"local": { "kind": "llamacpp", "baseUrl": "http://127.0.0.1:8080/v1",
           "model": "qwen3.8", "contextWindow": 65536,
           "maxOutputTokens": 8192,
           "extraBody": { "reasoning_effort": "none" } }
```

`extraBody` is merged into the request body verbatim, so it also carries a
gateway's own knobs. On a cloud endpoint it is the only lever — there is no
preset to write.

**`baselineVerify`** (default `true`) runs your verify commands once before each
ticket, so a failure that was already in the tree is not blamed on whichever
ticket happened to run next. Without it, one broken file fails every ticket in
the backlog, each executor is told to fix an error in a file its ticket does not
list, and respec then rewrites specs around somebody else's bug. Turn it off
only when a full suite is slow enough that paying it per ticket costs more than
the attempts it saves:

```json
"loop": { "baselineVerify": false }
```

**`executorTurns`** (default `4`) replays that many prior attempts to the
executor as real conversation turns — its own reply as an `assistant` message,
the failure that followed as the next `user` one — instead of one user message
rewritten every attempt. What it buys is the one thing the flat prompt cannot
say: *you wrote these files*. Shown the same files as disk state with nothing
claiming authorship, a model reads its own work as somebody else's and answers
"they already implement the spec correctly". Turns also append rather than
mutate, so the KV prefix stays stable instead of being re-prefilled each time —
which matters most with a single local model loaded.

The trade is not free: a model shown its own wrong answer as an assistant turn
defends it more readily, and the flat prompt already anchors that way through
disk state. Which effect wins was a measurement, and the Puzzle-Path run of
2026-08-22/23 made it — with this at `0`, one ticket ran 430 attempts whose
failure curve never descended, each attempt meeting its own previous work as a
stranger's. See [CONVERGENCE.md](CONVERGENCE.md). Set it to `0` to restore the
flat prompt:

```json
"loop": { "executorTurns": 0 }
```

Nothing else changes. The conversation is rebuilt from `run.db` on every call,
so transport stays stateless, a retry cycle inherits the thread, and the planner,
tester and reviewer keep their single-turn prompts — a reviewer that inherited
the executor's turns would stop being an independent check.

Then confirm every model actually answers:

```bash
forge doctor
```

### 3.2 Project memory

Without this block the loop runs with no project history: the executor only
sees what a ticket's own `context` field carries, which for an
`forge ingest`-seeded run is usually nothing. Point it at MemPalace to close
that gap.

```json
"room": "image-marquee",
"memory": { "command": ["mempalace-mcp"] }
```

Two transports, and you pick by where the palace lives:

| key | when | what it does |
|---|---|---|
| `command` | palace on the daemon's machine | runs MemPalace as a child process, JSON-RPC over its stdin |
| `url` | palace on another machine | Streamable HTTP to an MCP proxy fronting it (§1.4) |

`command` takes an argv list — a bare string is split on whitespace, with no
shell quoting, so a path with spaces belongs in list form. Setting both keys is
not an error but `command` wins; `forge doctor` prints which transport is live.

`room` scopes retrieval and is inherited from the top-level `room` field unless
you override it inside the block. Other optional keys: `searchTool` (skip
discovery and name the tool yourself), `limit` (results, default 6),
`maxTokens` (cap on retrieved context, default 1200), `timeout`, and
`enabled: false` to turn it off without deleting the block.

**When one `room` is not enough**, name the parameters yourself in
`arguments`. MemPalace scopes on two axes — a *wing* (the project) and a *room*
(the aspect: `decisions`, `backend`, `meetings`) — and no amount of guessing at
parameter names can invent the second one:

```json
"memory": {
  "command": ["mempalace-mcp"],
  "arguments": { "wing": "image-marquee" }
}
```

Every key is passed straight through to the tool being called, and keys that
tool does not declare are dropped — so one block can serve a search tool and a
write tool with different schemas. A configured value wins over the one `room`
would have supplied; the only parameter it cannot touch is the one carrying the
query or entry text, since overwriting that would send an empty search or file
the config block itself as a memory.

The daemon speaks MCP directly rather than going through Claude Code, so
MemPalace has to be startable — or reachable — from **whichever machine runs
the daemon**.

**Tool discovery is automatic**, because MemPalace's tool surface changes
across versions. The client lists the server's tools, picks the one that looks
like retrieval, and fills only the parameters that tool's schema declares. It
will never auto-select a tool whose name suggests it writes. `forge doctor`
prints what it found:

```
memory: ok memory transport=stdio target=mempalace-mcp room=image-marquee
        read=palace_recall write=off available=palace_write_entry, palace_recall
```

A failure names the transport and, under stdio, carries the server's own stderr
— so a palace that dies on a bad path says so instead of timing out silently.

If it picks the wrong tool, or finds none, set `memory.searchTool` explicitly —
the doctor output lists every name the server exposes.

Retrieval is best-effort by design. A memory server that is down logs one
warning and the run continues without context; after three consecutive
failures the loop stops trying for that run rather than paying a connection
timeout on every remaining ticket.

#### Write-back (opt-in)

After a ticket passes verification **and** review, the loop can ask whether the
work produced anything durable — a decision and its reasoning, a convention, or
a review correction — and write that to memory.

```json
"memory": {
  "command": ["mempalace-mcp", "--palace", "/path/to/palace"],
  "write": true,
  "dryRun": true
}
```

**Start with `dryRun: true`.** It runs the whole evaluation and logs exactly
what it *would* record, without sending anything. Watch a few runs before
turning it loose — this is the honest way to find out whether the recorder's
judgment matches yours on your project.

Why it is off by default: retrieval only reads, but write-back mutates a
durable store that every future session reads back, with no undo. A store full
of ticket narration is worse than an empty one, because future models are told
it is established fact.

Guardrails, all verified by the test suite:

| Guard | Behavior |
|---|---|
| Default | `write: false`. Setting `url` alone never enables writes. |
| Timing | Only after review passes. Blocked and failed tickets record nothing. |
| Bias | The recorder is instructed that `NOTHING` is the common, correct answer. |
| Credentials | An entry matching a credential shape is refused **before any network call**, and logged at `error`. Nothing is sent. |
| Destructive tools | Never auto-selected. Naming one in `writeTool` is refused outright. |
| Size | `maxWriteChars` (default 2000). Memory is for decisions, not transcripts. |
| Failure | Never fails a verified ticket — the work is done and reviewed; losing the note is the smaller loss. |

Other keys: `writeTool` (skip discovery), `maxWriteChars`, and `recordRole`
(which role judges durability — defaults to `reviewer`, since it has just read
the diff against the spec).

`writeArguments` layers over `arguments` for the write call only, because reads
and writes want different scopes: a search should span the whole project, while
a recorded decision belongs in one aspect of it. Against MemPalace that is:

```json
"memory": {
  "command": ["mempalace-mcp"],
  "arguments":      { "wing": "image-marquee" },
  "writeTool":      "mempalace_add_drawer",
  "writeArguments": { "room": "decisions", "added_by": "hybrid-forge-daemon" },
  "write": true,
  "dryRun": true
}
```

`writeTool` is belt and braces here: discovery already prefers a primary store
over a side channel, so it picks `add_drawer` rather than `diary_write` — but a
tool this far from being undoable is worth naming rather than inferring.

`forge init` fills `arguments.wing` from the room you give it, so a new repo is
scoped correctly without touching this block by hand.

`forge doctor` shows the write state and which tool was chosen:

```
memory: ok memory url=… room=image-marquee read=palace_recall
        write=ON(palace_remember) available=palace_delete_entry, palace_recall, palace_remember
```

### 3.3 Rate limits and usage windows

For API-key models with published limits, declare them so the loop waits
*before* crossing one rather than discovering it by being cut off:

```json
"claude": {
  "kind": "anthropic",
  "model": "claude-opus-5",
  "rateLimit": { "requestsPerMinute": 50, "tokensPerMinute": 40000 }
}
```

For subscription-backed models the rolling usage window is opaque, so leave
those fields unset and rely on the reactive path: the provider reports the limit
when it hits it, and the gate parks the run until the reported reset time. The
`claude-cli` adapter parses the CLI's limit message for that time; when the
message carries no time it waits 15 minutes and re-probes.

**Spend limits are the exception, and worth setting.** A monthly spend cap is
denominated in dollars, and the `claude-cli` adapter records the CLI's own
`total_cost_usd` for every call — so you can cap on it directly:

```json
"claude": {
  "kind": "claude-cli",
  "model": "opus",
  "rateLimit": { "costPerWindow": 25.00, "windowSeconds": 18000 }
}
```

This is the only *proactive* defence against a billing limit. Without it the
loop only learns about one when the CLI refuses a call — and a refusal that
late still costs the ticket its remaining attempts before the run parks.

Either way the run enters `waiting_budget`, which is a live state — the
dashboard shows the reason and the reopen time, and the loop resumes on its own.

---

## Part 4 — Daily use

### Define the work

Either design it inside Claude Code:

```bash
> /forge-spec add PNG export with configurable DPI
```

which settles the open questions first and writes a document in the shape ingest
parses verbatim — `/forge-spec-check <file>` confirms it before you commit…

…or bring a plan written anywhere else — the Claude desktop app, a web chat, an
ordinary Claude Code session, or a PRD you typed yourself:

```bash
forge ingest plan.md
```

If the document already contains ticket-shaped sections (`## IM-014: title`
with a `### Spec` block), they are parsed **verbatim** — nothing is re-planned
and the acceptance criteria stay the ones their author wrote. Only freeform
documents go through the planner model. `forge ingest` tells you which path it
took; it matters, because a re-planned document has the model's wording of your
criteria rather than yours.

Check a draft before ingesting it. The parser has traps that cost tickets and
report nothing at ingest — a bullet whose continuation is not indented loses its
tail, an unrecognised heading folds its bullets into the section above, and a
criterion about the project's own commands becomes a test that shells out to run
them:

```bash
python plugins/forge-spec/scripts/check_spec.py plan.md
```

It writes nothing and reads the same parser `forge ingest` does. The grammar it
enforces is documented in the `spec-contract` skill under
[`plugins/forge-spec/`](../plugins/forge-spec/).

### Review the backlog

```bash
cat .hybridforge/tickets/*.md
```

This is the last cheap moment to catch a ticket routed `delegate` that should
have been withheld. Edit the ticket files and re-ingest if the split is wrong —
that is much less expensive than discovering it three hours in.

### Go

```bash
forge go --open
```

The loop runs until the backlog is done or you stop it, printing a dashboard
URL. From here you are monitoring, not driving.

```bash
forge status                  # one-shot summary
forge pause | resume | stop   # applied after the current step, never mid-patch
```

**Running past your session.** A loop started inside a Claude Code session dies
with that session. For an overnight run, start it from a terminal that outlives
you:

```bash
nohup forge go --no-ui > forge.log 2>&1 &
forge ui --open               # attach the dashboard separately, any time
```

### When it stops

Read the run status before assuming something is wrong:

| status | meaning |
|---|---|
| `waiting_budget` | **Not stuck.** A usage window is exhausted; it resumes on its own. |
| `paused` | Someone pressed pause. `forge resume`. |
| `blocked` | Backlog exhausted, but tickets need a human. |
| `done` / `failed` / `stopped` | Terminal for that run. |

When the loop stops, `forge go` keeps the dashboard serving until you press
Ctrl-C, so the run you most want to read — which ticket failed, on what, with
the event stream still in front of you — does not vanish with the process. It
holds only when a terminal is attached: a scheduled run or a CI step exits on
its own. Force either way with `--wait` / `--no-wait`, and note that nothing is
lost either way, since `forge ui` serves the same database afterwards.

A `stopped` run is resumable — `forge go` picks it back up as long as tickets
remain. Blocked tickets carry a note saying what they need: a `BLOCKED:` from
the executor means the spec was ambiguous, and the fix is to edit the ticket,
not to re-run and hope.

### Retrying failed tickets

A ticket that exhausts `maxAttempts` is left `failed`, and once nothing is
`pending` the run stops making progress — `forge go` finds the run but no work,
so it just re-declares the backlog exhausted. `forge retry` reopens it:

```bash
forge retry                     # requeue every failed/blocked/skipped ticket
forge retry --ticket TT-005     # just this one, whatever its status
forge retry --all               # redo completed tickets too
forge retry --go                # requeue, then start the loop
```

The loop can do this for itself instead of waiting for you — see
[`retryCycles`](#retrying-without-you--retrycycles) below.

Each retry restores a full attempt budget, so fix the cause first — a spend
limit needs the cap raised, and a reviewer `REJECT` on the same defect three
times running will usually reject a fourth unless the ticket spec changes.

### Letting the planner fix the spec

When the failures are the spec's fault, `--respec` hands each requeued ticket
back to the planner along with every recorded failure and asks what the ticket
got wrong:

```bash
forge retry --respec              # revise every requeued ticket
forge retry --ticket TT-005 --respec
```

The planner rewrites `spec`, `allowed_files`, `reference_files`, and `context`,
and the revised tickets are written back to `.hybridforge/tickets/` for you to
read before starting. What it is looking for:

| Evidence in the failures | What it changes |
|---|---|
| The same rejection every attempt | The wording that let the executor misread it |
| A rejection naming an out-of-scope file | Widens `allowed_files` |
| Behaviour nobody asked for | Adds the missing criterion |
| A concrete defect the reviewer found | Records it in `context` so the next attempt starts knowing |
| A guessed export name, or "I don't have the contents of X" | Adds X to `reference_files` |

### Reference files — what the executor can actually see

**The executor has no filesystem.** It receives the ticket and returns whole
files as text; it cannot open anything. A ticket that says "read `src/api.rs`
for the export names" is asking for something impossible, and the executor will
guess instead — then get rejected for the guess.

`reference_files` is the fix. Files listed there are pasted into the prompt
read-only, alongside the current contents of everything in `allowed_files`:

```json
{"id": "TT-005", "allowed_files": ["web/main.js"],
 "reference_files": ["src/wasm.rs"], "spec": "..."}
```

The planner is told to populate it, `--respec` adds it when the failures show a
guessed API, and both are capped at 24k characters per file so a lockfile
cannot crowd out the spec.

This costs one planner call per ticket, so it is opt-in rather than the
default. It is also a suggestion, not a fix: read the revised spec before
`forge go`. If the failures show the work simply was not finished — no
recurring theme, nothing the spec could have prevented — the planner is told to
return the ticket unchanged and say why, and `forge retry` reports it as
`unchanged` rather than inventing a revision.

A respec never runs before the requeue is committed, so an unreachable or
misconfigured planner costs you the revision, never the retry.

#### What a respec is not allowed to do

Three limits, all of them there because a respec loop without them drifts. The
failure that produced them: one ticket, twelve cycles, ending with acceptance
criteria that asserted the opposite of what its author had written — and a
tester and reviewer downstream that believed the drift completely.

**The criteria are scoped by who wrote them.** Not frozen outright — that was
tried first and made things worse in the other direction: a criterion the loop
had invented became as immutable as a human's, so a respec could mint one no
implementation could satisfy and then never take it back. It rewrote the spec
around it instead, changing an xorshift constant to chase a sequence no
xorshift produces.

So provenance decides. Criteria from the ingested plan are protected: drop or
reword one and it is put back and the attempt logged. Criteria an earlier
revision added are the loop's own — it may revise or retire them on new
evidence. Anything else in the reply is an addition, which is the case the
freeze was wrongly blocking: a plan that specifies scoring in the spec and
states no criterion for it should acquire one. Additions cannot lower the bar.

A plan criterion the *human* has since removed is not resurrected — protecting
the contract must not mean overruling the person who edited it.

Allow wholesale rewrites deliberately when you mean to:

```bash
forge retry --respec --respec-criteria
```

```json
"loop": { "respecCriteria": true }
```

**It is shown the code.** The planner has no filesystem, exactly like the
executor — and a spec written about code nobody showed it is a guess the
executor is then judged on. One respec wrote "SoftDrop decrements `y`" into a
spec whose implementation incremented it. The ticket's writable and reference
files are now pasted into the respec prompt, and it is told every claim it
makes about behaviour must check against them.

**It is shown the original.** The ingested `spec` and `criteria` are kept in
columns nothing can rewrite, and once a ticket has drifted from them both
versions go into the prompt: this is what a human wrote, this is what earlier
revisions made of it, treat unjustified differences as drift to undo. Without
it, revision ten is derived from revision nine and the plan is no longer in the
loop at all.

**It can say the ticket is impossible.** Some criteria are not wrong, they are
unsatisfiable — two that contradict each other, or one asserting a value no
implementation of the spec produces. A planner with no way to report that has
only one move left, which is to bend the spec around the contradiction. So it
can answer with `impossible` instead of a revision, naming the criterion and
the conflict; the ticket parks with that note and spends nothing further:

```
  TT-003     CANNOT BE SATISFIED — Criterion 3 requires Game::new(1) to yield
             [6, 3, 5, 7, 4]. No xorshift32 with the shifts this spec defines
             produces that sequence; seed 1 yields [2, ...].
```

Nothing else in that reply is applied. A spec revised to satisfy a criterion
the planner has just called impossible is a spec bent around a contradiction.

### Retrying without you — `retryCycles`

Everything above is a human typing `forge retry --respec` the next morning. The
loop can do it itself:

It does, by default — `retryCycles` is `-1`, which is "until the backlog is
clean or you stop it". To bound it instead, or to hand every run straight back:

```json
"loop": { "retryCycles": 2, "respecOnRetry": true }
"loop": { "retryCycles": 0 }
```

```bash
forge go --retries 2       # this run only; config is not rewritten
forge go --retries -1      # until the backlog is clean or you stop it
forge go --retries 2 --no-respec
```

When the backlog empties with anything still `failed`, `blocked` or `skipped`,
a cycle requeues all of it, respecs each ticket from its recorded failures, and
runs the backlog again. `0` hands the run back to you after the first pass.

**`-1` means until success or stop.** `forge stop`, Ctrl-C,
`loop.maxRuntimeSeconds` and a spend cap all still apply — they are the brakes,
and one of them is worth setting before leaving a run overnight. Four things
bound it on their own, and the last is why `-1` is the default at all:

- The spent count lives in the run database, not in memory, so a killed daemon
  resuming yesterday's run continues its budget instead of starting a new one.
- A cycle with nothing to requeue ends the run rather than spinning. Two cases
  reach it: every ticket landed and the *final* verify still fails — breakage
  no ticket owns or has the scope to fix — or what is left is withheld.
  Triage still holds during a retry, so a withheld ticket stays withheld rather
  than being requeued; it would only be skipped again, once per cycle, forever.
  `forge release` or `forge discharge` is how one moves.
- **A respec that changed nothing ends the run.** The respec runs *before* the
  requeue, and if every ticket comes back as written there is no cycle left to
  run — the executor would receive the identical ticket that already failed,
  and the only thing still varying is how the model samples. When the planner
  says the ticket is right, the disagreement is between your executor and your
  reviewer, and no rewrite of the ticket settles that. The same applies when
  `respecOnRetry` is on and the planner cannot be reached at all.
- `forge retry` resets the count. A human who has just replaced the specs the
  automatic cycles gave up on gets the full budget against the new ones.
- **A cycle that repeats itself ends the retries.** `flatCycles` compares a
  cycle against the one before it by which *kinds* of failure it produced on
  which tickets. Identical means nothing is varying, so another cycle arrives
  back here — the run stops and says so. This is the measurement that makes
  `-1` a defensible default rather than an open-ended spend: an unattended run
  converges or stops. Both have been observed, one backlog stopping itself
  after a single repeated cycle and the next landing a ticket on the cycle
  after the one that gave up on it. **Turn `flatCycles` off and `-1` loses its
  floor** — set `retryCycles` back to `0` or a small number in the same edit.

Each cycle costs a full backlog of executor calls plus one planner call per
requeued ticket, and the attempt numbering carries on from the last one — so
`retryCycles: 2` with the default `maxAttempts: 5` is up to fifteen attempts on
a ticket that keeps failing, and `-1` is bounded by the repeat detector rather
than by arithmetic. `stopOnBlocked: true` short-circuits all of it: a blocked ticket
stops the run there, and a stopped run is not retried.

Retried attempts are numbered on from where the last cycle stopped, so a ticket
that failed three times writes its next artifacts to `attempt-4`. The failed
attempts stay on disk; a retry never overwrites the evidence for why it failed.

After merge, record durable outcomes to memory: decisions and their reasoning,
new conventions, and any review correction that should not recur.

---

## Operational notes

**Start narrow.** Run the pipeline on a low-stakes slice first — test scaffolding
or a self-contained module — before trusting it with anything load-bearing. The
failure mode you are looking for is not "the code doesn't compile"; it is
plausible code that quietly does the wrong thing.

**Watch what gets delegated over time.** The value of this setup depends entirely
on triage staying honest. If you find tickets touching auth or concurrency
getting routed to the executor because "it's mostly mechanical," tighten
`neverDelegate` rather than relying on judgment in the moment.

**Swap latency is rarely the complaint it looks like.** Alternating roles across
two checkpoints costs a reload each time, but measured with a warm page cache
that is 6-10s for a 15-23 GiB model — 54 swaps came to 3.5% of a 3.5-hour run.
If delegation feels slow, look at generation instead: reasoning tokens were 62%
of that same run's wall clock. See §"Thinking models answer last".

**Keep the security posture.** Three unauthenticated surfaces exist here and
none of them will ever ask who is calling: `llama-server` executes inference for
anyone who can reach it, MemPalace serves your project's decisions to anyone
who can reach it, and the dashboard's stop button ends a run for anyone who can
reach it. All three are bound narrowly for that reason — the dashboard to
loopback, the other two to one interface. If you later want any of them
reachable more broadly, put an authenticating proxy in front rather than
widening the bind address. The daemon prints a warning at startup when the
dashboard is bound off loopback; treat it as a reminder, not a permission slip.

---

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `EXECUTOR_UNREACHABLE` | Network path from the daemon's machine to the host, then `llama-server --host`, then firewall rules |
| `forge doctor` reports FAIL | The error names the kind — auth, unreachable, or bad response |
| MCP server missing from `/mcp` | `claude --debug`; restart Claude Code after config changes |
| MemPalace connects on host but not Mac | Stdio has no address. Either run it on the daemon's machine with `memory.command`, or put an MCP proxy in front (§1.4) |
| Executor returns `BLOCKED:` | The spec is underspecified. Fix the ticket, do not re-run |
| Ticket failed after N attempts | Read the `test` step detail — usually a wrong `commands` entry, not a model problem |
| "rejected out-of-scope edits" in the log | Working as intended. Widen `allowed_files` only if the ticket genuinely needs that file |
| Run sits in `waiting_budget` | Not a fault. The reason and reopen time are on the dashboard |
| Loop won't start: "no run to work on" | Nothing ingested yet — `forge ingest <spec>` |
| `forge init` asked nothing | No TTY (piped, redirected, or run by an agent). It took the defaults and printed them; edit the config, or re-run it in a terminal |
| `forge init` filled in the wrong endpoints | A stale machine profile. Overwrite the answers, or delete the profile — see §3.0 for its path |
| `forge init` found no verify commands | Nothing in the repo states them — no CI, no Makefile, no contributing guide. Fill them in yourself; §3.1 explains why this field matters most |
| Detected commands are wrong | It reports which file each came from. If CI is stale, fix `config.json` directly — the loop uses that, not CI |
| "could not build the planner model" during detection | Detection runs on the planner role, which is configured one question earlier. A failed probe there leaves nothing to ask |
| `memory: FAIL` in doctor | Under `command`, the report carries the server's own stderr — read that first; a wrong subcommand shows up as an immediate exit. Under `url`, wrong address or no proxy running |
| Memory connects but retrieves nothing | Wrong tool auto-selected, or wrong `room`. Doctor prints the chosen tool and every available name; set `memory.searchTool` |
| `/v1/models` lists nothing, or not what you expect | The router is reading a different preset than the one `forge models` wrote, or the paths in it do not resolve on the server's side of a bind mount. The id forge sends must be a section name in the preset the router was started with |
| `prompt_budget` is negative | Not a ticket that is too large, whatever the blocked note says: `maxOutputTokens` exceeds the window. Either the window collapsed to a default because discovery failed, or the two were set independently |
| A model answers but the reply is empty, `finish_reason: length` | A thinking model spent its whole output budget reasoning. Set `reasoningBudget` and re-run `forge models`, then restart the router so it re-reads the preset |
| Every retry produces byte-identical output | Temperature is too low for retries to explore. See §Sampling |
| Every ticket blocks on context overflow | The model's window is too small for these tickets. Split them, or raise `contextWindow` if it was set too low by hand |
| "planner did not return usable JSON:" with nothing after the colon | A thinking model spent its whole output budget reasoning and returned empty `content`. Set `reasoningBudget`, raise `maxOutputTokens`, or set `"extraBody": {"reasoning_effort": "none"}` — see §"Thinking models answer last" |
| Builds truncate mid-file, `finish_reason: length` | `maxOutputTokens` too small for a whole-file reply. It defaults to 4096, which a thinking model half-spends before writing any code |
| Slow first token every ticket | A checkpoint swap on a cold page cache, or another process holding VRAM. `nvidia-smi` while the run is going says which |
| The run keeps re-running the same backlog | `loop.retryCycles` is set (or `forge go --retries -1` is in the command). `forge stop`, then read the respec revisions before setting it going again |
