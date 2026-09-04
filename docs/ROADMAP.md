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
unexercised brakes is most likely to have. A backlog that stalls now exists:
`examples/sample-project/STALL.md`, one ticket whose criteria contradict code
it may not write. Its first two runs finished **done** — a green ticket over a
criterion nobody met, produced by a sign-off pass that reworded the criterion's
value, a tester that softened it, and a reviewer that approved with nothing
testing it. Three mechanical guards later it ends blocked, with a note naming
the real problem; the write-up is in [CONVERGENCE.md](CONVERGENCE.md).

What that run still did not exercise is this entry's own subject: the ticket
never reached a state where its failure classes could descend, so
`_convergence`, the ladder and `flatCycles` have yet to be asked a question.

`examples/sample-project/HARD.md` was written to be the backlog that would ask
them — satisfiable, and not on the first try. It has been landed on the first
try four times, across two versions of it, the second with nine exact criteria
and seven independent details. Difficulty that comes from care is not
difficulty for this executor; what defeats it is a spec that is wrong. That is
also what the `Puzzle-Path` run these features came from was: 430 attempts
against criteria that could not all hold at once. So the honest test of the ten
features is a backlog whose specs are subtly wrong in a way that takes several
attempts to expose.

`examples/sample-project/GRIND.md` was written to be that backlog and has now
run twice, both 2026-09-01. It did not reach the middle either, and how it
missed is the finding.

Both runs ended `done`, both tickets, on the first attempt, with zero retry
cycles and no failed step — 19 calls and 129.8k tokens in 1081 seconds, then 19
calls and 132.7k tokens in 1116 seconds. All four delivered files are correct
on every criterion, checked independently afterwards. The second run was made
because the first was `n = 1`; it reproduced, with three of the four files
byte-identical to the first run's.

- **`GR-001`** bet that what defeats this executor is state over time rather
  than breadth — a sliding window whose evictions have to reduce counts, drop a
  word that falls to zero, and leave a list handed out earlier alone, with every
  criterion a sequence of calls. Nine for nine, one attempt. It joins `HARD.md`
  as a control case.
- **`GR-002`** was written to be jointly impossible and is not: its criteria
  hold together under a correction that runs in one direction only, adding a
  shortfall when the rounded shares fall short of `100` and leaving them alone
  when they overshoot. The impossibility argument assumed a symmetric fix.

What the loop did with `GR-002` is the result worth keeping. On both runs all
four roles refused to sign it on pass 1, each naming the same defect — *each
`1/3` share rounds to `33`, so the displayed percentages sum to `99`* — and the
planner repaired the **spec** while the criteria ratchet held seven criteria
unchanged. Pass 2 unanimous, build green first attempt, and the delivered
`table.py` identical between runs, so the repair converged on the same
asymmetric rule rather than stumbling onto it once. The first live evidence
that the sign-off pass catches a genuine spec defect and puts the repair where
it belongs.

**Lint was switched on, and it changed nothing.** The fixture graded a ticket
on its unit tests alone — `lint` and `typecheck` were `skip` for `.py` in both
workspaces — while the `Puzzle-Path` stall was overwhelmingly a lint stall. So
`flake8` was turned on in both builds on 2026-09-02 and `GRIND.md` run a third
time. Every lint step passed: start, baseline, per-attempt, final, both builds,
both tickets, no finding, and the delivered files are clean at `79` as well as
at the configured `88`. Whatever defeats this executor, writing lint-clean
Python is not it.

The run did cost one extra attempt, and the reviewer spent it: `GR-002`'s first
attempt met all seven criteria and was rejected because the equal-thirds test
asserted only the sum, where the ratification record had the tester promising
exact expected rows. That is `weakened_criteria`'s failure class caught by
judgement on a ticket the mechanical net had passed — and a point against the
*Reviewer cost* entry below.

It still did not reach the convergence machinery. `_measure_cycle` runs over
tickets eligible for a *retry cycle*, and `GR-002` finished inside its first
one, so `_convergence` was never called and `flat_cycles` stayed `0`. Two
attempts is not a cycle.

