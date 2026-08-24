---
description: Set up this machine and this repository for the hybrid loop, end to end
argument-hint: [optional: "machine" or "project" to do only that layer]
---

Get the loop ready to run. Two layers, and they are configured in different
places for a reason — see the `workspace-layout` skill before writing anything.

If `$ARGUMENTS` says `machine` or `project`, do only that layer. Otherwise do
both, in order.

## 0. Find out what already exists

Do this before asking the user anything. Setup is resumable, and re-asking
questions that are already answered on disk is how people abandon it.

- `forge --version` (or `python -m forge --version`). Not installed → stop and
  say how to install it: `pipx install hybrid-forge`, or `pip install -e .`
  from a clone. Everything below needs the CLI.
- Machine profile: `%APPDATA%\hybrid-forge\profile.json` on Windows,
  `$XDG_CONFIG_HOME/hybrid-forge/profile.json` or
  `~/.config/hybrid-forge/profile.json` otherwise. `$FORGE_PROFILE` overrides
  both.
- Repo config: `.hybridforge/config.json` in this repository.

Report the three findings in one line each, then continue from the first thing
that is missing. If everything exists, skip to the doctor step and offer to fix
what it reports rather than starting over.

## 1. Machine layer — models, roles, memory

These answers are the same in every repository, so they are asked once and
stored in the profile.

**Ask what the user actually has**, do not assume the sample. The realistic
shapes:

- a local server (Ollama / vLLM / LM Studio / llama.cpp / LiteLLM) at some
  host:port — adapter `openai`, `baseUrl` ending in `/v1`;
- an existing Claude subscription — adapter `claude-cli`, no key, requires
  `claude` on PATH;
- an API key — adapter `anthropic` or `gemini`, keyed by the *name* of an
  environment variable via `apiKeyEnv`.

**Probe every endpoint while the user is still here.** A wrong URL costs one
retyped line now and an entire overnight run later.

- HTTP adapters: `GET {baseUrl}/models` — it also tells you the exact model tag
  the server serves, which is the field people most often get wrong.
- `claude-cli`: check `claude` resolves on PATH.
- key-based adapters: check the named environment variable is set. Never read,
  echo, or write the key itself — only the variable name goes in config.

Then assign the four roles: `planner`, `executor`, `tester`, `reviewer`. Any
declared model can play any of them. Explain the tradeoff rather than picking
silently:

- A strong model on `reviewer` is what keeps a cheap executor honest.
- **Never point `executor` and `reviewer` at the same model.** A model reviewing
  its own diff against a spec it just implemented accepts it, and the review
  step is the only thing standing between a cheap executor and a merged
  mistake.
- `planner` matters less than people expect if specs are authored with
  `/forge-spec` — a parsed plan never reaches the planner.

Memory (MemPalace) is optional. If the user has a host, take the URL; if not,
leave it empty and say retrieval is off — the loop runs fine without it.

If it is configured, set `"write": true` **and** `"dryRun": true`. Retrieval
alone means the loop rediscovers the same project conventions on every ticket
that needs them — one run read memory 262 times, wrote nothing, and relearned
the same three conventions eleven times across two tickets. Dry-run is what
makes write-back safe to have on immediately: it writes nothing and logs what
it would have written. Tell the user to read that log once and then clear
`dryRun`.

Write the profile by running `forge init` in a repository (it persists the
machine-level answers) or by writing `profile.json` directly. Show the JSON
before it lands.

## 2. Project layer — this repository

`forge init` is interactive when it has a terminal. You do not have one, so it
takes its defaults — which is useful rather than a problem: it still reads the
repo's CI config and docs to find the verify commands, and reuses the machine
profile. Your job is to check what it chose and fix what it could not know.

1. Run `forge init`. If it reports a config already exists, show the current
   configuration and skip to step 7 rather than overwriting — but still run
   steps 2 and 3 against it, since an existing config predates these checks.
