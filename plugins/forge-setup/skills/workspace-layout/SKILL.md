---
name: workspace-layout
description: Use when setting up hybrid-forge on a machine or in a repository, when a run behaves as though it is using the wrong model or the wrong build commands, when deciding whether a setting belongs in the machine profile or in .hybridforge/config.json, or when a forge doctor result needs interpreting. Covers the two-layer configuration split, the secrets rule, what is committed, one build per workspace, the loop defaults that keep a long run converging, and which misconfigurations fail loudly versus silently.
---

# Workspace layout

Configuration lives in two places, and which one a setting belongs in is not a
style question — putting a repo answer in the machine layer breaks the next
repository, quietly.

## The split

**Machine profile** — answers that are the same everywhere you work.

    $FORGE_PROFILE                       explicit override, wins everywhere
    %APPDATA%\hybrid-forge\profile.json  Windows
    $XDG_CONFIG_HOME/hybrid-forge/…      POSIX, when set
    ~/.config/hybrid-forge/profile.json  POSIX default

Holds `models`, `roles`, `memory`, and the dashboard port. Your executor lives
at one address and your memory server at another; you decide once who plays
which role, and every later repo starts from that.

**Repo config** — `.hybridforge/config.json`, committed.

Holds what only this repository can answer: `commands` (lint / typecheck /
test), `room`, `neverDelegate`, and the `loop` settings. A `cargo test` carried
into a Python repo does not fail loudly — it fails `maxAttempts` times per
ticket and parks the whole backlog, which reads as a model problem and is not.

When both exist, the repo config wins for the keys it declares. A repo may
override a role or a model when it genuinely needs a different one; it should
not restate the machine's endpoints just to have them written down.

## Secrets

**No credential is ever written to either file.** Providers resolve keys
through `apiKeyEnv` — the *name* of an environment variable. The profile
strips an inline `apiKey` on the way in rather than copying it to a second
location the user does not know exists.

So during setup: check the variable is *set*, never read its value, never echo
it, never put it in a config, a log, or a commit.

## What is committed

```
.hybridforge/
├── config.json        # room pointer, verify commands, never-delegate globs
├── tickets/           # ticket markdown, one per unit of work
└── run.db             # gitignored — mutable run state, not an artifact
```

`config.json` and `tickets/` are reviewable text and belong in PRs. `run.db` is
a mutable log with meaningless diffs and guaranteed conflicts; it is already
ignored. Never commit a MemPalace store either — the repo holds the pointer,
not the index.

## One build per workspace

A repository with more than one build manifest is more than one build, and
`workspaces` is how the config says so. Leaving it out is the single most
expensive silent misconfiguration there is, because nothing about it looks
wrong:

- every `*` command runs on every ticket, whatever that ticket writes;
- a ticket that touches only TypeScript still pays for the Godot suite, the
  Gradle suite, whatever the root command happens to be;
- and it passes, every time, so nothing is ever reported.

Measured on one run: 908 runs of an 8-second Godot suite that no ticket could
have affected — 2.1 hours of an 18-hour run, and 229 MB of passing output
saved as artifacts for a single ticket.

`forge doctor` names undeclared builds. `forge init` offers to configure them
separately whenever it finds two. Say yes unless the subdirectories genuinely
share one toolchain.

The related check doctor prints: a `test` command that *starts with* the whole
`typecheck` command runs the type check twice per attempt. Drop the prefix, or
clear the `typecheck` entry if the test command really is the check.

## The loop defaults that keep a long run converging

These live in the repo config's `loop` block and ship on. They exist because a
run can be long without being wrong, and the failure they address is a run that
is long *and* learning nothing. Do not turn them off to save calls without
saying what the run gives up.

- **`ratifyPasses: 2`** — every role is asked whether it can do its part of a
  ticket as written, before anything is built. Costs `roles x passes` calls per
  ticket. What it catches is the ticket nobody can build: a spec whose own
  criteria contradict it, a criterion demanding a number the fixture cannot
  produce. Two such tickets cost one run 650 attempts and about 16 hours.
- **`executorTurns: 4`** — prior attempts are replayed to the executor as real
  conversation, its own reply as an `assistant` message. Without it the
  executor is shown the files as disk state with nothing saying it wrote them,
  and reads its own work as somebody else's.
- **`memory.write: true` with `dryRun: true`** — the loop records durable
  conclusions instead of only reading them. Dry-run writes nothing and logs
  what it would have written, which is what makes it safe to have on from the
  first run. Clear `dryRun` once the log looks right.
- **`loop.toolchainContext: true`** — the executor and tester are shown the
  `tsconfig.json`, `gdlintrc`, `eslint.config.*` and friends that the verify
  commands enforce, resolved per language from the ticket's own scope. They are
  graded by those settings and were otherwise never shown them, so they
  inferred them from failures: 512 attempts against one compiler flag.

And one that is not in `loop` at all but belongs to the same argument:

- **`commands.format`** — a formatter, given *without* a target because the
  loop appends the files it just wrote. It runs before verification, its own
  failure never parks a ticket, and it turns a whitespace-only lint failure
  from a spent attempt into nothing at all. Empty by default; no formatter is
  ever inferred.

`retryCycles: -1` is the one to leave alone unless asked. Unbounded retries are
only safe when something detects that a cycle learned nothing, and until that
lands the cycle count is the brake.

## Reading `forge doctor`

`doctor` probes every configured model and the memory endpoint. What each
failure actually costs:

- **A role fails.** That role cannot run. `executor` or `tester` failing stops
  the loop at the first ticket; `reviewer` failing means nothing checks the
  executor's work; `planner` failing only matters for freeform input, since a
  parsed spec never reaches it.
- **`memory: FAIL`.** Not fatal. Retrieval is skipped after three consecutive
  failures and the run continues — losing an overnight run to a memory outage
  would be worse. But it silently costs every convention the executor would
  otherwise have followed, so it is worth fixing before a long run rather than
  after.
- **Everything passes but the run still stalls.** Suspect `commands` before
  suspecting the models. A verify command that fails for reasons unrelated to
  the ticket re-delegates work that was already correct.
- **`undeclared builds: ...`.** See *One build per workspace* above. Not fatal,
  and the most expensive line doctor prints.
- **`test[...] re-runs the typecheck command`.** The type check runs twice per
  attempt. Cheap in seconds, and a sign the two command kinds were filled in
  independently and one of them is not what you think it is.

## The one assignment to refuse

`executor` and `reviewer` must not be the same model. A model reviewing its own
diff against a spec it just implemented accepts it, and review is the only
thing keeping a cheap executor honest. If a machine has exactly one model
available, say plainly that review is decorative until a second one exists —
do not quietly configure it anyway.
