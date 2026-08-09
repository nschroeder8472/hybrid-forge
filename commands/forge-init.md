---
description: Initialize the current repository for the hybrid pipeline
---

Set up `.hybridforge/` in the current repository.

`forge init` is interactive when it has a terminal. You do not, so it will take
its defaults — which is useful rather than a problem: it still reads the repo's
CI config and docs to find the verify commands, and reuses the endpoints saved
from this machine's last setup. Your job is to check what it chose and fix what
it could not know.

1. Run `forge init`. If it reports the config already exists, show the current
   configuration and stop.
2. Read the config it wrote. It reflects the machine profile
   (`~/.config/hybrid-forge/profile.json`, or `%APPDATA%\hybrid-forge\` on
   Windows) when one exists, and localhost defaults when it does not.
3. Check the detected `commands`. They come from a model reading this repo's CI
   config and docs, so they are usually right — but verify them against the
   source they claim, and fill in anything left blank. **Confirm them with the
   user.** The loop treats a failing check as a reason to re-delegate, so a wrong
   command means every ticket burns `maxAttempts` and parks. Leave a field empty
   rather than putting a command in it you have not confirmed; empty is skipped.
4. Confirm which models play which roles. The four roles are `planner`,
   `executor`, `tester`, `reviewer`, and any declared model can play any of them.
   Explain the tradeoff rather than picking silently: a strong model on
   `reviewer` is what keeps a cheap executor honest, and pointing `executor` and
   `reviewer` at the *same* model largely defeats the review step.
5. Propose a `neverDelegate` list from what you find — auth code,
   concurrency-heavy modules, migrations, published interfaces. Present it for
   the user to amend.
6. Apply the agreed changes to `.hybridforge/config.json`, then run
   `forge doctor` and report the result. Every model must answer before the loop
   is worth starting.

If the user would rather answer the questions themselves, tell them to run
`forge init` directly in their terminal — it prompts, probes each endpoint as
they go, and remembers the endpoints for the next repo.

Do not write anything to project memory during initialization.
