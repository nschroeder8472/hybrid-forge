# Roadmap

Ideas not yet built, with enough of the reasoning to pick them up cold. Nothing
here is committed to. An entry earns its place by naming the problem it solves,
not the feature it adds — a feature whose problem has gone stale should be
deleted rather than implemented.

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
