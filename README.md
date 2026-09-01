# Hybrid Forge

An autonomous plan-and-execute coding loop. You define a feature, say go, and a
daemon runs the backlog to completion — planning, building, verifying, and
reviewing — pausing on its own when a usage window closes and resuming when it
reopens.

The point is not to replace Claude with a local model. It is to spend expensive
reasoning tokens on the parts that need reasoning, and let a cheaper model
absorb the token-heavy bulk generation for the cost of electricity.

## Measured throughput

Four runs against one repository, on two local models sharing a single 32 GB
card. Every figure is read from that project's `run.db`.

| Run | Backlog | Tickets | Attempts | Model calls | Tokens | Wall clock |
|---|---|---:|---:|---:|---:|---:|
| 1 | TypeScript port of a Godot level format | 9 | 18 | 169 | 3.00 M | 5 h 32 m |
| 2 | Canvas view for that format | 5 | 10 | 86 | 2.27 M | 2 h 00 m |
| 3 | Defects found reviewing run 2 | 3 | 3 | 54 | 1.13 M | 57 m |
| 4 | The one ticket run 3 could not place | 1 | 1 | 13 | 0.33 M | 15 m |
| | **Total** | **18** | **32** | **322** | **6.73 M** | **8 h 44 m** |

8 of the 18 tickets passed on their first attempt. The longest uninterrupted
run was 5 h 32 m.

Delivered by those runs and present in the repository now:

| | |
|---:|---|
| 34 | TypeScript files |
| 1,236 | lines of source |
| 2,213 | lines of tests |
| 184 | tests passing |
| 4 / 4 | verify commands exiting 0 (`lint`, `typecheck`, `typecheck:browser`, `test`) |

1 of those 34 files was edited by hand, between run 2 and run 3: 5 tests removed
from one file. They were there because run 2's spec put "the test command exits
0" on every ticket — a criterion the harness already settles — and the tester
encoded it the only way a criterion can be encoded, as tests that shell out to
run the commands. Reviewing run 2 found it, the tests were removed, and the rule
is now enforced where it was broken: the tester is told the harness runs those
commands, and `/forge-spec-check` reports the criterion at authoring time. Runs
3 and 4 were specified without it and needed no such edit.

Run 4 is the same shape one step further. Run 3 left one ticket parked because
its spec named no test file, so the tester's output landed outside the ticket's
own scope where the executor could not repair it — and because its criteria
asserted things about a DOM entry point that no test in the project can reach.
Both are now reported before a run starts, by `forge ingest` and by
`/forge-spec-check`. Respecified against what a test can actually assert, the
same work landed in one attempt.

Two earlier runs are excluded from the table. Both stopped for causes since
fixed, so neither describes what the loop does now:
[docs/CANVAS-POSTMORTEM.md](docs/CANVAS-POSTMORTEM.md).

## The loop is not a conversation

The orchestrator is a Python daemon that owns the state machine and reads its
next move from SQLite. No model decides what happens next.

That distinction is the whole design. A loop driven by a model inside a chat
session dies when the context window fills, when the process is killed, or when
a usage limit is hit at 2am. This one survives all three, because none of them
were holding the plan.

```
forged (daemon)
 ├─ state: .hybridforge/run.db
 ├─ RATIFY   every role signs off on the ticket before it is built
 ├─ BUILD    executor writes the implementation against the spec
 ├─ APPLY    edits land on disk; anything outside scope is rejected
 ├─ TESTS    tester encodes the ticket's criteria — never its own
 ├─ VERIFY   lint / typecheck / test, before any model reviews
 ├─ REVIEW   reviewer reads the diff against the spec
 ├─ RECORD   durable outcomes to memory (opt-in, usually nothing)
 └─ COMMIT   optional
loop until DONE | BLOCKED | stopped
```

Diagrams of the loop, the two ways work enters it, and what differs between a
greenfield repo and an existing one: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Bring your own models

Four roles — `planner`, `executor`, `tester`, `reviewer` — and any model can play
any of them. Local models are llama.cpp; cloud is whichever vendor you have:

