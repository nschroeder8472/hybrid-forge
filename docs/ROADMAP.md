# Roadmap

Ideas not yet built, with enough of the reasoning to pick them up cold. Nothing
here is committed to. An entry earns its place by naming the problem it solves,
not the feature it adds — a feature whose problem has gone stale should be
deleted rather than implemented.

---

## Convergence — built, barely run

**Status:** ten features specified in [CONVERGENCE.md](CONVERGENCE.md), derived
from the `Puzzle-Path` run of 2026-08-22/23. Nine shipped; Feature 5 was built,
replayed against that run's own data, and reverted. Every number in that
document is a replay over recorded steps, which is the weaker evidence in the
direction that matters: it shows what the code would have done to failures that
already happened, not what a live run does to the failures it causes itself.

**One live backlog has now run with all of it on** —
`examples/sample-project`, 2026-08-31: three tickets, 27 steps, all green, 21
calls, 87.2k tokens, 619 seconds, zero retry cycles. That is a floor, not
evidence about convergence: nothing failed, so no brake in that document was
ever asked a question. What it does establish is that none of the nine fires
spuriously on a run that is going well, which is the failure mode a set of
unexercised brakes is most likely to have. A backlog that *stalls* is still the
run this entry is waiting for, and the fixture is where to build one — widen a
ticket's criteria until the executor cannot satisfy them and watch what stops.

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

## Adaptive ticket loop — specified, not built

**Status:** specified in [ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md).
Written before the convergence work landed and revised against it, so about
half of the original draft is now a description of shipped behaviour rather
than a proposal.

The problem it names is the half of churn the convergence work does not
measure. `_convergence` compares this cycle's failure classes against the last
and answers *is the set moving* — `descending`, `churning`, `flat`. Nothing
reads *how big the set is*. A ticket failing on 38 distinct classes and one
failing on 7 are indistinguishable to every brake in the loop, and the only
remedies either is offered are another attempt, a respec, or a park. The
missing remedy is decomposition, and the invariant that makes it safe where
respec is not: the union of the children's acceptance criteria must cover the
parent's, so scope is conserved by construction rather than by judgement.

Three things the revision settled against the reference run rather than by
argument, each worth reading before picking the document up:

- **Its volume threshold splits the wrong tickets.** At the drafted value, the
  two tickets that went on to pass are decomposed and the one genuinely
  unsatisfiable ticket is left alone. The same shape as the `flatCycles`
  finding, and the same conclusion: record the signal, ship the brake off.
- **Its rule-promotion pipeline is Feature 5**, which was built and reverted —
  the planner rephrases every lesson, so a gate keyed on recurring text
  promotes nothing.
- **Its respec rule is backwards.** It permits adding acceptance criteria; the
  ratchet refuses additions, because the party that has just exhausted its
  attempts does not get to raise the bar it is judged against.

**What it asks for first is a live run**, not code. Building split on top of
nine unvalidated features makes a second layer with no way to attribute a
failure to either.

---

## Handback — built

**Status:** stages 1-5 shipped as `forge advise`, `forge release`,
`forge discharge` and `forge criteria --add`, with `withheld:<reason>` as the
route and `withheld` as a status distinct from `skipped`. Stages 6-7 — the
dashboard read side and its write endpoints — are deliberately not built; see
[HANDBACK.md](HANDBACK.md) for why, and for what the mechanism has since been
exercised against. This entry keeps the problem it was written around.

The loop has seven ways to stop working on a ticket and hand it back — a route
it will not take, an unmet dependency, `BLOCKED:`, `IMPOSSIBLE:`, a ladder
park, ratification without a majority, a bug that never reproduced. Every one
of them writes a sentence into `blocked_note` addressed to a person, and there
is no channel for that person to answer on. `respec_prompt` already carries
`report`, `ruled_out`, `contradiction` and `stuck`; a human's note is the block
it does not have.

Two smaller defects the document is built around. `TICKET_SKIPPED` means both
*a person must write this* and *this is waiting on PF-002*, distinguishable
only by reading prose. And a ticket routed away from the executor has no exit:
`forge retry --all` resets it to `pending`, `_work_ticket` re-reads the route
and skips it again, and nothing in the codebase ever writes `ticket.route`
after ingest — so work a human has already implemented by hand cannot rejoin
the run, and its dependents stay parked behind it.

`claude-only` became `withheld:<reason>` over a closed vocabulary drawn from
the categories the delegation-protocol skill already lists. The colon form was
deliberate: every gate is written as `route != "delegate"`, so the reason was
added without touching one of them, and rows recorded by older runs keep gating
correctly — `claude-only` still parses and still withholds, reading as
`withheld:unspecified` rather than being rewritten in place.

**The gate itself is not up for removal.** A withheld ticket is withheld
because a model should not write that code, not for want of detail, and a
better-specified auth ticket still ends with a model writing auth.

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

**Run four times, end to end.** Against `examples/sample-project`: a prose
report, a reproduction that went red on the code and green after the fix, one
attempt, 12 calls, 73.5k tokens, no retry cycle, and — on the fourth run — no
warning of any kind. Five defects came out of those runs: `reproduce-test`
reported as failed on a run where nothing went wrong; a sign-off pass that
demanded acceptance criteria a bug ticket must not have, which blocked the
second run on the same input the first had fixed; the revision pass that had
not been told either, so it proposed criteria the ratchet refused once per run;
a revision prompt that asked for a `context` it never showed, so every revision
replaced a paragraph it had not read; and a retry rule that filed a ticket
blocked *before* reproduction as an unreproducible bug, suppressing the retry
that would have helped and telling the human to sharpen a report that was never
the problem. All repaired, all re-confirmed live. See the first-live-runs
section of [BUG-LOOP.md](BUG-LOOP.md). The two Tetris defects below are still
the harder case: a fault in behaviour over time rather than in one call's
return value.

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

## Per-language verify commands — built

**Status:** built — [LANGUAGE-COVERAGE.md](LANGUAGE-COVERAGE.md), which carries
the same status. All five phases landed, along with `forge toolchain`, the
workspace layer described in [WORKSPACES.md](WORKSPACES.md), and the wizard
asking per language at `forge init`. Nothing here is open. This entry keeps the
problem it was written around.

`commands.test` was one string, which assumes a repository is one language.
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
  is a `str` and every adapter formats it as one. Multimodal messages are
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
  that has to *see* needs a capability most adapters cannot offer — and on
  `llamacpp` it is not even a property of the adapter but of the checkpoint,
  since forge turns the projector off by default. Discovering that at review
  time costs the ticket.
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
