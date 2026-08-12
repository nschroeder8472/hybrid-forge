# Hybrid Forge

An autonomous plan-and-execute coding loop. You define a feature, say go, and a
daemon runs the backlog to completion — planning, building, verifying, and
reviewing — pausing on its own when a usage window closes and resuming when it
reopens.

The point is not to replace Claude with a local model. It is to spend expensive
reasoning tokens on the parts that need reasoning, and let a cheaper model
absorb the token-heavy bulk generation for the cost of electricity.

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
any of them. Five adapters cover the field:

| kind | reaches |
|---|---|
| `openai` | Ollama, vLLM, LM Studio, llama.cpp server, LiteLLM, OpenRouter, Together, DeepSeek, OpenAI |
| `anthropic` | Anthropic Messages API |
| `gemini` | Google Gemini |
| `command` | any local binary that reads a prompt and writes a completion |
| `claude-cli` | headless `claude -p`, so planning and review run on an existing Claude subscription rather than an API key |

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
| GPU host | Ollama serving the executor model; MemPalace |
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

**Triage is not delegated.** A ticket routed `claude-only` is left for a human
even if that stalls the backlog. Auth, concurrency, migrations, and public API
surface stay with Claude.

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
forge pause | resume | stop # applied after the current step, never mid-patch
forge ui                    # dashboard without running the loop
```

In Claude Code: `/forge-init`, `/forge-plan`, `/forge-ingest`, `/forge-go`,
`/forge-status`.

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
setup — Ollama and which model to run, MemPalace, the daemon, and a narrated
first `forge init`. [docs/SETUP.md](docs/SETUP.md) is the reference behind it:
every option, every alternative, and the full security discussion. The daemon is
stdlib-only Python 3.10+ — a failed `pip install` is a bad way to discover that
an overnight run never started.

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
forge/prompts.py          per-role prompts (a contract the parsers depend on)
forge/wizard.py           interactive `forge init` — asks, probes, never hangs
forge/toolchain.py        reads the repo's CI/docs to find its verify commands
forge/profile.py          machine-level endpoints, reused by the next repo
forge/ui/                 dashboard
mcp_servers/              MCP shim for interactive (non-daemon) delegation
skills/                   triage rules and memory discipline
tests/                    python -m unittest discover tests
```

## Status

Working end to end, and young. Run it on a low-stakes slice first. The failure
mode to watch for is not "the code doesn't compile" — it is plausible code that
quietly does the wrong thing, which is what the review step and the scope
checks exist to catch.
