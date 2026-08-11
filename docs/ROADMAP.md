# Roadmap

Ideas not yet built, with enough of the reasoning to pick them up cold. Nothing
here is committed to. An entry earns its place by naming the problem it solves,
not the feature it adds — a feature whose problem has gone stale should be
deleted rather than implemented.

---

## Bug-report loop

**Status:** next up, design not started.

The loop today turns a *plan* into code. Every ticket describes work that does
not exist yet, and the criteria come from `plan.md`. That shape has a hard edge:
it verifies what the criteria say, so a defect nobody wrote a criterion for
survives the whole pipeline — verification, review, and all.

Two such defects are sitting in the test project right now, both shipped by a
run where all six tickets passed on the first attempt:

- `src/game.rs` — `Game::tick` drains its accumulator with a `while` loop that
  calls `SoftDrop`, and `SoftDrop` locks the piece on collision. A frame gap of
  3000 ms at level 1 therefore locks three pieces in a row. The accumulator is
  never reset on lock.
- `src/game.rs` — rotation has no wall kicks, so a piece against the right wall
  silently refuses to rotate rather than shifting away from it.

Neither is a spec violation. Both are bugs. They are deliberately left unfixed
as fixtures: the first real test of this feature is whether a bug-report loop
can take a plain-language report and land a fix that the existing suite still
passes.

The open questions, roughly in the order they need answering:

- **What is the input?** A prose report ("pieces sometimes drop three at once
  after I switch tabs") is what a human actually has. Turning that into a
  ticket is a different planning job from turning a spec into a backlog — it
  starts from a symptom rather than an outcome, and the file scope is unknown
  at the point the ticket is written.
- **Reproduce before fix.** The current loop's verify step answers "is the tree
  still green". A bug loop needs "does the bug still happen", which means a
  failing test written *first*, and a ticket that cannot be marked done until
  that test goes from red to green. That inverts the tester's contract: today a
  tester that writes a failing assertion has failed the ticket.
- **Scope discovery.** `allowed_files` is authored by the planner from the
  plan. For a bug there is no plan, and the file that needs changing is the
  thing being looked for. Either the planner gets to explore first, or the
  ticket starts wide and narrows.
- **Regression protection.** The reproduction test is the deliverable as much
  as the fix is. It should outlive the ticket under the same one-file-per-ticket
  rule the build loop uses.
- **Relationship to `forge retry --respec`.** Respec already reads failure
  evidence and rewrites a ticket. A bug report is the same shape from a
  different source — worth checking whether one mechanism covers both before
  building a second.

---

## Deferred from the review

Found while reviewing the loop, judged not worth building yet.

- **Retention for the step log.** `forge prune` clears artifact trees; `run.db`
  still grows without bound, and step detail is the bulk of it. Only matters
  once a daemon has been running against a real backlog for weeks.
- **Per-role provider guarantees.** `claude-cli` now defaults to no tools, which
  makes it behave like the completion endpoint the loop assumes. A stronger
  version would let a role *declare* what it needs — "this role reads text and
  returns text" — and refuse a provider that cannot promise it, rather than
  relying on a default.
- **Cross-ticket oscillation detection.** The executor now sees its last two
  failures, which is enough to spot an A-then-B-then-A cycle if it reads them.
  Detecting the cycle mechanically — comparing failure signatures across
  attempts, the way `signatures()` already compares them across tickets — would
  catch it without depending on the model noticing.
- **Reviewer cost.** Review is ~100% of the money on a hybrid run and roughly
  one call per ticket. Skipping review for tickets whose diff is trivial, or
  batching several tickets into one review, would cut that — at the cost of the
  thing that keeps a cheap executor honest. Needs evidence before it is worth
  the risk.
