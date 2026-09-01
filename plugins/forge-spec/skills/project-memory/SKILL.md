---
name: project-memory
description: Use when starting work in a repository containing a .hybridforge directory, before writing a delegation ticket, or after a ticket is merged. Covers retrieving established project conventions and decisions from MemPalace and recording new ones, so the local executor does not contradict choices made in earlier sessions.
---

# Project memory

Project history lives in a centrally hosted MemPalace instance, not in the repo.
The repo's `.hybridforge/` directory holds only the pointer and the
human-readable artifacts.

## Reading `.hybridforge/config.json`

```json
{
  "room": "image-marquee",
  "commands": {
    "lint": "cargo clippy --all-targets -- -D warnings",
    "typecheck": "cargo check --all-targets",
    "test": "cargo test"
  },
  "neverDelegate": ["src/auth/**", "src/wasm_bridge.rs"],
  "memory": { "url": "http://localhost:8787/mcp", "room": "image-marquee" }
}
```

`room` scopes every memory query and write for this project. Always pass it —
an unscoped query pulls decisions from unrelated projects, which is worse than
no context at all because it reads as authoritative.

`neverDelegate` is a project-specific extension of the categories in the
`delegation-protocol` skill, not a replacement for them.

## Who retrieves, and when

**The daemon retrieves on its own.** When `memory.url` is set, the loop queries
MemPalace before each ticket and passes the result to both the executor and the
reviewer. An autonomous run populates its own context, and `forge doctor` tells
you whether retrieval is actually working.

**You still retrieve when planning.** A ticket's own `context` field is for
constraints you found while deciding *what* the ticket should be — the daemon's
per-ticket query is topical and will not necessarily surface them. Both reach
the executor; ticket context comes first.

Retrieve narrowly either way. A ticket about the export pipeline does not need
the whole project history; irrelevant context spends executor attention and
invites inconsistency.

If retrieval is unavailable the run continues without it, logging one warning
and giving up after three consecutive failures. That is deliberate — losing an
overnight run to a memory outage would be worse than building without history —
but it does mean a silent `memory: FAIL` in `forge doctor` costs you every
convention the executor would otherwise have followed. Check it after changing
hosts.

## After a ticket merges

**The daemon can record for you.** With `memory.write` on, the loop asks after
each reviewed ticket whether anything durable emerged and writes it. It is
told that the right answer is usually nothing — and it is, for most tickets.
Turn it on with `dryRun` first and read a few runs' worth of proposals before
letting it write for real.

Whether you record by hand or the loop does it, the filter is the same.

Record only what is durable:

- Decisions and their reasoning ("chose tiny-skia over resvg because …").
- Conventions the executor must follow next time.
- Review corrections — what was wrong, and what right looks like.

Hold each candidate to two questions: will it still be true in six months, and
could a future reader recover it from the repository instead? Ticket-by-ticket
narration, transient state, file contents and anything reconstructible from git
history all fail that test — the repo and its history already hold them.

## What belongs in the repo instead

`.hybridforge/` is committed, so it must stay diffable and small:

```
.hybridforge/
├── config.json        # room pointer, verify commands, never-delegate globs
├── tickets/           # ticket markdown, one per unit of work
└── run.db             # gitignored — mutable run state, not an artifact
```

Leave the palace database on its host and commit the pointer to it. The
database is a binary index: it produces meaningless diffs, it conflicts on every
concurrent session, and copying it into the repo defeats the point of having one
authoritative copy.
