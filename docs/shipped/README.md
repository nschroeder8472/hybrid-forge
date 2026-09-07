# Shipped specs

Backlogs that have been through `forge ingest` and landed. They are kept
unedited, as the record of what was asked for, and each carries a status header
saying which run built it and what the run cost.

They are not documentation of how the code works — `docs/` holds that, and a
spec is a snapshot of an intention that the code has since moved past. What
makes them worth keeping is the gap between the two: a spec beside the commit
that implemented it shows which criteria survived contact with a model, which
were revised by the sign-off pass before any code existed, and which turned out
to be wrong.

A spec is moved here once its backlog has finished. One still being worked on
lives in the repository root, where `forge ingest <file>` finds it.

| Spec | Run | Outcome |
| --- | --- | --- |
| [HANDBACK-DASHBOARD.md](HANDBACK-DASHBOARD.md) | 1, then 2 | Run 1 blocked — the run the read tools were built from, 44 calls and 2.25M tokens with no file written. Run 2 landed both tickets in 2 attempts each. |
| [HANDBACK-WRITES.md](HANDBACK-WRITES.md) | 3 | Done. Five tickets, the dashboard's three write endpoints behind the loopback rule. |
| [HARNESS-TIMEOUT.md](HARNESS-TIMEOUT.md) | 4, then 5 | Done, over two runs. A verify command that hangs now stops the run rather than the daemon. |
| [USAGE-SPLIT.md](USAGE-SPLIT.md) | 6 | Done. Both tickets on the first attempt, no failed step. |
| [TICKET-INSPECTOR.md](TICKET-INSPECTOR.md) | 7 | Done. One ticket first attempt; the other spent a cycle returning a file with elided sections before landing. |

Two of them are worth reading for what went wrong rather than what shipped.
`HANDBACK-DASHBOARD.md` is the backlog whose first run produced no files at all
and led to [CONTEXT-TOOLS.md](../CONTEXT-TOOLS.md). `TICKET-INSPECTOR.md`
carries a criterion of the author's that the sign-off pass corrected, and the
trimmed test path that left its second ticket with no assertions of its own —
the defect [RATIFY.md](../RATIFY.md) now records a rule against.