**Which narrows what this entry waits on.** Three kinds of defective spec are
now distinguishable, and only one of them reaches the convergence machinery.
`STALL.md` is a criterion contradicting code the ticket may not write — no
revision inside the ticket fixes it, so it parks. `GR-002` is a rule that
cannot produce one of its own criteria — revising the rule fixes it, before a
build call is spent. Neither becomes a failing attempt, and every brake in
[CONVERGENCE.md](CONVERGENCE.md) lives on failing attempts. What is left is a
ticket that ratification signs off honestly and whose *work* the executor then
gets wrong several times over, hard enough to exhaust `maxAttempts` and be
requeued — because only a requeue reaches `_measure_cycle`. Every attempt at
that shape so far — four runs of `HARD.md` and three of `GR-001` — has landed
on the first try.

**The experiment that stopped trying to out-hard the executor has run:**
[BLIND-GRADING.md](BLIND-GRADING.md), three times on 2026-09-03. It put the
reference run's actual defect back — a grading rule no prompt contains — and
changed one variable, `loop.toolchainContext`, between two otherwise identical
arms. Results:

- **Feature 1 has live evidence.** Showing the executor the `.flake8` it is
  graded by cut first-pass findings from 24 to 4, charged attempts from 3 to 1,
  and tokens by a quarter. Everything said about toolchain context before this
  was a replay.
- **Feature 6 was seen working once, under conditions a defect created.** At
  `maxAttempts: 2` the ticket failed its cycle, recorded *"the verify lint
  rejects E501 for lines longer than 50 characters in this repository"*, and
  the next cycle landed on its first attempt — against a reference run whose
  context column held the plan's paragraph verbatim after 86 cycles. Round two,
  with the defect fixed, produced no failed cycle and so no learning to record.
- **`_measure_cycle` ran for the first time**, recorded the cycle's classes and
  returned `FIRST` — and stopped running again once the defect below was fixed.

**Round two, after fixing what round one found, re-measured all of it.** The
first round's compile gate could not count lint findings, so it charged two
attempts it should not have; with that repaired, both arms land in **one**
attempt and nothing is ever requeued. Feature 1's honest value is therefore not
`3 attempts against 1` but 24 findings against 4, one extra gated turn, and 19%
more tokens. And the only time this fixture reached `_measure_cycle` was on the
strength of that bug: fixing it took the requeue away.

**The evidence now comes from replay, not from another live run.**
`tests/recorded.py` drives a real `Orchestrator` with the model and shell
scripted and the failure *details* lifted verbatim from these runs' databases
by `scripts/harvest_recording.py`. Both halves already existed — the suite has
scripted models throughout, and §9 of [LOOP-INVARIANTS.md](LOOP-INVARIANTS.md)
has always said to write fixtures from recordings — and the seam between them
is where both defects above hid.

Replayed that way, `_convergence`, `flatCycles` and rung one of the ladder all
run, deterministically and without a GPU. What it established: a failure set
shrinking *within one file* — 7 findings, then 3, then 1 of the same recorded
`E501` output — read `FLAT` twice and reached `reviewWhenStuck`'s default rung,
so the ladder escalated a ticket converging as fast as anything here ever has.
That is the *Adaptive ticket loop* entry's argument happening to real output,
and it is the one thing in that entry that has since been built:
`_convergence` now reads the size of the failure set as well as its members,
and the same recorded curve descends. See the volume section of
[CONVERGENCE.md](CONVERGENCE.md).

**Which changes what this entry is waiting for.** After eleven runs the reading
is no longer that the fixture is not hard enough, nor that its specs are too
good. It is that every mechanism below the ladder absorbs the failure the
ladder exists to escalate: the compile gate answers a lint failure inside the
attempt, the learning slot and respec answer it across a cycle, and
ratification answers a defective spec before a build call is spent. What would
reach the ladder is a defect whose *failure text does not describe it* — the
reference run's `TS2532 object is possibly undefined` names a symptom whose
cause is a compiler flag two files away, where `E501 line too long (52 > 50
characters)` hands over the rule.

**That ticket now exists, and three runs of it answered a different question.**
`examples/sample-project/OPAQUE.md` asks for a second renderer in the plugin
build whose bars must match the ones `histogram.bars` already draws. It may not
call `bars` and `bars.py` is not in its reading scope, so the rule that decides
a bar's length — `count * width // tallest`, multiply then floor — is nowhere
in the prompt. Every natural alternative agrees with it on most inputs, and the
criteria name three mappings where it does not; on one of them the rule renders
a word with no bar at all, which an implementation will read as a bug of its
own and repair the wrong way. What a failing attempt is shown is
`AssertionError: 'c  x1' != 'c # x1'`: one character, no rule, and no file it
can open to find one.

