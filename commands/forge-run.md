---
description: Delegate a ticket to the local executor, then verify and review the result
argument-hint: [ticket id]
---

Run ticket $ARGUMENTS through the pipeline.

1. Read `.hybridforge/tickets/$ARGUMENTS.md`. If it is marked `claude-only`,
   stop and say so — implement it directly instead.
2. Confirm no allowed-scope path matches a `neverDelegate` glob in
   `.hybridforge/config.json`. If one does, stop.
3. Retrieve relevant context from MemPalace for the ticket's subsystem.
4. Call `delegate_implementation` with the spec, allowed files, acceptance
   criteria, and retrieved context.
5. If the response begins with `BLOCKED:`, do not attempt to work around it.
   Report what the executor found ambiguous and propose a spec fix.
6. Apply the returned changes, rejecting any file outside the allowed scope.
7. Call `delegate_tests` with the criteria from the ticket — never criteria the
   executor produced.
8. Run the project's lint, type-check, and test commands. Fix mechanical
   failures by re-delegating with the error output; escalate anything that looks
   like a design problem.
9. Only once verification passes, review the diff against the spec per the
   `delegation-protocol` skill.

Report what passed, what you corrected, and anything you would not merge.