| kind | reaches |
|---|---|
| `llamacpp` | **the local backend.** `llama-server` in router mode, swapping checkpoints on one endpoint as the loop alternates roles |
| `openai` | OpenAI, and the gateways that speak its wire — OpenRouter, LiteLLM, Together, DeepSeek |
| `anthropic` | Anthropic Messages API |
| `gemini` | Google Gemini |
| `claude-cli` | headless `claude -p`, so planning and review run on an existing Claude subscription rather than an API key |

One local backend is deliberate. Forge used to carry four — Ollama and friends
through `openai`, FreeToken, and a `command` escape hatch — and each had its own
way of being asked what it was serving, its own way of being told to load
something else, and its own silent failure. An Ollama name that never matched
because config omitted `:latest`; an engine that answered to any model id and
echoed it back. Every one of them cost a run before it was found, and carrying
them meant diagnostics could only say what all four had in common.

With one, they can name the thing in front of you: the preset a checkpoint is
spawned from, the `--models-max` slot another model is holding, the reasoning
budget a thinking model is spending its whole answer on, the context window out
of the argv rather than out of a guess. `forge models` writes the preset from
your config, so the numbers in the two files cannot drift apart.

It also means forge can install the backend rather than describe it. `forge llama
install` fetches a pinned llama.cpp build, picks CUDA over Vulkan by reading the
GPU's compute capability, and verifies the download against the SHA-256 GitHub
published for it before unpacking. The same checkpoint measured 16 tok/s on a
Vulkan build and 353 on CUDA, and nothing reports the slow path — see
[docs/LLAMA-PACKAGING.md](docs/LLAMA-PACKAGING.md).

### Two models that work

A pairing that has run this loop end to end, on a single 32 GB card. It is one
machine and one project rather than a benchmark, so take it as a starting point
that is known to work rather than as the answer:

| Role | Checkpoint | Why this one |
|---|---|---|
| `planner`, `reviewer` | **Nemotron-3-Nano-Omni-30B-A3B-Reasoning**, Q4_K_M | An A3B MoE: 30B of weights, ~3B active, so it reads a long ticket and a long diff at around 150 tok/s. Planning and review are the roles that read the most and write the least, which is exactly what a sparse model is cheap at |
| `executor`, `tester` | **Qwen3.8-27B**, UD-Q4_K_M | Dense, ~40 tok/s, and the executor emits whole files — the role where being right per token beats being fast per token. It is the half of the run worth spending the slower model on |

Both at `ctx-size = 131072`, both `exclusive`, `--models-max 1`. They do not
co-reside: about 25 GiB resident each against 32 GB of VRAM, so the router
swaps checkpoints as the loop alternates roles, and that swap is why
`loop.ratifyOrder` is worth grouping by model — see `/forge-setup`.

One run, for scale: five tickets, all landing, 120 minutes, 86 model calls and
2.27M tokens — 1.10M in and 415K out through the executor pair, 545K in and
210K out through the planner pair. All of it local, on electricity.

**Set `reasoningBudget` on both, before the first run.** Neither of these is
usable without it. Unbounded, the Nemotron spent an entire 32,768-token output
budget on hidden reasoning and never began its answer — forge notices, throws
the call away and retries with thinking off, at a cost of roughly 93 seconds of
wasted generation per planner and reviewer call. 8,192 for the Nemotron and
6,144 for the Qwen leave both room to answer. The budget is one number per
checkpoint, so size it against the *smaller* of the two output budgets the
roles sharing it are given.

Two smaller things that cost a run each if missed. Nemotron's "Omni" is
multimodal, and a preset generated from `--models-dir` loads the vision
projector beside a text-only checkpoint, spending VRAM no role here uses —
`no-mmproj = true`. And the Qwen returns its `<think>` block inline in
`content` rather than in `reasoning_content` depending on the chat template;
forge strips it at the provider boundary, which is why it is worth knowing that
a reply looking truncated in the logs may have been trimmed there.

Adding a backend is one module and one registry line. Nothing in the loop, the
budget gate, or the dashboard knows which kind it is talking to.

## Waiting is a feature

