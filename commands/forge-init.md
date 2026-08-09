---
description: Initialize the current repository for the hybrid pipeline
---

Set up `.hybridforge/` in the current repository.

1. Check whether `.hybridforge/` already exists. If it does, report its current
   configuration and stop.
2. Inspect the repo to infer language, test command, and lint command. Confirm
   these with the user rather than assuming.
3. Propose a MemPalace `room` name derived from the repository name.
4. Propose an initial `neverDelegate` list based on what you find — auth code,
   concurrency-heavy modules, migrations, published interfaces. Present it for
   the user to amend.
5. Write `.hybridforge/config.json`, an empty `.hybridforge/tickets/`, and a
   starter `.hybridforge/conventions.md` seeded with conventions you can observe
   in the existing code.
6. Verify connectivity by calling `executor_health`, and report the result.

Do not write anything to MemPalace during initialization.
