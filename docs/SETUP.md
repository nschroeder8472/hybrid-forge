# Hybrid Forge — setup

End-to-end setup for the plan-and-execute pipeline: Claude plans and reviews,
a locally hosted Qwen3.6-35B-A3B executes, MemPalace carries project decisions
across sessions.

There are three distinct installs, and conflating them is the most common way
this goes wrong:

| | What it is | Where it runs | How often |
|---|---|---|---|
| **Host services** | Model weights, inference server, MemPalace store | 5090 desktop only | Once |
| **Plugin** | Manifest, skills, commands, MCP config — all text | Every machine you code from | Once per machine |
| **Project config** | `.hybridforge/` — room pointer, conventions, tickets | Per repository | Once per repo |

The plugin is a few kilobytes of text. It does not contain the model. It contains
the address of the model.

---

## Part 1 — Host setup (5090 desktop)

### 1.1 Prerequisites

```bash
# Ollama — the inference server
curl -fsSL https://ollama.com/install.sh | sh

# Tailscale, if not already on this machine
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

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

### 1.3 Bind the inference server to Tailscale only

**Ollama ships with no authentication.** Binding it to `0.0.0.0` publishes an
unauthenticated endpoint that will happily execute inference for anything on your
LAN. Bind it to the Tailscale interface instead.

```bash
TS_IP=$(tailscale ip -4 | head -n1)
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=${TS_IP}:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

`OLLAMA_KEEP_ALIVE=30m` keeps weights resident between tickets. Without it you
pay a cold load on every delegation, which is the single biggest source of
"why is this so slow" in this setup.

Verify from the host, then from your Mac:

```bash
curl http://$(tailscale ip -4 | head -n1):11434/v1/models          # on host
curl http://<host-tailscale-name>:11434/v1/models                  # on Mac
```

If the second fails, the problem is Tailscale ACLs or the bind address — fix it
here before going further. Everything downstream assumes this works.

### 1.4 MemPalace

Install MemPalace on the host and ingest whatever project history you already
have. **Verify the current install and transport instructions against the
MemPalace docs** — this project moved fast through 2026 and its MCP surface has
changed across versions.

Two things to establish before wiring clients to it:

1. **Transport.** MemPalace exposes an MCP server, but if it defaults to stdio it
   will only work for a client on the same machine. This pipeline needs it
   reachable from your Mac, so you need either a network transport (HTTP/SSE) or
   an MCP proxy fronting the stdio server. Confirm which your version supports.
2. **Bind address.** Same reasoning as Ollama — Tailscale interface only.

The palace database stays here, on the host, permanently. One authoritative copy.
Do not sync it, do not check it into a repo, do not keep a second copy on the Mac.

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
tailscale ip -4 | head -n1        # put this in TS_IP
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
network cannot see the host's `tailscale0` interface, so `OLLAMA_HOST` pointed at
a Tailscale IP would fail to bind. `network_mode: host` makes the container's
bind address the host's bind address, which is why the security posture from 1.3
carries over unchanged. Note that `ports:` is ignored in this mode — the
environment variable is what controls exposure.

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

- `executorBaseUrl` — `http://<host-tailscale-name>:11434/v1`
- `executorModel` — `qwen3.6:35b-a3b`
- `memPalaceUrl` — your MemPalace endpoint from step 1.4

### 2.3 Install the shim's dependency

The delegation shim needs the MCP Python SDK:

```bash
pip install mcp
```

### 2.4 Verify

```bash
claude
> /mcp
```

You should see `forge-executor` and `mempalace` connected. Then ask Claude to run
`executor_health` — it should return the endpoint, model, and a live reply.

If a server fails to connect, `claude --debug` shows connection attempts and tool
discovery, which is considerably more informative than the summary view.

---

## Part 3 — Per-project setup

In each repository you want to use the pipeline in:

```bash
cd ~/code/image-marquee
claude
> /forge-init
```

This creates:

```
.hybridforge/
├── config.json        # room pointer, test/lint commands, never-delegate globs
├── conventions.md     # human-readable, reviewed in PRs like any other doc
└── tickets/           # one markdown file per delegated unit of work
```

Commit all of it. It is text, it diffs cleanly, and `conventions.md` in
particular benefits from being reviewable — it is the document that stops the
executor from relitigating decisions.

The `room` field scopes every memory read and write to this project. Without it,
queries pull decisions from unrelated repos and present them as authoritative,
which is worse than having no memory at all.

---

## Part 4 — Daily use

```bash
> /forge-plan add PNG export with configurable DPI
```

Claude retrieves project context, breaks the work into tickets, and marks each
`delegate` or `claude-only`. Review the split before proceeding — this is the
step where you catch a risky ticket being routed to the executor.

```bash
> /forge-run IM-014
```

Claude delegates, applies the result, has tests written against the criteria it
authored, runs lint/type-check/tests, and only then reviews the diff against the
spec.

After merge, ask Claude to record durable outcomes to memory: decisions and their
reasoning, new conventions, and any review correction that should not recur.

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

**Keep the security posture.** Both services are unauthenticated and bound to
Tailscale. If you later want them reachable more broadly, put them behind
Authentik like your other services rather than widening the bind address.

---

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `EXECUTOR_UNREACHABLE` | Tailscale connectivity, then `OLLAMA_HOST` bind address |
| MCP server missing from `/mcp` | `claude --debug`; restart Claude Code after config changes |
| MemPalace connects on host but not Mac | Transport is stdio — needs HTTP or a proxy (see 1.4) |
| Executor returns `BLOCKED:` | The spec is underspecified. Fix the spec, do not work around it |
| Executor edits files outside scope | Reject the diff; tighten `allowed_files` and the ticket spec |
| Slow first token every ticket | `OLLAMA_KEEP_ALIVE`, VRAM eviction by another process |