Subscription plans enforce a rolling usage window, and when it is exhausted the
Claude CLI does not return a `429` with a header — it prints a sentence. The
budget gate parses that into a reset time and parks the run in
`waiting_budget`, which is a live state, not a failure: the dashboard shows when
the window reopens and the loop wakes itself up.

The same gate checks context windows before a call rather than after, so a
prompt that will not fit is trimmed or the ticket is flagged for splitting —
instead of the backend silently truncating an implementation.

## Project memory reaches the executor

The daemon retrieves prior decisions from MemPalace before each ticket and
passes them to both the executor and the reviewer, so a local model does not
relitigate a convention settled weeks ago. It speaks MCP directly — no Claude
Code in the path — and discovers the server's tool surface at connect time
rather than hardcoding names that shift between MemPalace versions.

Retrieved context is the *droppable* part of a prompt: when a ticket does not
fit the executor's context window, memory yields and the spec stays. A memory
outage degrades the run to "no context", never ends it.

Write-back is the other direction, and it is opt-in. After a ticket passes
review the loop can ask whether the work settled a decision or produced a
correction worth keeping, and record it. Off by default, because retrieval only
reads while recording mutates a store every future session reads back with no
undo — and because a memory full of ticket narration is worse than an empty
one. `dryRun` logs what it would write without writing it. Entries that match a
credential shape are refused before any network call, and destructive tools are
never selected.

## Where each piece runs

**The daemon goes where the project goes.** It writes the executor's files, runs
your lint and test commands, and builds the diff the reviewer reads — all
against a local working tree. It is not a compute service you can point at a
repo across the network.

So the split is by weight, not by role:

| Machine | Runs |
|---|---|
| GPU host | `llama-server` in router mode, serving the local models; MemPalace |
| Your workstation | the repo, the daemon, Claude Code, the toolchain |

Model calls and memory reads cross the network; files, git, and builds stay
local. Any network the two machines share will do — the only requirement is
that the daemon can reach the model and memory endpoints, and that those
endpoints are not reachable by anything else, since neither has authentication
of its own. This is also the only workable shape for a macOS-only
toolchain — `xcodebuild` and the simulators cannot run in a Linux container, so
verification has to happen on the Mac.

The daemon is stdlib-only Python, so "install it next to the project" is
`pip install -e .`, not a deployment.

**Containerizing is for the verify step, not the daemon.** `commands` are
ordinary shell strings, so isolating model-authored code from your host needs
no special support:

```json
"commands": {
  "test": "docker run --rm -v \"/abs/path/repo\":/w -w /w python:3.12-slim python -m pytest -q"
}
```

That is worth doing on its own merits — today those commands run directly on
your machine, and they are running code a model just wrote.

## Start from a plan written anywhere

The loop's input does not have to come from this tool:

```bash
forge ingest plan.md      # a spec from the Claude app, a web chat, or a human
forge go
```

If the document already contains ticket-shaped sections they are used verbatim —
no model re-reads them, and the acceptance criteria stay the ones their author
wrote. Only freeform documents go through the planner.

## What it deliberately does not do

**The executor never decides scope.** It gets an explicit spec, an allowed file
list, and acceptance criteria. Every edit is checked against that list before it
touches disk, and paths that escape the project root are refused outright.

**The executor never authors its own acceptance criteria.** A model that writes
both the implementation and the test it is judged against will encode its bugs
as passing tests.

**Triage is not delegated.** A ticket routed `withheld:<reason>` is left for a
human even if that stalls the backlog. The reason travels in the route —
`withheld:security`, `withheld:concurrency`, `withheld:interface` — so a ticket
parked in March still says in May what it was parked for. Auth, concurrency,
migrations, and public API surface stay with a person.

**A bug is never fixed on faith.** `forge bug` writes a test that asserts the
correct behavior and requires it to *fail* before any fix is attempted. A fault
that cannot be demonstrated parks for a human rather than being fixed against a
guess — see [docs/BUG-LOOP.md](docs/BUG-LOOP.md).

**A `BLOCKED:` never retries.** An underspecified spec does not improve by being
asked again — the run parks the ticket and says what was ambiguous.

