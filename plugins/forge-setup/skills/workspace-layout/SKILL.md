---
name: workspace-layout
description: Use when setting up hybrid-forge on a machine or in a repository, when a run behaves as though it is using the wrong model or the wrong build commands, when deciding whether a setting belongs in the machine profile or in .hybridforge/config.json, or when a forge doctor result needs interpreting. Covers the two-layer configuration split, the secrets rule, what is committed, and which misconfigurations fail loudly versus silently.
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

## The one assignment to refuse

`executor` and `reviewer` must not be the same model. A model reviewing its own
diff against a spec it just implemented accepts it, and review is the only
thing keeping a cheap executor honest. If a machine has exactly one model
available, say plainly that review is decorative until a second one exists —
do not quietly configure it anyway.
