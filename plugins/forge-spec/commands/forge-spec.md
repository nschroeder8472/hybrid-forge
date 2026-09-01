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
- Where the new code lives, and which files stay as they are.
- What the observable behavior is at the edges: empty input, failure, limits.
- **If this produces something a person starts — an app, a page, a server, a
  CLI — how do they start it, how do they give it something to work on, and
  what do they see?** Ask the user these explicitly. They are the questions
  that get dropped, because a backlog that skips them still passes every check
  it has. One shipped an editor whose file input was hidden with nothing to
  open it: green suite, unusable page. The `spec-contract` skill has the four
  things to name.

Anything left unresolved here comes back later as a `BLOCKED:` ticket, which
costs a round trip and a stalled backlog. Resolve it now, then write it into a
**Design decisions** section — sentences there are protected from being revised
away when a ticket is respec'd on retry. Unmarked prose is not.

Write every one of them as what the code does, not as what it avoids. That
holds for the whole document — spec, context and criteria alike — and the
`spec-contract` skill has the section on why, including the case where the
property really is a prohibition.

## 3. Split into tickets

Each ticket must be independently verifiable and confined to a known set of
files. Order them so each can assume the previous ones landed.

For every ticket, apply the triage rules in the `delegation-protocol` skill and
mark it `delegate` or `withheld:<reason>` — the reason is part of the route,
not a separate line. Check each `delegate` ticket against `neverDelegate`
before writing it down.

Check the other direction too: when one ticket produces something another has
to call, some ticket must be allowed to write the file holding the call, and it
must run after the one being called. A module nothing calls passes every check
there is.

Two tickets may share a file — they get an ordering edge from document order.
What they may not do is assert that file's whole contents; ingest refuses that,
and the `spec-contract` skill says why.

## 4. Write the criteria yourself

Acceptance criteria are assertions that would fail if the behavior were wrong.
Name the input and the expected output, including errors. Wrapping a long one
is fine as long as the continuation is indented; a flush-left continuation is
dropped.

Two things to keep out of them. Say nothing about the project's own commands
exiting 0 — the harness runs them before anything is judged, and a criterion
repeating it gets encoded as a test that shells out to run the command. And pin
the property that would actually be wrong: counts and indices are easy to
assert and easy to satisfy while the behavior is still broken.

This step stays with you and the user. A model that writes both the
implementation and the criteria it is judged against will encode its bugs as
passing tests, so every criterion in the contract is one a person wrote. Where a
criterion is subtle enough that phrasing it wrong would hide a bug, say so in
the ticket's Notes.

## 5. Write the file and check it

Use `${CLAUDE_PLUGIN_ROOT}/templates/spec.md` as the shape. Write to
`docs/specs/<slug>.md` if that directory exists, otherwise `<slug>-spec.md` at
the repo root. Tell the user the path.

Then run `/forge-spec-check <path>`. This is not optional and it is not a
formality — every silent trap in the grammar is one it reports and no human
reads reliably. A spec that skipped it lost 31 of its 51 criteria to bullets
wrapped without an indent, and the run parked without a single attempt; the
checker names each one in a line.

Fix everything it reports, warnings included, and re-run until it prints `OK`.
A warning is not a style note here: each one is a place where what you wrote
and what the executor is told to do have come apart.

## 6. Stop

Present the ticket list with each route and let the user read it. This is the
last cheap moment to catch a ticket routed `delegate` that should have been
withheld.

Then hand off — from their terminal, not from here:

```
forge ingest <path>     # review the backlog it prints
forge go
```

If the backlog produces something runnable, tell them the command that starts
it and what they should see when it comes up. That belongs in the last ticket's
Notes as well, so it survives this conversation.

A loop started inside a Claude Code session dies with the session.