## Monitoring

`forge go` serves a dashboard on `127.0.0.1:8799`: backlog with per-ticket
status, live event stream, recent steps, per-model token usage, and
pause/resume/stop. It reads the same SQLite the loop writes, so a crashed
dashboard cannot take a run with it and a restarted one reattaches with no
handshake.

It has no authentication and its stop button ends a run, so it binds to
loopback and warns on startup if you point `ui.host` anywhere else. Tunnel in
rather than widening the bind address, or put something that authenticates in
front of it.

## Commands

```bash
forge init [--defaults]     # set up .hybridforge/ for this repo, with prompts
forge doctor                # probe every configured model
forge ingest <file|->       # spec or plan -> reviewable backlog
forge go [--plan f] [--open]# run until done or stopped
forge go --retries N        # requeue and respec what did not land, N more
                            # times; -1 = until clean or stopped
forge status                # one-shot summary
forge retry [--respec]      # requeue failed tickets, optionally re-specced
forge bug "<report>"        # reproduce a bug, then fix it
forge toolchain             # what tests each language; set up what nothing does
forge criteria [ID --accept N]
                            # adopt a criterion the loop proposed and refused
forge advise <ID> "<note>"  # a note the planner and executor read next pass
forge release <ID> "<why>"  # hand a withheld ticket back to the executor
forge discharge <ID>        # mark a withheld ticket done — you wrote the code
forge models                # write the llama.cpp preset from your config
forge llama [status|install|list]
                            # fetch or inspect the pinned llama.cpp build
forge replay                # re-read a past run's output with today's parsers
forge prune                 # delete the artifact trees of old runs
forge pause | resume | stop # applied after the current step, never mid-patch
forge ui                    # dashboard without running the loop
forge ui --host IP --port N # bind it elsewhere, this run only (no auth!)
```

`advise`, `release` and `discharge` are the return channel: a run that parks a
ticket can be answered without restarting it, and a ticket you implemented by
hand can be closed without pretending the loop did it. See
[docs/HANDBACK.md](docs/HANDBACK.md).

Two Claude Code plugins sit beside the CLI rather than wrapping it. **Forge
Setup** (`/forge-setup`) handles the cold start — install check, endpoint
probes, the machine profile, and this repo's verify commands and never-delegate
list. **Forge Spec** (`/forge-spec`, `/forge-spec-check`) is where a feature
gets designed into a document `forge ingest` parses verbatim, so the acceptance
criteria stay in the words a human wrote. Running the loop stays in the
terminal, where it survives the session.

## Setup asks once

