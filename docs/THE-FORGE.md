# The forge — tools for a smith

Claude is the blacksmith. Every tool here exists to make one of the smith's
motions faster or more precise, and none of them takes a decision away from the
smith. This replaces host mode ([FORGE-AS-A-TOOL.md](FORGE-AS-A-TOOL.md)) as the
direction for forge-as-a-tool; the overnight autopilot (`forge go`) stays a
separate product.

## Why the change

The tic-tac-toe bench ([bench/tictactoe/RESULTS.md](../bench/tictactoe/RESULTS.md))
ran the same task three ways. All three passed 49/49 hidden checks. Opus alone
did it for $0.95 in 3 minutes; Opus hosting forge cost $3.53–$4.13 and 25–56
minutes. The host's own spend was about 3x Opus doing everything, before any
seat was paid, because it spent 101 turns writing contracts, polling jobs,
reading results and re-specifying — 5.6–6.1M input tokens against 0.9M. And the
work most worth delegating (the opponent in one arm, the board in the other)
came back to Opus anyway.

What did help, there and in the hand-steered run before it, was mechanical:
failures attributed against a baseline, exhaustive checks, measured renders, a
finisher, a contact sheet to look at once. So the design keeps those and drops
the ceremony.

## Principles

1. **Judgement never leaves the smith.** No model reviewer, no sign-off vote,
   no respec. A cheap model produces candidates; whether one is kept is decided
   by a test, a measurement, a predicate — or by the smith in one look.
2. **One call, synchronous, compact.** No jobs to poll. A tool answers in
   about a thousand tokens or less, with details by reference (a step id, a
   file path).
3. **Batch, do not loop.** Several candidates at once and a filter, rather than
   attempt, review, revise.
4. **Every tool is optional and says when it pays.** Using none is a valid
   outcome.
5. **Every call is metered**, so the ledger can show which tools earn their
   place.

## The tools

### anvil — what the work is tested against

Runs the project's verify commands and attributes every failure against a
baseline: `introduced` (yours), `already` (was failing before), `environment`
(the command never reached the code). Plus the gates that need no model,
applied to what changed since `HEAD`: relative imports that resolve to
nothing, dependencies a manifest no longer declares.

`anvil(mark=true)` records the baseline. With none recorded, the first call
records one and says so, so a smith that forgot is told rather than misled.

### gauge — calipers

Measures instead of guessing:

- an image: size, transparency (corners, opaque share), the colours of what is
  opaque, its bounding box, and the brightest rows and columns (where drawn
  lines are);
- several images: one contact sheet, labelled, on a checkerboard, so the smith
  looks once;
- the working tree: what changed since `HEAD`, by file.

### mold — casting

The image pipeline as one call. `mold(prompt, n)` renders `n` candidates with
the project's image model, runs the project's formatter on each, measures each
with gauge, drops the ones that fail `require` (measured predicates), and
returns their numbers and one contact sheet. `mold(pick=…, to=…)` puts the
chosen one in place. The smith writes the prompt and makes the choice; the
model only renders.

### Later, and only with evidence

- **quench** — test the tests: run a suite against mutants of the code and
  report which survive.
- **tongs** — try a patch in a throwaway worktree, run anvil there, keep or
  discard.
- **bellows** — a local model fills one bounded artifact N times; only the
  candidates that pass a predicate come back.
- **striker** — a large mechanical transform across files, scope-enforced,
  run inside tongs, accepted only by anvil.
- **ledger** — cost per call and whether its output was kept.

These are the ones that put cheap models to work. They are built when a
benchmark task exists where they can win: one where the bulk of the work is
large, boring and mechanically checkable, and Opus alone fills its own
context doing it.

## How it is measured

Arm D of the bench: Opus with anvil, gauge and mold, same task, same hidden
suite. The prediction for tic-tac-toe is that D costs no more than about 1.3x
Opus alone and its art is better than what Opus drew from code. If D is not
cheaper than the host arms and no better than B, these tools are not earning
their keep either, and the bench says so.

## What carries over

`failures`, `imports`, `manifests` and `evidence` are anvil and gauge's
machinery; the ComfyUI and GIMP integration, the servers and the GPU lease are
mold's. `api.verify` becomes anvil. Contracts, jobs, objections and decisions
stay where autopilot needs them and leave the smith's surface.
