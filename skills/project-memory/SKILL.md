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
  "executorModel": "qwen3.6:35b-a3b",
  "testCommand": "cargo test",
  "lintCommand": "cargo clippy -- -D warnings",
  "neverDelegate": ["src/auth/**", "src/wasm_bridge.rs"]
}
```

`room` scopes every memory query and write for this project. Always pass it —
an unscoped query pulls decisions from unrelated projects, which is worse than
no context at all because it reads as authoritative.

`neverDelegate` is a project-specific extension of the categories in the
`delegation-protocol` skill, not a replacement for them.

## Before writing a ticket

Query memory for anything that constrains the work — established conventions,
prior decisions on the same subsystem, and past review corrections. Pass what
you find into the ticket's `context` field.

Retrieve narrowly. A ticket about the export pipeline does not need the whole
project history; irrelevant context spends executor attention and invites
inconsistency.

## After a ticket merges

Record only what is durable:

- Decisions and their reasoning ("chose tiny-skia over resvg because …").
- Conventions the executor must follow next time.
- Review corrections — what was wrong, and what right looks like.

Do not record: ticket-by-ticket narration, transient state, the contents of
files (the repo already holds those), or anything reconstructible from git
history.

## What belongs in the repo instead

`.hybridforge/` is committed, so it must stay diffable and small:

```
.hybridforge/
├── config.json        # room pointer, commands, never-delegate globs
├── conventions.md     # human-readable, reviewable in PRs
└── tickets/           # ticket markdown, one per delegated unit
```

Never commit the palace database itself. It is a binary index, it produces
meaningless diffs, it conflicts on every concurrent session, and it defeats the
point of having one authoritative copy on the host.
