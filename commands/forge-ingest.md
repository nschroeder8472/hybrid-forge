---
description: Turn a spec or plan written anywhere into a reviewable backlog
argument-hint: [path to a spec, plan, or PRD]
---

Convert `$ARGUMENTS` into tickets the loop can execute.

The document does not have to come from this tool. A spec from the Claude
desktop app, a plan from a web chat, output from an ordinary Claude Code
session, or a PRD a human wrote by hand are all valid input.

1. Run `forge ingest "$ARGUMENTS"`.
2. Read back **which path it took**, because it matters:
   - `parsed` — the document already contained tickets, and they were used
     verbatim. Nothing was rephrased and the author's acceptance criteria are
     the ones the executor will be judged against.
   - `planned` — the document was freeform, so the planner model converted it.
     The criteria are now the model's wording, and are worth reading closely.
3. Show the ticket list with each route. Flag anything routed `delegate` that
   touches auth, concurrency, migrations, or public API surface — per the
   `delegation-protocol` skill, those belong to a human.
4. Tickets are written to `.hybridforge/tickets/` as markdown. If the user
   wants a ticket changed, edit the file and re-run ingest rather than
   arguing with the planner.

Stop after showing the backlog. Starting the loop is `/forge-go`.
