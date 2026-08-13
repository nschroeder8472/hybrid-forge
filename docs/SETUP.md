# Hybrid Forge — setup

End-to-end setup for the plan-and-execute pipeline: Claude plans and reviews,
a locally hosted Qwen3.6-35B-A3B executes, MemPalace carries project decisions
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
| **Daemon + plugin** | The `forge` CLI, skills, commands, MCP config | Every machine you code from — it must sit with the repo | Once per machine |
| **Project config** | `.hybridforge/` — models, roles, commands, tickets | Per repository | Once per repo |

The plugin is a few kilobytes of text. It does not contain the model. It contains
the address of the model.

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
| **GPU host** (the 5090) | Ollama + MemPalace, in Docker | Needs the GPU and holds the palace |
| **Workstation** (your Mac) | repo, daemon, Claude Code, toolchain | Needs the files |

Only model calls and memory reads cross the network. Files, git, and builds stay
local and fast.

### A worked example: Mac + 5090

Nothing new on the GPU host — the existing `docker-compose.yml` is already
exactly right. On the Mac, everything is local except two URLs:

```json
{
  "models": {
    "local":  { "kind": "openai", "baseUrl": "http://forge-host:11434/v1",
                "model": "qwen3.6:35b-a3b", "contextWindow": 32768 },
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

```bash
# Ollama — the inference server
curl -fsSL https://ollama.com/install.sh | sh
```

You also need a network path from your workstation to this host. Anything that
gives the two machines a stable address for each other works — a LAN, a VPN, an
overlay network, an SSH tunnel. Pick one now and note the address, because
§1.3 binds the services to it. What matters is not which you choose but that
the resulting address is reachable by your workstation and *not* by anything
else: neither service below has authentication.

Confirm the GPU is visible to Ollama:

```bash
nvidia-smi
ollama --version
```

### 1.2 Pull the executor model

```bash
ollama pull qwen3.6:35b-a3b
```

At Q4 this lands comfortably inside 32GB with room for a working context. If you
want more headroom for long specs, or more quality, benchmark Q4 against Q6 on
your own code before committing — published benchmark deltas will not tell you
how it does on your Rust or Swift.

Verify it loads and answers:

```bash
ollama run qwen3.6:35b-a3b "Reply with exactly: OK"
nvidia-smi   # confirm VRAM occupancy looks sane
```

### 1.3 Bind the inference server to one interface

**Ollama ships with no authentication.** Binding it to `0.0.0.0` publishes an
endpoint that will happily execute inference for anything that can reach the
port — every network this host is attached to, including whatever your router
is doing. Bind it to a single address instead, and make it the narrowest one
that your workstation can still reach.

List the candidates and pick one:

```bash
ip -4 -o addr show scope global      # interface name and address, one per line
```

```bash
BIND_ADDR=<the address you picked>
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=${BIND_ADDR}:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

`OLLAMA_KEEP_ALIVE=30m` keeps weights resident between tickets. Without it you
pay a cold load on every delegation, which is the single biggest source of
"why is this so slow" in this setup.

Verify from the host, then from your workstation:

```bash
curl http://${BIND_ADDR}:11434/v1/models       # on host
curl http://<host-address>:11434/v1/models     # on the workstation
```

Both checks matter, and the second is the one that counts — an endpoint
answering on the machine it runs on proves nothing about reachability. If it
fails, the problem is the bind address, a firewall rule, or your VPN's access
controls. Fix it here before going further; everything downstream assumes this
works.

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

Unlike Ollama, this listener authenticates. A non-loopback bind auto-generates
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

