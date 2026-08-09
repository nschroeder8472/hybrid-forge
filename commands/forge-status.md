---
description: Report what the loop is doing right now
---

Run `forge status` and report it plainly.

Read the run status carefully before characterizing it — these mean different
things and only some of them need a human:

- `running` — working. Nothing to do.
- `waiting_budget` — **not stuck.** A usage window is exhausted and the loop is
  parked until it reopens. It will resume on its own. Say when.
- `paused` — someone pressed pause. Resumes on `forge resume`.
- `blocked` — the backlog is exhausted but tickets need a human: an
  underspecified spec the executor refused to guess at, or a ticket routed
  `claude-only`. List them and what each one needs.
- `done` / `failed` / `stopped` — terminal for that run.

For blocked tickets, read the `blocked_note` and say what would unblock it. A
`BLOCKED:` from the executor means the spec was ambiguous — the fix is to edit
the ticket in `.hybridforge/tickets/`, not to re-run and hope.
