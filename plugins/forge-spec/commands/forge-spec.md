---
description: Design a specification the loop can execute verbatim, ticket by ticket
argument-hint: [what you want built, or a path to notes/a PRD to work from]
---

Design a spec for: $ARGUMENTS

The output is a markdown document that `forge ingest` parses **verbatim** — no
planner model rephrases it, and the acceptance criteria you and the user agree
on here are the ones the executor is judged against. Read the `spec-contract`
skill before writing anything; the grammar is exact and the failure modes are
silent.

This is a design conversation, not a transcription. Arrive with a position.

## 1. Ground it in the repo

Before proposing anything:

- Read `.hybridforge/config.json` for `commands`, `neverDelegate`, and `room`.
  No config → the repo is not set up; say so and point at `/forge-setup`.
- Find the code this touches. Name real paths, real function names, real types.
  A spec written against imagined structure produces tickets the executor
  cannot satisfy.
- Retrieve prior decisions relevant to this work from project memory, scoped to
  `room`. Retrieve narrowly — see the `project-memory` skill.

If `$ARGUMENTS` is a path, read it and treat it as the user's intent, not as a
finished spec. Freeform notes are exactly what this command is for.

## 2. Settle the design before splitting it

Put the open questions to the user, with a recommendation on each rather than a
menu. The ones that matter:

- Which library, format, or algorithm — the executor will not choose.
- Where the new code lives, and what it may not touch.
- What the observable behavior is at the edges: empty input, failure, limits.

Anything left unresolved here comes back later as a `BLOCKED:` ticket, which
costs a round trip and a stalled backlog. Resolve it now, then write it into a
**Design decisions** section — sentences there are protected from being revised
away when a ticket is respec'd on retry. Unmarked prose is not.

## 3. Split into tickets

Each ticket must be independently verifiable and confined to a known set of
files. Order them so each can assume the previous ones landed.

For every ticket, apply the triage rules in the `delegation-protocol` skill and
mark it `delegate` or `claude-only`. **State the reason for every `claude-only`
call.** Check each `delegate` ticket against `neverDelegate` before writing it
down.

Two tickets may share a file — they get an ordering edge from document order.
What they may not do is assert that file's whole contents; ingest refuses that,
and the `spec-contract` skill says why.

## 4. Write the criteria yourself

Acceptance criteria are assertions that would fail if the behavior were wrong.
Name the input and the expected output, including errors. One per line — a
bullet that wraps loses everything after its first line.

Never let the user hand this step to a model. A model that writes both the
implementation and the criteria it is judged against will encode its bugs as
passing tests. Where a criterion is subtle enough that phrasing it wrong would
hide a bug, say so in the ticket's Notes.

## 5. Write the file and check it

Use `${CLAUDE_PLUGIN_ROOT}/templates/spec.md` as the shape. Write to
`docs/specs/<slug>.md` if that directory exists, otherwise `<slug>-spec.md` at
the repo root. Tell the user the path.

Then run `/forge-spec-check <path>` and fix whatever it reports. A spec that
does not parse falls through to the planner model, which is the one outcome
this whole command exists to avoid.

## 6. Stop

Present the ticket list with each route and let the user read it. This is the
last cheap moment to catch a ticket routed `delegate` that should have been
`claude-only`.

Then hand off — from their terminal, not from here:

```
forge ingest <path>     # review the backlog it prints
forge go
```

A loop started inside a Claude Code session dies with the session.