Same bind rule as Ollama: the narrowest interface that your daemon still
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
docker compose up -d
docker compose exec ollama ollama pull qwen3.6:35b-a3b
```

The first pull writes into the `ollama-models` volume and persists from then on.

### Teardown semantics

```bash
docker compose down          # destroys containers, keeps volumes — what you want
docker compose down -v       # ALSO destroys the volumes: 20GB re-pull, palace gone
```

The compute layer is disposable; the state is not. `-v` is the footgun.

### Two things that behave differently under containers

**Host networking is doing real work here.** A container on the default bridge
network has its own network namespace and cannot see the host's interfaces, so
`OLLAMA_HOST` pointed at a host address would fail to bind — this bites hardest
with VPN and overlay interfaces, which exist only on the host.
`network_mode: host` makes the container's bind address the host's bind
address, which is why the security posture from 1.3 carries over unchanged.
Note that `ports:` is ignored in this mode — `BIND_ADDR` is what controls
exposure, so a wrong value there is not contained by Docker.

**Restarts evict VRAM.** `OLLAMA_KEEP_ALIVE` only helps within a container's
lifetime. If your habit is spinning the stack up and down around each coding
session, you pay a cold load on the first delegation every time. Leaving the
containers running and restarting only on config changes gets you the disposable
runtime without that cost.

### Windows caveat

If the 5090 sits in a Windows workstation, run Ollama natively and containerize
only MemPalace. Docker Desktop routes through WSL2, where GPU passthrough works
but adds a filesystem translation layer that is genuinely slow for large
sequential reads — exactly the access pattern of loading 20GB of weights — and is
more fragile across driver updates.

---

## Part 2 — Client setup (each machine you code from)

Do this on the 5090 itself as well — the plugin is per-machine even when the
services are local, because the 5090 running Claude Code is just another client
that happens to have a very short network path.

### 2.1 Install the plugin

From your GitHub repo:

```bash
claude plugin marketplace add <your-github-user>/hybrid-forge
claude plugin install hybrid-forge@nms-forge
```

For local development, skip the marketplace and load the directory directly:

```bash
claude --plugin-dir /path/to/hybrid-forge
```

### 2.2 Configure endpoints

The plugin declares its endpoints in `userConfig`, so Claude Code prompts for
them at enable time rather than making you hand-edit settings. Supply:

- `executorBaseUrl` — `http://<host-address-or-name>:11434/v1`
- `executorModel` — `qwen3.6:35b-a3b`
- `memPalaceUrl` — your MemPalace endpoint from step 1.4

### 2.3 Install the daemon

```bash
pip install -e /path/to/hybrid-forge      # provides the `forge` command
```

The daemon and CLI have **no runtime dependencies** — stdlib Python 3.10+ only.
That is deliberate: a failed `pip install` is a bad way to discover that an
overnight run never started.

The optional MCP shim (`mcp_servers/executor_server.py`, used for interactive
delegation rather than the loop) does need one package:

```bash
pip install "hybrid-forge[mcp]"           # or: pip install mcp
```

If you want the loop to drive Claude for planning and review, Claude Code must
be installed **on whatever machine runs the daemon** — the `claude-cli` adapter
shells out to it. That machine is usually your workstation, not the GPU host.

### 2.4 Verify

```bash
forge --help
```

For the interactive MCP path as well:

```bash
claude
> /mcp
```

You should see `forge-executor` and `mempalace` connected. Then ask Claude to run
`executor_health` — it should return the model, its context window, and a live
reply.

If a server fails to connect, `claude --debug` shows connection attempts and tool
discovery, which is considerably more informative than the summary view.

---

## Part 3 — Per-project setup

In each repository you want to use the pipeline in:

```bash
cd ~/code/image-marquee
forge init          # or, inside Claude Code: /forge-init
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
    "local":  { "kind": "openai", "baseUrl": "http://forge-host:11434/v1",
                "model": "qwen3.6:35b-a3b", "contextWindow": 32768 },
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
  "loop": { "maxAttempts": 3, "autoCommit": false, "stopOnBlocked": false,
            "retryCycles": 0, "respecOnRetry": true, "respecCriteria": false },
  "ui":   { "host": "127.0.0.1", "port": 8799 }
}
```

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