`forge init` prompts for the endpoints, **probes each one while you are still
sitting there**, and writes what it learned to a machine-level profile
(`~/.config/hybrid-forge/profile.json`, `%APPDATA%\hybrid-forge\` on Windows).
The next repo starts from those answers, so the second setup is Enter-through
except for the things that repo actually decides.

A wrong endpoint found now costs one retyped line. The same wrong endpoint found
by `forge go` costs the run.

For the verify commands it does not guess at all. It collects the repo's own CI
workflow, Makefile, and contributing guide, hands them to the planner model, and
asks what this project actually runs — so you get `cargo nextest run --workspace`
because that is what CI runs, not `cargo test` because a `Cargo.toml` exists.
Nothing found means an empty field, which the loop skips. A wrong `test` command
does not fail once; it fails `maxAttempts` times per ticket and parks the whole
backlog, looking exactly like a bad executor model.

Credentials are never stored. Providers resolve keys through `apiKeyEnv`, the
*name* of an environment variable, and that name is what the profile keeps.

```bash
forge init              # prompts, probes, remembers
forge init --defaults   # no questions; writes a config to edit by hand
```

With no terminal attached — piped, redirected, or run from a script — it takes
every default and says so rather than blocking on stdin nobody is watching.

First time through, [docs/QUICKSTART.md](docs/QUICKSTART.md) walks the whole
setup — llama.cpp and which models to run, MemPalace, the daemon, and a narrated
first `forge init`. [docs/SETUP.md](docs/SETUP.md) is the reference behind it:
every option, every alternative, and the full security discussion. The daemon is
stdlib-only Python 3.10+ — a failed `pip install` is a bad way to discover that
an overnight run never started.

[docs/BUG-LOOP.md](docs/BUG-LOOP.md) covers `forge bug` — the loop that has to
reproduce a fault before it is allowed to fix it, and what it refuses to do when
it cannot.

[docs/CANVAS-POSTMORTEM.md](docs/CANVAS-POSTMORTEM.md) is the shortest way to
see what this loop's failures actually look like: a backlog that parked without
writing a line because the parser dropped three fifths of its criteria, and the
run after it that went green while deleting the project's dependencies.

[docs/LOOP-INVARIANTS.md](docs/LOOP-INVARIANTS.md) is the one to read before
adding a step, a role, or any check that attributes blame. Eighteen rules that
hold across the whole harness — read scope versus write scope, why an anchor the loop
wrote is not an anchor, why attribution must come from diagnostic blocks and
never from raw output. Each was learned by breaking it.

[docs/CONFIG.md](docs/CONFIG.md) is the key-by-key reference for
`.hybridforge/config.json`, with a populated example at
[templates/config.sample.json](templates/config.sample.json) to copy from.

[docs/ROADMAP.md](docs/ROADMAP.md) holds what is not built yet and why — the
bug-report loop first among it.

## Layout

```
forge/providers/          adapter layer — one module per backend
forge/loop.py             the state machine
forge/budget.py           context accounting + rate-limit gate
forge/state.py            SQLite: runs, tickets, steps, events, usage, control
forge/memory.py           MCP client for project memory (read + guarded write)
forge/secrets.py          credential detection for anything about to be persisted
forge/ingest.py           outside spec/plan -> backlog
forge/patch.py            model output -> file writes, with scope enforcement
forge/manifests.py        what a build manifest declared before a rewrite
forge/prompts.py          per-role prompts (a contract the parsers depend on)
forge/ratify.py           the pre-build sign-off pass
forge/respec.py           revising a ticket from why it failed
forge/routes.py           delegate vs withheld:<reason>, and what withheld means
forge/evidence.py         which files a ticket may read
forge/llama.py            fetching and verifying the pinned llama.cpp build
forge/presets.py          config -> the llama.cpp router preset
forge/wizard.py           interactive `forge init` — asks, probes, never hangs
forge/toolchain.py        reads the repo's CI/docs to find its verify commands
forge/profile.py          machine-level endpoints, reused by the next repo
forge/ui/                 dashboard
plugins/forge-setup/      Claude Code plugin: machine + repository setup
plugins/forge-spec/       Claude Code plugin: spec authoring, triage, memory
examples/sample-project/  the fixture a loop change is run against
scripts/sample_workspace.py  copies that fixture somewhere a run may write
tests/                    python -m unittest discover tests
```

## Changing the loop

Unit tests say a change is what you meant. They do not say what it does to a
run, and most of what this project knows came from watching a real backlog
fail. `examples/sample-project` is the cheapest imitation of that: two builds,
a three-ticket spec on the parsed path, one dependency between tickets, a green
baseline, and a bug the suite does not catch.

```
python scripts/sample_workspace.py     # copy it somewhere a run may write
cd <the path it prints>
forge --root . doctor                  # the coverage matrix, no tokens spent
forge ingest SPEC.md                   # three tickets, parsed
forge go
```

Run it against a copy, never in place — a run writes code, a database and an
artifact tree, and the committed tree is a fixture. `.gitignore` holds the
fixture as an allow-list so anything a run leaves behind is ignored rather than
staged, and `tests/test_sample_project.py` pins what a run depends on: both
suites green, the spec parsed rather than replanned, every path it names owned
by a build, every ticket carrying its own test file, the four paths the spec
must find missing, and the seeded defect still a defect. Those run with the
ordinary suite and spend nothing.

## Status

Working end to end, and young. Run it on a low-stakes slice first. The failure
mode to watch for is not "the code doesn't compile" — it is plausible code that
quietly does the wrong thing, which is what the review step and the scope
checks exist to catch.
