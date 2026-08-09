---
name: delegation-protocol
description: Use when working in a repository containing a .hybridforge directory, or whenever the user asks to delegate implementation work to the local executor model. Governs how work is split between Claude (planning, triage, review) and the local Qwen executor (implementation, test authoring), what a valid ticket must contain, and which categories of work must never be delegated.
---

# Delegation protocol

Claude is the planner and reviewer. The local executor model writes code against
specs Claude has already fully resolved. The executor is capable but does not
reliably know when it is out of its depth, so **triage is Claude's job and is
never delegated.**

## Triage: decide before writing a ticket

Delegate when all of these hold:

- The approach is settled — no open design questions remain.
- The change is confined to a known set of files.
- Success can be stated as concrete assertions.
- A wrong answer would be caught by tests, types, or review.

Do **not** delegate — implement it directly instead — when the work touches:

- Authentication, authorization, session handling, or secrets.
- Concurrency, locking, async ordering, or shared mutable state.
- Public API surface, published interfaces, or database migrations.
- Cryptography, payment flows, or anything with a compliance dimension.
- Performance-critical paths where the fix depends on profiling judgment.
- Anything where "what should this do?" is still genuinely unresolved.

If a ticket is mostly delegable but contains one risky piece, split it: keep the
risky piece, delegate the rest. Do not delegate a whole ticket because most of it
is safe.

## Ticket contract

A ticket handed to `delegate_implementation` must specify:

1. **Spec** — behavior, function/type signatures, error handling, and the exact
   libraries to use. Name the library; do not let the executor choose.
2. **Allowed files** — explicit paths. Anything outside is out of bounds.
3. **Acceptance criteria** — assertions, not aspirations. "Returns `Err(ParseError)`
   for input missing a closing brace" is a criterion; "handles malformed input
   gracefully" is not.
4. **Context** — relevant prior decisions, retrieved from MemPalace, so the
   executor does not contradict conventions established weeks ago.

Use `templates/ticket.md` as the starting shape.

## Tests

**Claude authors the acceptance criteria; the executor only encodes them.** A
model that writes both the implementation and the criteria it is judged against
will encode its own bugs as passing tests. Never pass the executor's own
suggested criteria into `delegate_tests`.

Where a criterion is subtle enough that phrasing it wrong would hide a bug, write
that test yourself.

## Review

Verification runs before Claude reads anything: lint, type-check, and the test
suite. Claude's review time is for what tooling cannot catch.

Review the diff **against the spec**, not against "the tests pass." Specifically check:

- Every acceptance criterion is actually satisfied, not approximated.
- No files outside the allowed scope were modified.
- Tests assert real behavior rather than restating the implementation.
- No silent scope creep, dropped error handling, or swallowed exceptions.
- Nothing contradicts the conventions in project memory.

If the executor returned a `BLOCKED:` response, treat that as a signal the spec
was underspecified. Fix the spec — do not paper over it in the ticket text.

## After merge

Record durable outcomes to MemPalace: decisions made, conventions established,
and any review correction the executor should not repeat. See the
`project-memory` skill.