`contextWindow` and `maxOutputTokens` are per model, and config always wins over
discovery. Two things about them are worth getting right before the first run,
because neither fails a health probe.

**Set `contextWindow` to what your server is serving, not what the model can
do.** These are different numbers and they routinely disagree. Ollama reports
the architectural maximum through `/api/show` and the loaded `num_ctx` through
`/api/ps`; on one box those read 131072 and 32768. Forge asks `/api/ps` first
for exactly this reason. Believing the larger figure defeats the budget gate —
it approves a 90k prompt for a 32k server, which truncates from the *front*,
taking the system prompt and the spec with it. What comes back then reads as a
weak model rather than a truncated request.

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
`temperature 1.0`. Check with `ollama show <model>` before overriding.

Keep the reviewer tighter than the rest. Its verdict has to parse, and a
verdict that does not is treated as a rejection.

**State it rather than discovering it.** `/api/ps` reports nothing until a
model is resident, so the first probe of a run can read the architectural
maximum and cache it for the whole run — a race whose outcome depends on
whether something happened to load the model first. Setting `contextWindow`
explicitly per model removes it.

Raise the server instead if you want the rest of the window:
`OLLAMA_CONTEXT_LENGTH=131072`, or `num_ctx` in the Modelfile. A larger
`num_ctx` allocates a larger KV cache, so check it still fits in VRAM — `forge
doctor` warns when a model is only partly resident, which costs several times
the speed rather than failing outright.

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

Ollama takes the same setting from three places, and they do not agree. Which
one wins is not obvious, and getting it wrong is silent:

| Setting | Modelfile | `config.json` | `OLLAMA_CONTEXT_LENGTH` | Ollama app settings |
|---|---|---|---|---|
| `num_ctx` | **wins** | — | applies when no Modelfile pins it | overridden by the env var |
| `temperature` | ignored by forge | **wins** | — | — |
| `top_p` | ignored by forge | **wins** | — | — |
| `top_k` | **only place it works** | sent, discarded | — | — |
| `min_p` | **only place it works** | sent, discarded | — | — |
| models directory | — | — | `OLLAMA_MODELS` | used when the env var is unset |

Three consequences worth stating plainly.

**`top_k` and `min_p` do not reach the model through the OpenAI endpoint.**
Measured against Ollama 0.32: `top_p 0.01` collapses six samples to one, while
`top_k 1` leaves all six distinct. The shim accepts both and applies only the
OpenAI-standard ones. Set them in the Modelfile; `forge doctor` warns if it
finds them in config against an Ollama endpoint.

**A Modelfile `PARAMETER temperature` is inert for forge**, which sends an
explicit temperature on every request — 0.0 for the reviewer and planner, 0.1
for the tester, 0.2 for the executor. It only applies when something other
than forge calls the model.

**`OLLAMA_CONTEXT_LENGTH` is one number for every model.** A per-model pin in
a Modelfile is the only way to give a 24B reviewer and a 35B executor different
windows, and the global silently overrides the desktop app's own setting. If
you pin per model, unset the global — otherwise you are maintaining two
sources of truth for one value and only one of them is in effect.

The models directory has the same shape. The desktop app stores its own path
in `%LOCALAPPDATA%\Ollama\db.sqlite`, and `OLLAMA_MODELS` overrides it. When
they disagree, whichever launched the server decides — a tray-launched server
and a hand-started `ollama serve` can read different directories, and the
symptom is every model vanishing at once. Make them agree.

### Generating the Modelfiles

`forge models` writes one per Ollama-backed model into
`.hybridforge/models/`, using each model's real numbers rather than a
remembered default:

```
$ forge models
Wrote 4 Modelfile(s) in .hybridforge/models:
  local-code   ollama create forge-code -f ".../Modelfile.local-code"
  ...
```

It reads the trained maximum from `/api/show`, keeps the base model's own
sampling recipe where it ships one, and pins `contextWindow` when config
states it. `forge init` runs it too, so a new project starts with the right
file instead of a copied one.

