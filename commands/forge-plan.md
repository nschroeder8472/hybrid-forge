---
description: Break a feature into delegable tickets with explicit specs and acceptance criteria
argument-hint: [feature description]
---

Plan the following work: $ARGUMENTS

1. Read `.hybridforge/config.json` and any conventions the project records.
2. Retrieve prior decisions relevant to this feature from project memory,
   scoped to the project's `room`.
3. Break the work into tickets. Each must be independently verifiable and
   confined to a known set of files.
4. For each ticket, apply the triage rules in the `delegation-protocol` skill
   and mark it `delegate` or `claude-only`. State the reason for every
   `claude-only` call — do not leave it implicit.
5. Write the plan to a markdown file using the shape in `templates/ticket.md`,
   then hand it to `forge ingest <file>` so the loop picks it up verbatim.

**You author the acceptance criteria.** Write them as assertions that would
fail if the behavior were wrong, and never delegate that authoring — a model
that writes both the implementation and the criteria it is judged against will
encode its bugs as passing tests.

Stop after writing the plan. Present it and wait for approval before starting
the loop.