2. Check `commands` — `lint`, `typecheck`, `test`. They come from a model
   reading this repo's CI config and docs, so verify each against the file it
   claims to come from, and **confirm them with the user**. The loop treats a
   failing check as a reason to re-delegate, so a wrong command means every
   ticket burns `maxAttempts` and parks the backlog — which looks like a model
   problem and is not. Leave a field empty rather than filling it with a
   command you have not confirmed; empty is skipped.

   Two shapes to catch while you are in there. A `test` command that begins
   with the whole `typecheck` command runs the check twice per attempt — drop
   the prefix. And a `*` command is a claim to cover every language in the
   tree; if the repo holds a language that command cannot run, give that
   language its own key rather than letting the catch-all claim it.

   **Fill in `commands.format` if this project has a formatter.** It is worth
   its own moment because it is the cheapest thing in the config: it runs
   before verification, rewrites what the attempt just wrote, and a ticket
   whose only defect is whitespace then costs nothing instead of a full
   attempt. One run spent 117 of a ticket's 160 lint failures on exactly that,
   in a file the tester had written.

   Three rules, and the first is the one that goes wrong. **Give the command
   without a target** — the loop appends the paths: `gdformat`,
   `prettier --write`, `ruff format`, `rustfmt`, `gofmt -w`, `black`,
   `dart format`. A command that ignores its arguments and walks the whole tree
   reformats files no ticket owns, on every attempt. Never a check-only mode
   (`black --check`, `gofmt -l`) — those report, and this one has to rewrite.
   And never a `make fmt` target whose own arguments you cannot read.

   Blank is a supported answer and the right one when the project's formatter
   can only be run over everything.
3. **Check `workspaces`.** Search for build manifests below the root —
   `package.json`, `Cargo.toml`, `go.mod`, `pyproject.toml`, `build.gradle`,
   `pom.xml`. If there is more than one and the config declares no
   `workspaces`, this repository is about to verify as one build, and every
   ticket will pay for every other build's suite on every attempt. It never
   fails, so nothing reports it: one run spent 2.1 of 18 hours running a Godot
   suite against TypeScript tickets, and wrote 229 MB of passing output to
   disk for a single ticket.

   Declare each manifest directory as a workspace with its own commands, or
   re-run `forge init` and answer yes when it offers. The exception is a
   monorepo whose subdirectories genuinely share one toolchain — ask, do not
   assume.
4. **Leave the `loop` block alone unless the user asks.** `ratifyPasses: 2`,
   `executorTurns: 4` and `baselineVerify: true` ship on, and each is paying
   for something a run loses silently without it — see the `workspace-layout`
   skill for what each one buys. If the user wants to cut model calls, say what
   the run gives up rather than trimming quietly. `retryCycles` is the one
   worth discussing: the default hands blocked work back after two cycles, and
   raising it only helps on a backlog you expect to converge slowly.
5. Propose a `neverDelegate` list from what is actually in this repo — auth and
   session handling, migrations, concurrency-heavy modules, published
   interfaces, CI workflow files, crypto, payment paths. One question decides
   every candidate: **would a wrong edit here still come back green?** The
   verify commands and the review step already catch anything that turns red,
   so a path whose breakage fails the build does not belong on this list. What
   belongs is the silent kind — authorization that still compiles and lets the
   wrong caller through, a migration that runs clean and drops a column, a
   workflow edited to stop running the suite at all.

   Do not add a path because it looks infrastructural. Build files, manifests,
   lockfiles, and project config are where a large share of real bugs live, and
   listing one costs more than it reads: `forge bug` routes any report scoped to
   a matching path `claude-only`, so every such bug is filed already parked, for
   a human to implement by hand. The one build-file case worth listing is a file
   that can weaken the verify commands themselves — a test task disabled, a
   source set narrowed — and only when `autoCommit` is on, because a human
   reading the diff before it lands is the same guard by other means.

   Err narrow. A missing entry costs one diff to read; a wrong entry makes a
   file permanently undelegatable and the reason is invisible six weeks later.
   Empty is a legitimate answer for a repo holding nothing sensitive yet.
   Present it as globs for the user to amend — it is a project-specific
   extension of the categories in the `delegation-protocol` skill, not a
   replacement for them.
6. Set `room` if memory is configured. It scopes every retrieval and write for
   this project; an unscoped query pulls decisions from unrelated projects,
   which is worse than no context because it reads as authoritative.
7. Run `forge doctor` and report it per role. Every model must answer before
   the loop is worth starting. A `memory: FAIL` is not fatal but costs every
   convention the executor would otherwise have followed — say so plainly.

   Doctor also prints findings that are not failures and are worth acting on
   before a long run: `undeclared builds`, `no type check`, `owned by no
   workspace`, and `test[...] re-runs the typecheck command`. Report each with
   what it costs, and offer the fix. None of them will ever turn a run red;
   that is exactly why they need reading here.

## 3. Hand off

Say what is configured, and what the next move is:

- `/forge-spec <what you want built>` — author a spec the loop executes
  verbatim.
- `forge ingest <spec.md>` then `forge go` — from the terminal, not from here.
  A loop started inside a Claude Code session dies with the session.

If the user would rather answer the setup questions themselves, tell them to
run `forge init` directly in their terminal — it prompts, probes each endpoint
as they answer, and remembers the machine-level answers for the next repo.

Write nothing to project memory during setup. Nothing durable has happened yet.
