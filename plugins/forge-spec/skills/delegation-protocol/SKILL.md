---
name: delegation-protocol
description: Use when deciding whether a unit of work should be delegated to the executor model or kept for a human, when writing or reviewing tickets in a spec destined for `forge ingest`, or when a run comes back blocked or with rejected out-of-scope edits. Governs triage between the `delegate` and `withheld:<reason>` routes, what a ticket must contain to be executable, and what the human stays responsible for once the loop is running.
---

# Delegation protocol

The spec resolves the design; the executor writes code against a design that is
already resolved. The executor is capable but does not reliably know when it is
out of its depth, so **triage stays with you.**

Triage happens while the spec is being written. Once `forge go` starts, the
loop acts on that routing for hours without asking — the moment before it
starts is the last cheap one.

## Triage

Route a ticket `delegate` when all of these hold:

- The approach is settled — no open design questions remain.
- The change is confined to a known set of files.
- Success can be stated as concrete assertions.
- A wrong answer would be caught by tests, types, or review.

Withhold it — a human implements it — when the work touches one of these, and
write the category into the route itself:

| Route | The work touches |
|---|---|
| `withheld:security` | authentication, authorization, session handling, secrets |
| `withheld:concurrency` | locking, async ordering, shared mutable state |
| `withheld:interface` | public API surface, published interfaces, database migrations |
| `withheld:compliance` | cryptography, payment flows, anything with a compliance dimension |
| `withheld:performance` | a path where the fix depends on profiling judgment |
| `withheld:unresolved` | what this should do is still genuinely open |

The reason rides inside the route because every gate in the loop is written
against the whole value, so it costs no call site and travels everywhere the
route does. `claude-only` still withholds the ticket and always will, but it
records who decided rather than what the objection was, and it displays as
`withheld:unspecified` — which a reader six weeks later cannot act on.

If a ticket is mostly delegable but contains one risky piece, split it: keep the
risky piece and delegate the rest. Route each half on what that half touches —
a whole ticket delegated because most of it is safe, or withheld because one
line is not, is the same mistake in either direction.

`neverDelegate` in `.hybridforge/config.json` is a project-specific extension of
these categories, enforced as globs. It is not a replacement for them — the
list cannot know that the module you are about to touch became
concurrency-sensitive last month.

## Ticket contract

Every ticket needs a spec, an allowed-files list, and acceptance criteria; the
exact markdown the parser expects is in the `spec-contract` skill, and
`${CLAUDE_PLUGIN_ROOT}/templates/spec.md` is the starting shape.

Four rules that decide whether the ticket is executable at all:

1. **Name the libraries.** The executor implements; it does not choose. An
   unresolved choice comes back as `BLOCKED:`.
2. **List every file it must read.** The executor has no filesystem. A file it
   needs for a signature, an export name, or an enum order goes in the
   reference list, or it will guess.
3. **Give a runnable thing a way to be run.** When the backlog produces
   something a person starts, some ticket owns how it starts, some ticket owns
   the control that gets data into it, and some ticket owns what the person
   sees. Left out, all three pass silently: the suite is green and the thing
   cannot be opened. See the `spec-contract` skill.
4. **Say what to do rather than what to avoid.** A prohibition names everything
   except the thing you want and leaves the model to infer the target. This
   matters most in the criteria, where a negative is usually satisfiable by
   doing nothing: "never returns an `x1` past the level width" is true of an
   implementation that returns an empty window every time, and "returns
   `{x0:0,y0:0,x1:7,y1:5}`" is not. The `spec-contract` skill covers the case
   where the property really is a prohibition.

## Tests

**You author the acceptance criteria; the executor's tester only encodes them.**
A model that writes both the implementation and the criteria it is judged
against will encode its own bugs as passing tests. Every criterion in the
contract is one you wrote; a criterion the executor suggests is a suggestion.

Where a criterion is subtle enough that phrasing it wrong would hide a bug,
write that test yourself and say so in the ticket.

## Review

Verification runs before any model reads the diff: lint, type-check, test
suite. Review time is for what tooling cannot catch.

Review the diff **against the spec**, not against "the tests pass":

- Every acceptance criterion is actually satisfied, not approximated.
- No files outside the allowed scope were modified.
- Tests assert real behavior rather than restating the implementation.
- Every change in the diff is one the spec asked for, and every error path it
  touches is still handled or still propagates.
- A file rewritten whole still declares everything it declared before. The
  executor emits whole files, so a manifest, a config or a barrel module comes
  back missing whatever it did not think to copy. The loop now refuses an
  attempt that drops a dependency from a build manifest; the same omission in
  an ordinary file is yours to catch.
- Nothing contradicts the conventions in project memory.

## Once the loop is running

**A `BLOCKED:` ticket is a spec defect, not a transient failure.** The loop does
not retry it and neither should you. Fix the ticket in `.hybridforge/tickets/`,
then re-ingest.

**`waiting_budget` is not a fault.** A usage window is exhausted and the run is
parked until it reopens, and it resumes itself — leave it running. Swapping in a
different model gets around the wait by giving that model the work, so make that
swap only when you want it doing the work.

**Read the rejections.** "rejected out-of-scope edits" in the log means the
executor tried to write a file its ticket did not authorize. The check worked —
but it is also a signal: either the spec was missing a file the work genuinely
needs, or the executor is wandering. Widen the allowed list only when it is the
first.

**Assign different models to `executor` and `reviewer`.** A model reviewing its
own diff against a spec it just implemented will accept it.

## After merge

Record durable outcomes to project memory: decisions made, conventions
established, and any review correction worth carrying into the next ticket. See
the `project-memory` skill.
