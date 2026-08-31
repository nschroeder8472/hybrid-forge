# Hybrid Forge — getting started

A guided, start-to-finish setup: the local model, project memory, the `forge`
daemon, and a narrated walkthrough of your first `forge init`. Follow it top to
bottom once and you will have a repository running the loop.

[SETUP.md](SETUP.md) is the reference manual — every option, every alternative,
and the full security discussion. This guide picks one good path through it and
explains what each step is for.

---

## What you are building

You describe a feature. A daemon called `forge` breaks it into tickets, and for
each ticket it writes the code, runs your lint and tests, reviews the diff
against the spec, and moves on. It keeps going while you do something else.

Four pieces, and it is worth knowing what each is for before you install
anything:

| Piece | What it does | Required? |
|---|---|---|
| **Executor model** | Writes the code. High volume, so this is the one you want cheap — usually a local model on your own GPU | Yes |
| **Planner / reviewer model** | Decides what to build, and checks the result against the spec. This should be a strong model | Yes |
| **`forge` daemon** | Owns the loop. Writes files, runs your tests, builds the diff | Yes |
| **MemPalace** | Remembers decisions across sessions, so the executor does not re-litigate a convention you settled last month | Optional, but it is most of what makes runs two and three better than run one |

**The executor and the reviewer must not be the same model.** A model reviewing
its own diff approves it. That check is the only thing standing between a cheap
executor and plausible code that quietly does the wrong thing.

### Where things run

The daemon has to sit **on the machine that holds the repository**. It writes
files to disk, runs your test commands, and shells out to `git`. It is not a
service you can point at a repo over the network.

The model and the memory server can live anywhere you can reach:

| Your situation | Do this |
|---|---|
| One machine with a decent GPU | Everything local. Simplest — start here |
| Laptop for code, desktop with the GPU | `llama-server` and MemPalace on the desktop, repo and daemon on the laptop |
| No GPU at all | Use a hosted API for the executor, everything else local |

This guide assumes one machine. Where a second machine changes something, it
says so and points at the relevant part of SETUP.md.

---

## Prerequisites

```bash
python3 --version        # need 3.10 or newer
git --version
```

You also want a **low-stakes git repository** for your first run — a side
project, or a branch you would not mind throwing away. The loop writes real
files to disk.

---

## Part 1 — The executor model

This is the model that writes code. It runs constantly during a run, which is
why it is worth hosting yourself.

### Install llama.cpp

`llama-server` is the only local backend forge speaks to. Let forge fetch it:

```bash
forge llama install
```

That picks the build for your machine, downloads it, checks it against the
SHA-256 GitHub published for it, and unpacks it where forge will find it.
`forge llama` on its own says what it would pick and what you already have.

**Which build matters more than which version.** Measured on a 5090 with a 30B
A3B MoE at Q4_K_M: 16 tok/s on the Vulkan build against 353 tok/s on CUDA. That
is not tuning, it is whether an overnight run finishes — and nothing reports the
slow path, which is why forge chooses rather than leaving it to a paragraph.
Detection reads your GPU's compute capability; `--backend` overrides it.

