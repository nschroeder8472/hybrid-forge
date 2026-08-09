---
description: Break a feature into delegable tickets with explicit specs and acceptance criteria
argument-hint: [feature description]
---

Plan the following work: $ARGUMENTS

1. Read `.hybridforge/config.json` and `.hybridforge/conventions.md`.
2. Query MemPalace for prior decisions relevant to this feature, scoped to the
   project's `room`.
3. Break the work into tickets. Each ticket must be independently verifiable and
   confined to a known set of files.
4. For each ticket, apply the triage rules in the `delegation-protocol` skill and
   mark it `delegate` or `claude-only`. State the reason for every `claude-only`
   call — do not leave it implicit.
5. Write each ticket to `.hybridforge/tickets/<id>.md` using `templates/ticket.md`.

Write acceptance criteria as assertions that would fail if the behavior were
wrong. Do not delegate the authoring of criteria.

Stop after writing the tickets. Present the plan and wait for approval before
implementing or delegating anything.