The files are written; `ollama create` is not run. Building takes minutes and
changes something outside the repository.

One detail it handles that is easy to get wrong by hand: when config names a
base model directly, the generated file is `FROM` those weights, and building
it under the same name would *replace the model it derives from*. In that case
a new name is proposed and the output says which config key to update.

### Thinking models answer last

A thinking model writes its reasoning before a single character of its answer,
and over the OpenAI-compatible shape that reasoning does not arrive in
`content`. Ollama returns it as `reasoning`, vLLM and DeepSeek as
`reasoning_content` — none of which are in the spec, and all of which still
count against `maxOutputTokens`. Run out of budget mid-thought and the reply is
an *empty string* with `finish_reason: length`, which reads downstream as
"planner did not return usable JSON:" followed by nothing at all.

This bites hardest where replies are longest — respec and whole-file builds —
so a model can pass `forge doctor`, plan a backlog, and still fail every ticket.
Forge now names this case instead of passing the empty string on, but the fix is
config. Either give the thinking room:

```json
"local": { "kind": "openai", "baseUrl": "http://127.0.0.1:11434/v1",
           "model": "qwen3.6:35b-a3b", "contextWindow": 32768,
           "maxOutputTokens": 16384 }
```

or turn thinking off and reclaim the budget for the answer:

```json
"local": { "kind": "openai", "baseUrl": "http://127.0.0.1:11434/v1",
           "model": "qwen3.6:35b-a3b", "contextWindow": 32768,
           "maxOutputTokens": 8192,
           "extraBody": { "reasoning_effort": "none" } }
```

`extraBody` is merged into the request body verbatim, so it also carries vLLM's
`top_k` and OpenRouter's routing preferences. Note that `reasoning_effort` is
the only thinking switch Ollama's `/v1` endpoint honors — `think: false` and
`chat_template_kwargs.enable_thinking` are accepted and silently ignored, and
the model keeps thinking.

Check which you have before guessing: `ollama show <model>` lists `thinking`
under capabilities.

**`baselineVerify`** (default `true`) runs your verify commands once before each
ticket, so a failure that was already in the tree is not blamed on whichever
ticket happened to run next. Without it, one broken file fails every ticket in
the backlog, each executor is told to fix an error in a file its ticket does not
list, and respec then rewrites specs around somebody else's bug. Turn it off
only when a full suite is slow enough that paying it per ticket costs more than
the attempts it saves:

```json
"loop": { "maxAttempts": 3, "baselineVerify": false }
```

**`executorTurns`** (default `0`, off) replays that many prior attempts to the
executor as real conversation turns — its own reply as an `assistant` message,
the failure that followed as the next `user` one — instead of one user message
rewritten every attempt. What it buys is the one thing the flat prompt cannot
say: *you wrote these files*. Shown the same files as disk state with nothing
claiming authorship, a model reads its own work as somebody else's and answers
"they already implement the spec correctly". Turns also append rather than
mutate, so the KV prefix stays stable instead of being re-prefilled each time —
which matters most with a single local model loaded.

It is off because the trade is not clean: a model shown its own wrong answer as
an assistant turn defends it more readily, and the flat prompt already anchors
that way through disk state. Which effect wins is a measurement, so run the same
backlog both ways rather than assuming:

