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

Write the profile by running `forge init` in a repository (it persists the
machine-level answers) or by writing `profile.json` directly. Show the JSON
before it lands.

## 2. Project layer — this repository

`forge init` is interactive when it has a terminal. You do not have one, so it
takes its defaults — which is useful rather than a problem: it still reads the
repo's CI config and docs to find the verify commands, and reuses the machine
profile. Your job is to check what it chose and fix what it could not know.

1. Run `forge init`. If it reports a config already exists, show the current
   configuration and skip to step 5 rather than overwriting.
2. Check `commands` — `lint`, `typecheck`, `test`. They come from a model
   reading this repo's CI config and docs, so verify each against the file it
   claims to come from, and **confirm them with the user**. The loop treats a
   failing check as a reason to re-delegate, so a wrong command means every
   ticket burns `maxAttempts` and parks the backlog — which looks like a model
   problem and is not. Leave a field empty rather than filling it with a
   command you have not confirmed; empty is skipped.
3. Propose a `neverDelegate` list from what is actually in this repo — auth and
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
4. Set `room` if memory is configured. It scopes every retrieval and write for
   this project; an unscoped query pulls decisions from unrelated projects,
   which is worse than no context because it reads as authoritative.
5. Run `forge doctor` and report it per role. Every model must answer before
   the loop is worth starting. A `memory: FAIL` is not fatal but costs every
   convention the executor would otherwise have followed — say so plainly.

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
