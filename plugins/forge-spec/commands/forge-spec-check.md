---
description: Dry-run a spec through the ingest parser and report what it would do
argument-hint: [path to a spec markdown file]
---

Check `$ARGUMENTS` against the real ingest parser, without creating a run.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/check_spec.py" "$ARGUMENTS"
```

It imports `forge.ingest`, so what it reports is what ingest will actually do.
It writes nothing — no tickets, no run, no database. Run it as often as the
draft changes.

Report the output and act on it:

**`PLANNED`** — the worst outcome, and it exits 1. The document does not read
as a plan, so ingest would hand it to the planner model and the criteria would
come back in the model's wording. It needs at least one ticket header
(`## AB-001: Add PNG export`) and a `## Spec` heading. Fix the shape and re-run;
this is the one result worth stopping for.

**`Ingest would REFUSE`** — a dependency that does not resolve, or a whole-file
claim about a file two tickets write. Both are fatal at ingest. The message
says what to change; the reasoning is in the `spec-contract` skill.

**Warnings** — not fatal, and each one is a silent behavior change:

- *bullet continued on an unindented line* — that line is dropped, so the
  criterion reaches the tester as its first line only. Indent the continuation
  by two spaces; an indented wrap joins.
- *unrecognized heading* — its bullets fold into the section above, so a
  read-only list can become a writable one. Rename it to a section the parser
  knows, or move it after `Context`.
- *vague criterion* — "handles bad input gracefully" cannot fail, so it cannot
  catch anything. Rewrite it as an assertion naming the input and the expected
  result.
- *retired route spelling* — `claude-only` withholds the ticket but records no
  reason and displays as `withheld:unspecified`. Write `withheld:security`, or
  whichever category applies.
- *lists no test file* — the tester writes into a path the ticket designates,
  and with none it writes outside the ticket's scope, where the executor cannot
  repair what it produced. Add the test file to `Allowed files`; a `bug` ticket
  is exempt, since its reproduction path is granted separately.
- *criterion the harness already settles* — the run executes lint, typecheck,
  the build and the suite before anything is judged. Delete the criterion: the
  tester's job is to turn every one of them into an assertion, and this one
  becomes a test that shells out to run the command it names.

**Ordering edges** — reported because nobody typed them. Two tickets writing
the same file get an order derived from document order. Read them: if the
derived order is backwards, reorder the tickets or declare `**Needs:**`
explicitly.

**This backlog writes an entry point** — listed whenever a ticket creates a file
a person starts. It is a reminder, not a finding: the parser cannot read prose,
so it cannot tell whether the spec says how the thing starts, what control gets
data into it, what the readout says, and what shows before anything is loaded.
Those four are the ones that get dropped, and all four fail silently — one
backlog shipped an editor with a hidden file input and nothing to open it, on a
green suite. Confirm each has a ticket before ingesting.

**Written here, named by no other ticket** — files one ticket creates that
nothing else in the backlog reads, writes, or mentions. Entry points and config
are left out, so what is listed is either a leaf module, which is fine, or a
module something was meant to call where no ticket owns the call site, which
ships as dead code past every check the loop has. Decide which each one is.

Fix what it reports, re-run, and stop when it prints `OK`. Then the handoff is
`forge ingest <path>` from a terminal.