To install it yourself instead, take a release binary from
[the releases page](https://github.com/ggml-org/llama.cpp/releases) and put
`llama-server` on PATH — forge uses that when it has fetched nothing. On
Windows the CUDA builds need the matching `cudart-*` archive unpacked beside
them, or the server exits on a missing DLL without mentioning CUDA.

The server speaks an OpenAI-compatible API, which is what forge sends. Your
endpoint will be `http://127.0.0.1:8080/v1`.

### Pick a model

`llama-server` serves `.gguf` files directly — there is no pull step and no
model store. Download one and note the path:

```bash
curl -fsSLO https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q4_K_M.gguf
```

A mixture-of-experts model is worth seeking out here: a 30B MoE with ~3B active
per token runs several times faster than a dense model of the same size, and
the executor is the role that runs constantly.

If that does not fit your hardware, roughly:

| VRAM | Try | Expect |
|---|---|---|
| 32 GB+ | A 27-30B at Q4_K_M, MoE if you can get one | Best results of anything you can host at home |
| 24 GB | The same at a lower quantization, or a ~14B coder model at Q4 | Works well; occasionally needs a second attempt on a ticket |
| 16 GB | A 14B coder-tuned model at Q4 | Usable on well-specified tickets; split work into smaller pieces |
| 8–12 GB | A 7B coder-tuned model at Q4 | It will write code, but expect more `BLOCKED:` tickets and more failed verify rounds |
| No GPU | A hosted API — see below | Costs money per token, works anywhere |

Leave room for the KV cache, which is not small and scales with the context
window: measured at `--ctx-size 131072`, a 15.3 GiB Qwen at Q4 occupied 24.6 GiB
resident. A window you will not use is VRAM you could have spent on weights.

**What actually matters in this role** is not benchmark score. The executor is
handed a spec, a list of files it is allowed to touch, and acceptance criteria,
and it has to return edits in a fixed format without wandering outside scope.
Instruction-following and a long context window beat raw reasoning here. A model
that is brilliant but ignores the output format is useless to the loop.

A thinking model needs one more thing: a `reasoningBudget`. Without a cap, one
30B MoE measured here spent all 32,768 of its output tokens reasoning and
returned an empty answer on *every* call. Set it and the same model answers in
146. See [SETUP.md §"Thinking models answer last"](SETUP.md#thinking-models-answer-last).

### Serve it in router mode

```bash
llama-server --models-preset .hybridforge/models/llamacpp.ini --models-max 1
```

Router mode is what lets forge swap checkpoints: each section of the preset is a
model id, and the router spawns one child server per id and proxies by name.
Even with a single model it is the right shape, because adding a second one
later is then a config edit rather than a second server.

You do not have to write that preset. `forge init` and `forge models` generate
it from `config.json`, which is the point — `ctx-size` there and `contextWindow`
here are the same number, and keeping them in step by hand is where the silent
failures live. Step 1 below is where you give forge the `.gguf` path it needs.

`--models-max 1` keeps one checkpoint resident. Raise it only if every model in
the preset fits in VRAM together.

> **Security note, if the GPU is on another machine:** `llama-server` has no
> authentication of any kind. Anything that can reach the port can run
> inference on your hardware. Bind it to one specific private address with
> `--host` — never `0.0.0.0`.

### Check it works

```bash
curl http://127.0.0.1:8080/v1/models
```

That lists every id in the preset with its status. Those ids are exactly what
`model` in your config must name.

### No GPU? Use a hosted endpoint

Any OpenAI-compatible API works — OpenRouter, DeepSeek, Together, OpenAI
itself. You need three things: a base URL, a model name, and an API key set as
an environment variable.

```bash
export OPENROUTER_API_KEY=sk-...          # macOS / Linux
$env:OPENROUTER_API_KEY = "sk-..."        # Windows PowerShell
```

Forge never stores the key. It stores the **name** of the variable holding it,
so your config file stays safe to commit.

---

## Part 2 — The planner and reviewer

This model plans the work and reviews the executor's diff. It runs far less
often than the executor, so it is worth spending on.

Three ways to provide it, in order of convenience:

**1. Claude Code CLI** (easiest — no API key, uses the subscription you already
have):

```bash
claude --version
```

If that prints a version, you are done. Forge shells out to `claude -p` for
planning and review.

**2. Anthropic API** — set `ANTHROPIC_API_KEY` in your environment.

**3. Google Gemini** — set `GEMINI_API_KEY` in your environment.

There is a fourth option in the setup wizard: use the executor model for this
too. It exists for experimenting on hardware that has nothing else available.
It removes the check that makes the whole design work — do not choose it for
real work.

---

## Part 3 — Project memory (MemPalace)

**What it is for.** Without memory, each ticket sees only its own spec. The
executor cannot know that you settled on a particular error-handling pattern
three weeks ago, so it invents a new one, and your reviewer spends its budget
correcting the same thing repeatedly. MemPalace is a small MCP server that
stores decisions and hands them back before each ticket.

This part is optional. **You can skip it and add it later** — nothing else in
the setup depends on it, and a memory outage never ends a run, it only removes
context. If you would rather get a first run done today, skip to Part 4.

### Install it

MemPalace publishes a container image, so there is nothing to build:

```bash
docker pull ghcr.io/mempalace/mempalace:latest
```

If you would rather install it natively, follow MemPalace's own installation
docs; what matters here is ending up with a `mempalace-mcp` command on your
PATH. Everything below was verified against MemPalace 3.6.

### Choose a transport

Which one you want follows entirely from where the palace sits relative to the
daemon:

| Your situation | Transport | Config |
|---|---|---|
| Palace on the same machine as the repo | **stdio** | `"command": ["mempalace-mcp"]` |
| Palace on another machine | **HTTP** | `"url": "http://host:8765/mcp"` |

**Same machine — the easy case.** There is no server to start and no port to
secure. The daemon launches MemPalace as a child process and talks to it over
its stdin. You just need the command:

```
mempalace-mcp
```

Or, using the container:

```
docker run --rm -i -v mempalace-data:/data ghcr.io/mempalace/mempalace:latest
```

The `-i` is not optional — the protocol needs stdin held open, and without it
the server exits before the handshake finishes.

Write down whichever of those two lines applies to you. The setup wizard asks
for it verbatim in step 3.

**Different machine.** MemPalace serves MCP over HTTP itself:

```bash
mempalace serve --host <private-address> --port 8765
```

Unlike `llama-server`, this one authenticates: a non-loopback bind generates a bearer
token and stores it under `~/.mempalace/server/`. Give that to forge by naming
the environment variable holding it (`"tokenEnv": "MEMPALACE_TOKEN"`), never by
pasting the token into your config file. Details in
[SETUP.md §1.4](SETUP.md#14-mempalace).

Two things worth knowing either way:

- Add `--read-only` if you only want forge to *read* memory. It removes every
  mutating tool from the server entirely, which is stronger than trusting the
  daemon not to call one. Write-back is off by default anyway.
- **The palace database lives on exactly one machine, permanently.** Do not sync
  it, do not commit it, do not keep a second copy.

### About writing to memory

The loop can also record decisions back to memory after a ticket passes review.
This is **off by default**, deliberately: reading is harmless, but writing
mutates a store that every future session reads back as established fact, with
no undo. A memory full of ticket narration is worse than an empty one.

The wizard offers to enable it in dry-run mode, which logs exactly what it
*would* write without writing anything. That is the honest way to find out
whether its judgment matches yours. Say yes if you are curious, watch a few
runs, then flip `memory.dryRun` to `false` in your config if you like what you
see. Full guardrail list in
[SETUP.md §3.2](SETUP.md#32-project-memory).

---

## Part 4 — Install Hybrid Forge

```bash
git clone <this-repo> ~/code/hybrid-forge
pip install -e ~/code/hybrid-forge
forge --help
```

If `forge --help` prints a list of commands, you are installed. The daemon is
stdlib-only Python — there is nothing else to fetch, on purpose, because a
failed `pip install` is a bad way to discover at 2am that an overnight run
never started.

### Optional: the Claude Code plugins

Two of them, and neither wraps the CLI:

- **Forge Setup** — `/forge-setup` walks the cold start, from "is it installed"
  through probing your endpoints to this repo's verify commands.
- **Forge Spec** — `/forge-spec` designs a feature into a document the loop
  executes verbatim, and `/forge-spec-check` dry-runs it through the ingest
  parser before you commit to it.

Running the loop stays in a terminal either way. The CLI works fine without
both.

Each is its own directory under `plugins/`, so load them separately:

```bash
claude --plugin-dir ~/code/hybrid-forge/plugins/forge-setup
claude --plugin-dir ~/code/hybrid-forge/plugins/forge-spec
```

Everything in this guide is done from an ordinary terminal, so you can come
back to this later.

---

## Part 5 — Your first `forge init`, walked through

```bash
cd ~/code/image-marquee
forge init
```

Five questions. **Every endpoint is tested with a real request as you answer
it**, not just pinged — an endpoint that is up but rejects your key, or that
serves a model name with a typo in it, both look fine to a cheaper check and
fail on your first ticket instead.

Nothing is written to disk until the last question. Ctrl-C at any point leaves
your repository untouched.

Below is the whole flow, with what to type and why.

---

### Step 1 of 5 — the executor

```
Hybrid Forge setup — image-marquee

1/5  Executor — the model that writes the code
A local model, served by llama.cpp's router:

  llama-server --models-preset <preset> --models-max 1

The model name below is the router's id for a checkpoint, which is the
section name in the preset. `forge models` writes that preset from this
config once you are through here.

Router URL [http://127.0.0.1:8080/v1]:
Model id (the preset's section name) [qwen3.8]:
Path to its .gguf, if you want `forge models` to write the preset:

probing…
  ok  answered — context 65.5k
```

Values in `[brackets]` are defaults — press Enter to accept. For a router on
this machine the first default is already right; the model id has to match a
section in your preset, and the `.gguf` path is what lets forge write that
preset for you.

For a hosted endpoint, fill in its base URL and model name, and give the **name
of the environment variable** holding your key (`OPENROUTER_API_KEY`), not the
key itself.

`ok answered — context 131.1k` means the model replied and reports a 131,000
token context window. Forge uses that number later to decide whether a ticket
fits before sending it.

If you get `FAIL`, it offers to let you retype. The most common causes are a
missing `/v1` on the end of the router URL, a model id that is not a section
name in the preset the router was started with, or a router that is not running
yet. `curl http://127.0.0.1:8080/v1/models` lists the ids it will accept.

### Step 2 of 5 — the planner and reviewer

```
2/5  Planner & reviewer — the judgment roles
The review step is what keeps a cheap executor honest, so it should not
be the executor. A model reviewing its own diff accepts it.

Who plans and reviews?
  * 1. Claude Code CLI — uses the subscription you already sign into
    2. Anthropic API — needs ANTHROPIC_API_KEY in the environment
    3. Google Gemini — needs GEMINI_API_KEY in the environment
    4. Same model as the executor — no second endpoint, weaker review
choice [1]:

Model (blank = whatever the CLI defaults to) [opus]:

probing…
  ok  answered — context 200.0k
```

The `*` marks the default. Option 1 is chosen for you if the `claude` command
is on your PATH; if it is not, the option says so and the default moves to 2.

The probe here runs an actual `claude -p` call, so it also confirms you are
signed in.

### Step 3 of 5 — project memory

```
3/5  Project memory (optional)
An MCP server holding decisions from past sessions. Without it the
executor sees only what each ticket carries. Blank to skip.

A command runs the server here, as a child process — MemPalace speaks
stdio, so this is the usual answer:  mempalace-mcp
A URL reaches one already running elsewhere:  http://host:8765/mcp

MCP command or URL: mempalace-mcp

probing…
  ok  transport=stdio target=mempalace-mcp room=(unscoped) read=palace_recall write=off available=palace_recall, palace_remember
```

Paste the line you wrote down in Part 3. Press Enter with nothing typed to skip
memory entirely — the wizard says `skipped — the loop will run without project
context` and moves on.

Forge tells the two transports apart by looking for `://`, so you do not have
to say which kind it is.

The probe output is worth reading: `read=palace_recall` is the tool it will use
to retrieve memories, chosen by inspecting what your server actually offers
rather than hardcoding a name that changes between MemPalace versions.
`available=` lists everything it found. If it picked something odd, you can
override it later with `memory.searchTool` in the config.

Then:

```
Let the loop write durable decisions back to memory (starts in dry-run) [y/N]:
```

Default is no. Answering `y` enables it with `dryRun: true` — it logs what it
would record and writes nothing.

### Step 4 of 5 — this repository

```
4/5  This repository

Memory room — scopes retrieval to this project [image-marquee]:
```

The "room" keeps this project's memories separate from every other project's.
The repository folder name is a fine answer.

```
Verify commands. These run before any model reviews, and an empty one
is skipped — which is better than a command that does not work, because
a failing check re-delegates the ticket rather than reporting itself.

Read this repo's CI config and docs to find them [Y/n]:
  reading…
  read: .github/workflows/ci.yml, Makefile, CONTRIBUTING.md
  from .github/workflows/ci.yml

lint [npm run lint]:
typecheck [npm run typecheck]:
test [npm test]:
```

**This is the most important question in the whole setup.** Say yes to the
detection: forge hands your CI workflow, Makefile, and contributing guide to the
planner model and asks what this project actually runs. That is how you get the
command your CI really uses rather than a guess based on which files exist.

It tells you which file each answer came from, and warns you explicitly if it
is not confident. **Check the commands before accepting them.** Run each one by
hand in another terminal and confirm it passes on your current, unmodified
code.

Why this one matters more than the others: a wrong `test` command does not fail
once. Every ticket runs it, fails, gets re-delegated, fails again, and the whole
backlog parks — looking exactly like a broken executor model. A *blank* command
is simply skipped, which is far better than a wrong one.

If detection finds nothing, you get empty fields. That means your repo does not
state its commands anywhere; type them yourself.

```
Paths the executor must never touch, comma-separated.
Auth, migrations, and crypto belong here.
neverDelegate: src/auth/, migrations/
```

Anything listed here stays with a human even if that stalls the backlog. Auth,
database migrations, concurrency, cryptography, and public API surface are the
usual entries. Being generous here costs you very little.

### Step 5 of 5 — review and write

```
5/5  Review

This will write /home/you/code/image-marquee/.hybridforge/config.json:

  {
    "room": "image-marquee",
    "models": {
      "local": {
        "kind": "llamacpp",
        "baseUrl": "http://127.0.0.1:8080/v1",
        "model": "qwen3.8",
        "modelPath": "/models/Qwen3.8-27B-UD-Q4_K_M.gguf"
      },
      "claude": {
        "kind": "claude-cli",
        "model": "opus"
      }
    },
    "roles": {
      "planner": "claude",
      "executor": "local",
      "tester": "local",
      "reviewer": "claude"
    },
    "commands": {
      "lint": "npm run lint",
      "typecheck": "npm run typecheck",
      "test": "npm test"
    },
    "neverDelegate": ["src/auth/", "migrations/"],
    "memory": {
      "command": ["mempalace-mcp"],
      "room": "",
      "arguments": {"wing": "image-marquee"}
    }
  }

Write it [Y/n]:

Wrote /home/you/code/image-marquee/.hybridforge/config.json
Saved these endpoints to /home/you/.config/hybrid-forge/profile.json
  The next repo on this machine starts from them.

Next:
  1. `forge doctor` — re-checks every endpoint.
  2. `forge ingest <spec>` — turn a plan into a reviewable backlog.
  3. `forge go` — run it.
```

Read the preview. This is a plain JSON file you can edit by hand at any time —
nothing here is locked in.

The empty `memory.room` is not a mistake: it inherits the top-level `room`
unless you deliberately override it. `arguments.wing` was filled in from the
same answer, because MemPalace scopes on two axes — a wing (the project) and a
room (the aspect within it) — and forge sets the project one for you.

Note the four **roles**. `tester` went to the local model along with `executor`,
while `planner` and `reviewer` went to Claude. The tester writes tests from the
ticket's acceptance criteria — never its own — because a model that authors both
the code and the test it is judged by will encode its bugs as passing tests.

That last line about the machine profile is why the second repository takes
about ninety seconds: your endpoints are remembered (**not** your credentials —
only the *names* of the environment variables), so next time you press Enter
through steps 1–3 and only genuinely answer step 4.

### If you would rather not answer questions

```bash
forge init --defaults      # writes a config to edit by hand
forge init --force         # overwrite an existing config
```

Run with no terminal attached — piped, redirected, or launched by a script —
`forge init` takes every default and prints what it chose rather than hanging on
input nobody is watching.

---

## Part 6 — Confirm the whole thing

```bash
forge doctor
```

```
project: /home/you/code/image-marquee
roles:   {"planner": "claude", "executor": "local", "tester": "local", "reviewer": "claude"}

  claude: ok name=claude kind=claude-cli model=opus reply='OK'
      context=200.0k max_output=64.0k
  local: ok name=local kind=llamacpp model=qwen3.8 reply='OK'
      context=65.5k max_output=8.2k  resident=qwen3.8
  memory: ok memory transport=stdio target=mempalace-mcp room=image-marquee scope=[wing=image-marquee] read=palace_recall write=off available=palace_recall, palace_remember
  lint command: npm run lint
  typecheck command: npm run typecheck
  test command: npm test

all checks passed
```

Every model gets one real request. Fix anything reporting `FAIL` before going
further — the error names the cause. A missing memory server is reported but not
counted as a failure; a *broken* one is, because silently running without
project history is the exact problem memory exists to solve.

---

## Part 7 — Your first run

### Describe the work

Keep it small. One self-contained feature, not a rewrite.

Write a plain markdown file — a paragraph of what you want and how you will know
it worked:

```bash
forge ingest plan.md
```

```
Run 1: 3 ticket(s) planned from your spec.

  IM-001  Add a --json flag to the export command
      .hybridforge/tickets/IM-001.md
  IM-002  Serialize the export payload
      .hybridforge/tickets/IM-002.md
  IM-003  Cover the flag in the CLI tests
      .hybridforge/tickets/IM-003.md

Review the tickets, then run `forge go`.
```

Inside Claude Code, `/forge-spec add a --json flag to the export command`
designs the same work as a document first — settling the open questions, then
writing tickets in the shape ingest parses verbatim.

Either way, note the line that says **planned from your spec** or **parsed
directly from your plan**. If your document already contains ticket-shaped
sections, they are used verbatim and your acceptance criteria stay in your own
words. Freeform documents go through the planner model, which means the
criteria end up in the model's words.

Getting the parsed path is worth the small effort. The shape is a `## AB-001:
title` header plus `## Spec`, `## Allowed files`, and `## Acceptance criteria`
sections —
[`plugins/forge-spec/templates/spec.md`](../plugins/forge-spec/templates/spec.md)
is a filled-in example. To check a draft before ingesting it, without creating a
run:

```bash
python plugins/forge-spec/scripts/check_spec.py plan.md
```

It reports whether ingest would parse or re-plan the document, which tickets it
found, and the authoring mistakes that otherwise pass silently. Worth knowing
before you write the draft rather than after:

- **Wrap a long criterion, but indent the continuation.** Two spaces, which is
  what every markdown formatter produces, and the lines are read as one. A
  continuation written flush left is dropped, and the criterion reaches the
  tester as its first line only — precise-looking and missing its own point.
  One spec wrapped at 95 columns lost 31 of its 51 criteria that way.
- **Leave the project's own commands out of the criteria.** The harness runs
  lint, typecheck and the suite before anything is judged, so "`npm test` exits
  0" is settled by the run. Written down, it becomes a test that shells out to
  run the command — one backlog got a suite that invoked itself.
- **A heading the parser does not know folds its bullets into the section
  above.** A `## Notes` list between `Allowed files` and `Acceptance criteria`
  becomes allowed files. Put free prose after `Context`.

### Read the backlog before running it

```bash
cat .hybridforge/tickets/*.md
```

Two minutes here is the cheapest quality control available. You are looking for
a ticket that should have been `withheld:<reason>` and got routed to the
executor instead, and for acceptance criteria that do not describe what you
actually wanted. Edit the files directly — that is enormously cheaper than
discovering it three hours in.

### Go

```bash
forge go --open
```

A dashboard opens at `http://127.0.0.1:8799` showing every ticket and its
status, a live event stream, token usage per model, and pause/resume/stop
buttons. From here you are watching, not driving.

From another terminal:

```bash
forge status                  # one-shot summary
forge pause                   # takes effect after the current step, never mid-edit
forge resume
forge stop
```

To run overnight, or with the terminal closed, start it detached — a loop
started inside a Claude Code session dies with that session:

```bash
nohup forge go --no-ui > forge.log 2>&1 &
forge ui --open               # attach the dashboard any time
```

> The dashboard has no login and its stop button ends a run. Leave it bound to
> `127.0.0.1`. If you need it from another machine, tunnel in — do not widen the
> bind address.

---

## Reading what happened

Check the status before assuming something broke:

| Status | Meaning |
|---|---|
| `waiting_budget` | **Not stuck.** A usage window ran out. It wakes itself up and continues; the dashboard shows when |
| `paused` | Someone pressed pause. `forge resume` |
| `blocked` | Nothing left it can do alone — some tickets need you |
| `done` | Backlog finished |
| `stopped` | You stopped it. `forge go` picks it back up |
| `failed` | Terminal for that run |

A blocked ticket carries a note saying what it needed. If that note starts with
`BLOCKED:`, the spec was ambiguous — **edit the ticket, do not re-run it.**
Asking the same question again produces the same answer.

Then read the diff the way you would read a colleague's pull request. The
failure to watch for is not code that fails to compile. It is plausible code
that quietly does the wrong thing.

---

## When something goes wrong

| What you see | What to do |
|---|---|
| `EXECUTOR_UNREACHABLE` | The model endpoint is not answering. `curl <baseUrl>/models` from the daemon's machine, then check the bind address, then the firewall |
| `forge doctor` reports FAIL | The error names the cause — unreachable, bad auth, or a model name that does not exist |
| `forge init` asked nothing | No terminal attached. It took every default and printed them; re-run it in a normal terminal |
| `forge init` filled in wrong endpoints | Remembered answers from a previous setup. Type over them, or delete `~/.config/hybrid-forge/profile.json` |
| `forge init` found no verify commands | Your repo does not state them anywhere. Type them in yourself |
| `memory: FAIL` under stdio | The report carries MemPalace's own stderr — read that first. A wrong subcommand shows up as an immediate exit |
| Memory connects but retrieves nothing | Wrong tool auto-selected, or wrong room. Doctor prints the chosen tool and every available name; set `memory.searchTool` |
| "no run to work on" | Nothing ingested yet — `forge ingest <file>` |
| A ticket fails over and over | Almost always a wrong verify command, not a bad model. Read the failing step on the dashboard and run that command by hand |
| "rejected out-of-scope edits" | Working as designed — the executor tried to touch a file its ticket does not cover |
| Every ticket blocks on context | Tickets too large for the executor's window. Split them |
| Slow first response every ticket | A checkpoint swap on a cold page cache. Warm swaps measured 6-10s; if it is much worse, the weights are being read from disk each time |

---

## Where to go next

- **Tune it.** `.hybridforge/config.json` holds everything — models, roles,
  verify commands, the never-touch list. Edit it directly.
- **Move the model to a GPU box** and keep coding on your laptop:
  [SETUP.md Part 1](SETUP.md#part-1--host-setup-5090-desktop).
- **Turn on memory write-back** once you have watched a few dry runs:
  [SETUP.md §3.2](SETUP.md#32-project-memory).
- **Understand the loop** step by step:
  [ARCHITECTURE.md](ARCHITECTURE.md).

**One safety item worth carrying forward.** Your verify commands run code a
model just wrote, directly on your machine. They are ordinary shell strings, so
you can run them in a container instead:

```json
"commands": {
  "test": "docker run --rm -v \"/abs/path/repo\":/w -w /w python:3.12-slim python -m pytest -q"
}
```

Worth doing on its own merits, and it costs you one line.

**And start narrow.** Run the pipeline on a low-stakes slice first — test
scaffolding, or a self-contained module — before trusting it with anything
load-bearing.
