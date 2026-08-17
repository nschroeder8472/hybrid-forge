---
name: delegation-protocol
description: Use when deciding whether a unit of work should be delegated to the executor model or kept for a human, when writing or reviewing tickets in a spec destined for `forge ingest`, or when a run comes back blocked or with rejected out-of-scope edits. Governs triage between the `delegate` and `claude-only` routes, what a ticket must contain to be executable, and what the human stays responsible for once the loop is running.
---

# Delegation protocol

The spec resolves the design; the executor writes code against a design that is
already resolved. The executor is capable but does not reliably know when it is
out of its depth, so **triage is never delegated.**

Triage happens while the spec is being written. Once `forge go` starts, the
loop acts on that routing for hours without asking — the moment before it
starts is the last cheap one.

## Triage

Route a ticket `delegate` when all of these hold:

- The approach is settled — no open design questions remain.
- The change is confined to a known set of files.
- Success can be stated as concrete assertions.
- A wrong answer would be caught by tests, types, or review.

Route it `claude-only` — a human implements it — when the work touches:

- Authentication, authorization, session handling, or secrets.
- Concurrency, locking, async ordering, or shared mutable state.
- Public API surface, published interfaces, or database migrations.
- Cryptography, payment flows, or anything with a compliance dimension.
- Performance-critical paths where the fix depends on profiling judgment.
- Anything where "what should this do?" is still genuinely unresolved.

State the reason for every `claude-only` call — do not leave it implicit. A
reader six weeks later cannot reconstruct which category it fell under.

If a ticket is mostly delegable but contains one risky piece, split it: keep the
risky piece, delegate the rest. Do not delegate a whole ticket because most of
it is safe, and do not withhold a whole ticket because one line is not.

`neverDelegate` in `.hybridforge/config.json` is a project-specific extension of
these categories, enforced as globs. It is not a replacement for them — the
list cannot know that the module you are about to touch became
concurrency-sensitive last month.

## Ticket contract

Every ticket needs a spec, an allowed-files list, and acceptance criteria; the
exact markdown the parser expects is in the `spec-contract` skill, and
`${CLAUDE_PLUGIN_ROOT}/templates/spec.md` is the starting shape.

Two rules that decide whether the ticket is executable at all:

1. **Name the libraries.** The executor implements; it does not choose. An
   unresolved choice comes back as `BLOCKED:`.
2. **List every file it must read.** The executor has no filesystem. A file it
   needs for a signature, an export name, or an enum order goes in the
   reference list, or it will guess.

## Tests

**You author the acceptance criteria; the executor's tester only encodes them.**
A model that writes both the implementation and the criteria it is judged
against will encode its own bugs as passing tests. Never let a suggested
criterion from the executor become the contract.

Where a criterion is subtle enough that phrasing it wrong would hide a bug,
write that test yourself and say so in the ticket.

## Review

Verification runs before any model reads the diff: lint, type-check, test
suite. Review time is for what tooling cannot catch.

Review the diff **against the spec**, not against "the tests pass":

- Every acceptance criterion is actually satisfied, not approximated.
- No files outside the allowed scope were modified.
- Tests assert real behavior rather than restating the implementation.
- No silent scope creep, dropped error handling, or swallowed exceptions.
- Nothing contradicts the conventions in project memory.

## Once the loop is running

**A `BLOCKED:` ticket is a spec defect, not a transient failure.** The loop does
not retry it and neither should you. Fix the ticket in `.hybridforge/tickets/`,
then re-ingest.

**`waiting_budget` is not a fault.** A usage window is exhausted and the run is
parked until it reopens. It resumes itself. Do not restart it, and do not swap
in a different model to get around the wait unless you actually want that model
doing the work.

**Read the rejections.** "rejected out-of-scope edits" in the log means the
executor tried to write a file its ticket did not authorize. The check worked —
but it is also a signal: either the spec was missing a file the work genuinely
needs, or the executor is wandering. Widen the allowed list only when it is the
first.

**Never assign the same model to `executor` and `reviewer`.** A model reviewing
its own diff against a spec it just implemented will accept it.

## After merge

Record durable outcomes to project memory: decisions made, conventions
established, and any review correction the executor should not repeat. See the
`project-memory` skill.
