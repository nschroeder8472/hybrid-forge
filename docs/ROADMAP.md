# Roadmap

Ideas not yet built, with enough of the reasoning to pick them up cold. Nothing
here is committed to. An entry earns its place by naming the problem it solves,
not the feature it adds — a feature whose problem has gone stale should be
deleted rather than implemented.

---

## Convergence — specified, not built

**Status:** ten features specified in [CONVERGENCE.md](CONVERGENCE.md), derived
from the `Puzzle-Path` run of 2026-08-22/23.

The problem that document names: a run can be long without being wrong, and
this one was neither converging nor able to tell. 18.2 hours, 24.5M tokens, 430
attempts on one ticket, and the distinct-error-kinds-per-failure curve is flat
across every decile of every stuck ticket. The loop had no measure of progress,
its only durable per-ticket learning slot was rebuilt from the plan every cycle,
and the models were being graded against linter and compiler configuration they
were never shown.

The stall-detection note under *Deferred from the review* below is the same
problem seen from the image loop, and Feature 7 is its general answer.

---

## Bug-report loop — built

**Status:** shipped as `forge bug`. See [BUG-LOOP.md](BUG-LOOP.md) for how it
works and what it refuses to do; this entry keeps only what is still open.

The five questions this entry was written around were answered as follows.

- **Input** — a prose report, through `forge bug "<report>"`, `--file`, or
  stdin. Separate from `ingest`: that turns a document into a backlog and takes
  its criteria as the contract, while a report is one symptom whose contract
  does not exist yet.
- **Reproduce before fix** — a `REPRODUCE` step ahead of `BUILD`. The tester
  writes a test asserting the correct behavior, the test command runs it, and it
  must fail. The tester's contract really is inverted for this ticket kind, and
  it is inverted explicitly: a separate prompt, a separate system message, and a
  step whose `ok` means the suite went red.
- **Scope discovery** — the harness gathers `git ls-files` and grep hits for the
  report's own specific words, and the planner names the files from that. The
  planner does not explore, and the ticket does not start wide.
- **Regression protection** — the reproduction stays. It is the one file
  `_discard_tests` does not reclaim, because it is the only assertion in the
  loop demonstrated against real behavior.
- **Relationship to respec** — they did not collapse into one mechanism. Respec
  rewrites a ticket that already exists from evidence the loop produced; a bug
  report arrives before any ticket, and its evidence has to be manufactured by
  running something. What they share is the failure-to-revision shape, not the
  code.

**Still open: it has never been run.** Every part of it is covered by tests and
none of it has met a real model, a real repository, or the two defects below.

- `src/game.rs` — `Game::tick` drains its accumulator with a `while` loop that
  calls `SoftDrop`, and `SoftDrop` locks the piece on collision. A frame gap of
  3000 ms at level 1 therefore locks three pieces in a row. The accumulator is
  never reset on lock.
- `src/game.rs` — rotation has no wall kicks, so a piece against the right wall
  silently refuses to rotate rather than shifting away from it.

Both are still deliberately unfixed. Neither is a spec violation, both shipped
from a run where all six tickets passed, and they are the honest first test:
a plain-language report, a fix, and the existing suite still passing afterwards.

---

## Per-language verify commands

**Status:** designed, not built — [LANGUAGE-COVERAGE.md](LANGUAGE-COVERAGE.md).

`commands.test` is one string, which assumes a repository is one language.
Everything downstream inherits it: which language the tester writes in, what
verification proves, and whether a bug can be reproduced at all. Three observed
failures share that root — a ticket that shipped green over JavaScript the suite
never ran, a bug report whose fault lived in that same unrun layer, and one
stray `.js` file that once disabled test authoring for a whole Rust backlog.

The spec turns each command into a map from language to command, blocks a ticket
whose language has no runner rather than letting it pass on review alone, and
adds `forge toolchain` to set one up. Five phases, each landing on its own.

---

## Image generation as a ticket kind

**Status:** designed, not built — [IMAGE-LOOP.md](IMAGE-LOOP.md).

The loop's steps are named after code, but only two of them are about code. The
rest is a shape: produce an artifact under a scope, check it mechanically, have
something that gains nothing from passing rule on it, refine until a criterion
is met. Nothing in `state.py`, `budget.py`, `artifacts.py` or the retry and
respec machinery knows what a program is.

The problem it solves is that generated images are refined the same way and
nowhere near as carefully. The usual shape is a person in a chat window, judging
by eye, with no record of the prompt or seed that produced the accepted version,
no assertion that the palette or the dimensions are right, and no bound on how
many attempts it took. Every part of that is something this harness already
does for code.

Three things make it more than a `kind` string, and the spec is mostly about
them.

- **A reviewer that cannot see the image is not a reviewer.** `Message.content`
  is a `str` and all five adapters format it as one. Multimodal messages are
  phase 1, and they ship useful on their own — a reviewer handed a screenshot on
  an ordinary code ticket needs the same change, with no image generation in the
  picture.
- **There is no compiler.** A six-fingered hand passes every mechanical check
  that can be written. Dimensions, palette distance, OCR and safe-area
  occupancy are real and worth asserting, and they are also a shell script the
  tester writes and `commands.test[".png"]` runs — no new loop machinery. What
  they miss falls on a paid vision reviewer, which is why review here is one
  call per *attempt* rather than per ticket, and why an unverifiable criterion
  blocks at ingest instead of running forever.
- **The refinement input is spatial.** "The hand is wrong" is a mask, not a
  sentence. So the next attempt is either a regenerate or an edit of the
  previous rendering, and the reviewer picks — the same distinction bug
  re-diagnosis draws when the first explanation is disproved.

**Open: which backend.** `supports_edit` decides whether the interesting loop is
buildable or degrades to regenerate-with-a-better-prompt. The spec is written so
a generate-only backend still works.

**The honest weakness: no evidence.** The per-language spec above opened with
three failing runs. This one opens with none — every claim in it was derived from
reading the loop rather than watching an image ticket fail, which is the kind of
argument §9 of [LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) exists to distrust. The
phase order is the mitigation: phase 1 is worth building even if the rest of the
spec does not survive contact.

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
  relying on a default. The image spec above makes this load-bearing: a reviewer
  that has to *see* needs a capability four of the five adapters cannot offer,
  and discovering that at review time costs the ticket.
- **Cross-ticket oscillation detection.** The executor now sees its last two
  failures, which is enough to spot an A-then-B-then-A cycle if it reads them.
  Detecting the cycle mechanically — comparing failure signatures across
  attempts, the way `signatures()` already compares them across tickets — would
  catch it without depending on the model noticing. Deferrable only while the
  terminating condition is a test result; an image ticket ends on an opinion, and
  the spec above treats stall detection as required rather than nice to have.
- **Reviewer cost.** Review is ~100% of the money on a hybrid run and roughly
  one call per ticket. Skipping review for tickets whose diff is trivial, or
  batching several tickets into one review, would cut that — at the cost of the
  thing that keeps a cheap executor honest. Needs evidence before it is worth
  the risk. An image ticket makes it worse in the other direction — one vision
  call per *attempt*, because every attempt produces something only a judge can
  rule on.
