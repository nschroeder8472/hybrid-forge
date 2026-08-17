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
(`## AB-001: Add PNG export`) and a `## Spec` heading. Fix the shape; do not
shrug and ingest it anyway.

**`Ingest would REFUSE`** — a dependency that does not resolve, or a whole-file
claim about a file two tickets write. Both are fatal at ingest. The message
says what to change; the reasoning is in the `spec-contract` skill.

**Warnings** — not fatal, and each one is a silent behavior change:

- *wrapped bullet* — the continuation line is dropped. The criterion reaches
  the tester as its first line only. Rejoin it onto one line.
- *unrecognized heading* — its bullets fold into the section above, so a
  read-only list can become a writable one. Rename it to a section the parser
  knows, or move it after `Context`.
- *vague criterion* — "handles bad input gracefully" cannot fail, so it cannot
  catch anything. Rewrite it as an assertion naming the input and the expected
  result.

**Ordering edges** — reported because nobody typed them. Two tickets writing
the same file get an order derived from document order. Read them: if the
derived order is backwards, reorder the tickets or declare `**Needs:**`
explicitly.

Fix what it reports, re-run, and stop when it prints `OK`. Then the handoff is
`forge ingest <path>` from a terminal.
