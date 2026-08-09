---
description: Initialize the current repository for the hybrid pipeline
---

Set up `.hybridforge/` in the current repository.

1. Run `forge init`. If it reports the config already exists, show the current
   configuration and stop.
2. Inspect the repo to infer language, test command, lint command, and
   type-check command. **Confirm these with the user rather than assuming** —
   the loop treats a failing check as a reason to re-delegate, so a wrong
   command means every ticket fails twice and gets parked.
3. Ask which models should play which roles. The four roles are `planner`,
   `executor`, `tester`, `reviewer`, and any declared model can play any of
   them. Explain the tradeoff rather than picking silently: putting a strong
   model on `reviewer` is what keeps the cheap executor honest, and putting the
   executor and reviewer on the *same* model largely defeats the review step.
4. Propose an initial `neverDelegate` list based on what you find — auth code,
   concurrency-heavy modules, migrations, published interfaces. Present it for
   the user to amend.
5. Write the config, then run `forge doctor` and report the result. Every model
   must answer before the loop is worth starting.

Do not write anything to project memory during initialization.