```json
"loop": { "maxAttempts": 3, "executorTurns": 2 }
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

Either plan inside Claude Code:

```bash
> /forge-plan add PNG export with configurable DPI
```

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

### Review the backlog

```bash
cat .hybridforge/tickets/*.md
```

This is the last cheap moment to catch a ticket routed `delegate` that should
have been `claude-only`. Edit the ticket files and re-ingest if the split is
wrong — that is much less expensive than discovering it three hours in.

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

```json
"loop": { "retryCycles": 2, "respecOnRetry": true }
```

```bash
forge go --retries 2       # this run only; config is not rewritten
forge go --retries -1      # until the backlog is clean or you stop it
forge go --retries 2 --no-respec
```

When the backlog empties with anything still `failed`, `blocked` or `skipped`,
a cycle requeues all of it, respecs each ticket from its recorded failures, and
runs the backlog again. `0` is the default and hands the run back to you.

**`-1` means until success or stop, and nothing else ends it.** `forge stop`,
Ctrl-C, `loop.maxRuntimeSeconds` and a spend cap all still apply — they are the
brakes, so set one before leaving it. Three things bound it on their own:

- The spent count lives in the run database, not in memory, so a killed daemon
  resuming yesterday's run continues its budget instead of starting a new one.
- A cycle with nothing to requeue ends the run rather than spinning. Two cases
  reach it: every ticket landed and the *final* verify still fails — breakage
  no ticket owns or has the scope to fix — or what is left is claude-only.
  Triage still holds during a retry, so a claude-only ticket is never requeued;
  it would only be skipped again, once per cycle, forever.
- **A respec that changed nothing ends the run.** The respec runs *before* the
  requeue, and if every ticket comes back as written there is no cycle left to
  run — the executor would receive the identical ticket that already failed,
  and the only thing still varying is how the model samples. When the planner
  says the ticket is right, the disagreement is between your executor and your
  reviewer, and no rewrite of the ticket settles that. The same applies when
  `respecOnRetry` is on and the planner cannot be reached at all.
- `forge retry` resets the count. A human who has just replaced the specs the
  automatic cycles gave up on gets the full budget against the new ones.

Each cycle costs a full backlog of executor calls plus one planner call per
requeued ticket, and the attempt numbering carries on from the last one — so
`retryCycles: 2` with `maxAttempts: 3` is up to nine attempts on a ticket that
keeps failing. `stopOnBlocked: true` short-circuits all of it: a blocked ticket
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

**Cold-start latency is the usual complaint.** If delegation feels slow, check
`OLLAMA_KEEP_ALIVE` and whether another process evicted the weights from VRAM.

**Keep the security posture.** Three unauthenticated surfaces exist here and
none of them will ever ask who is calling: Ollama executes inference for
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
| `EXECUTOR_UNREACHABLE` | Network path from the daemon's machine to the host, then `OLLAMA_HOST` bind address, then firewall rules |
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
| Every model vanishes at once (`/api/tags` empty) | The server is reading a different models directory than the one holding them. `OLLAMA_MODELS` and the desktop app's own setting disagree; whichever launched the server won. See §"Where each setting has to live" |
| `prompt_budget` is negative | Not a ticket that is too large, whatever the blocked note says: `maxOutputTokens` exceeds the window. Either the window collapsed to a default because discovery failed, or the two were set independently |
| `topK` / `minP` in config appear to do nothing | They do nothing. Ollama's OpenAI endpoint accepts and discards them — put them in the Modelfile, and run `forge models` to generate one |
| Every retry produces byte-identical output | Temperature is too low for retries to explore. See §Sampling |
| Every ticket blocks on context overflow | The model's window is too small for these tickets. Split them, or raise `contextWindow` if it was set too low by hand |
| "planner did not return usable JSON:" with nothing after the colon | A thinking model spent its whole output budget reasoning and returned empty `content`. Raise `maxOutputTokens` or set `"extraBody": {"reasoning_effort": "none"}` — see §"Thinking models answer last" |
| Builds truncate mid-file, `finish_reason: length` | `maxOutputTokens` too small for a whole-file reply. It defaults to 4096, which a thinking model half-spends before writing any code |
| Slow first token every ticket | `OLLAMA_KEEP_ALIVE`, VRAM eviction by another process |
| The run keeps re-running the same backlog | `loop.retryCycles` is set (or `forge go --retries -1` is in the command). `forge stop`, then read the respec revisions before setting it going again |