**It never produced that failure, and why is the finding.** Three runs, all
2026-09-03:

| | outcome | what happened |
|---|---|---|
| 1 | done, 1 attempt, 7 calls | `reading_scope` adds the source siblings of every writable file, so `bars.py` arrived beside a `legend.py` written in the same directory. The delivered file carried `bars.py`'s own docstring. |
| 2 | failed, 2 cycles, 27 calls | Module moved to a package of its own. Ratification then **blocked on pass 1**, naming the missing rule, and the repair added `bars.py` to the reading scope. The run itself died on a defect of the spec's own making — it asked for an *empty* `__init__.py`, which is `W391` when written as a blank line and "not empty" when written as a docstring. |
| 3 | done, 1 attempt, 12 calls | Trap removed. Ratify blocked on pass 1 again and carried pass 2 on a majority, the reviewer dissenting that the rule is unpinned outside the listed mappings. Every criterion verified independently against the delivered file. |

So the loop has **two independent ways of refusing a ticket whose failures
could not describe their own cause**: sibling expansion hands the neighbouring
file over silently, and the sign-off pass demands it in writing. Neither is a
defect. That is a better answer than the stall the backlog was aiming for, and
it narrows this entry further: reaching the ladder live now means turning
ratification off *and* placing the module where sibling expansion cannot see
it — a fixture arguing with two of its own safety mechanisms. The replay in
`tests/test_recorded_output.py` exercises the ladder directly and costs
nothing.

Two things the runs produced along the way. `_measure_cycle` ran on a live run
for the first time, recorded `cycle_volume` and carried it across a requeue.
And the learning slot recorded a **misgeneralisation** — *"a file must not end
with a trailing newline"*, from a `W391` about a blank line — which travels
with every later attempt on that ticket.

---

## Adaptive ticket loop — specified, not built

**Status:** specified in [ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md).
Written before the convergence work landed and revised against it, so about
half of the original draft is now a description of shipped behaviour rather
than a proposal.

The problem it names is the half of churn the convergence work does not
measure. `_convergence` compares this cycle's failure classes against the last
and answers *is the set moving* — `descending`, `churning`, `flat`. A ticket
failing on 38 distinct classes and one failing on 7 are indistinguishable to
every brake in the loop, and the only remedies either is offered are another
attempt, a respec, or a park. The missing remedy is decomposition, and the
invariant that makes it safe where respec is not: the union of the children's
acceptance criteria must cover the parent's, so scope is conserved by
construction rather than by judgement.

**Half of that is now built, and it is the half that was costing something.**
`_convergence` reads the number of findings a cycle ended on as well as the
classes, so a set shrinking inside one class descends instead of reading flat —
the false stall the recorded `E501` curve demonstrated. What is still unbuilt
is everything the volume was wanted *for*: nothing chooses a remedy from how
large the set is, and no ticket is ever split. The signal is recorded on the
ticket as `cycle_volume` and named in the log, which is where the entry below
says a signal should sit until a live run has an opinion about the threshold.

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

**Status:** phase 1 built, the rest designed — [IMAGE-LOOP.md](IMAGE-LOOP.md).
Multimodal messages landed on their own: a prompt may carry an `ImagePart`, a
provider declares whether it can see one, and a model that cannot is refused
before the request rather than shown a question with the image removed. The
loop builds them from a ticket's own reading scope, so a reference `.png` now
reaches the executor, the tester and the reviewer as something to look at —
where it used to arrive as several thousand replacement characters labelled
with the file's name. That is useful with no image generation anywhere, and it
is the only part of this entry that does not depend on the spec below
surviving contact with a model.

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
  was a `str` and every adapter formatted it as one. This was phase 1 and it is
  built; it shipped useful on its own, which is the argument the phase order was
  making.
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
  relying on a default. One capability now works that way and only one:
  `supports_images` is declared per model, and a prompt carrying an image
  reaches a provider that cannot see it as a refusal rather than as a silently
  text-only question. It is still discovered at call time rather than at
  `config.validate()`, because nothing yet knows a role is *going* to be sent
  an image — which is what `kind: image` would settle.
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
