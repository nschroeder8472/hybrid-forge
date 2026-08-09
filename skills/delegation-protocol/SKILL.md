---
name: delegation-protocol
description: Use when working in a repository containing a .hybridforge directory, or whenever the user asks to delegate implementation work to an executor model, plan a feature for the autonomous loop, or start/monitor a forge run. Governs how work is split between the planning/review roles and the executor role, what a valid ticket must contain, which categories of work must never be delegated, and what the human is responsible for once the loop is running.
---

# Delegation protocol

The planner and reviewer roles resolve the design; the executor role writes code
against specs that are already fully resolved. The executor is capable but does
not reliably know when it is out of its depth, so **triage is never delegated.**

## Two ways work reaches the executor

**Interactively** — you call `delegate_implementation` yourself, one ticket at a
time, and read each result. Good for a single risky change you want to watch.

**Autonomously** — the daemon (`forge go`) runs the whole backlog: build, apply,
test, verify, review, repeat, until it is done or blocked. This is the normal
path, and it changes what your attention is for. You are not approving each
step; you are getting the plan right beforehand, because the loop will act on it
for hours without asking.

Everything below applies to both. It matters more in the second.

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

## Running the loop unattended

**Get the plan right before saying go.** Once the loop starts it will work
through the backlog for hours without asking permission. The moment before that
is the cheapest possible time to catch a ticket routed `delegate` that should
have been `claude-only` — afterwards you are reviewing something that was
already built.

**Never assign the same model to `executor` and `reviewer`.** A model reviewing
its own diff against a spec it just implemented will accept it. The review step
is what keeps a cheap executor honest, and it only works if something else does
the reviewing.

**A `BLOCKED:` ticket is a spec defect, not a transient failure.** The loop does
not retry it, and neither should you. Fix the ticket in
`.hybridforge/tickets/`, then re-ingest.

**`waiting_budget` is not a fault.** It means a usage window is exhausted and
the run is parked until it reopens. It resumes itself. Do not restart it, and
do not switch it to a different model to get around the wait unless you
actually want that model doing the work.

**Read the rejections.** "rejected out-of-scope edits" in the log means the
executor tried to write a file its ticket did not authorize. That is the check
working, but it is also a signal: either the spec was missing a file the work
genuinely needs, or the executor is wandering. Widen `allowed_files` only when
it is the first.

## After merge

Record durable outcomes to MemPalace: decisions made, conventions established,
and any review correction the executor should not repeat. See the
`project-memory` skill.
