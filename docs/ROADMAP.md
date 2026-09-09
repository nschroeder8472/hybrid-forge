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

## Context by retrieval — built, run live twice

**Status:** built, phases 1-5, and run live twice on 2026-09-04 —
[CONTEXT-TOOLS.md](CONTEXT-TOOLS.md), which now carries the result. Read-only
tools (`grep`, `read_file`, `list_dir`, `outline`), a generated repository map,
a stable cached prefix, reading granted rather than limited, and no silent
truncation for a role that can read. The executor, tester and reviewer all go
through `Orchestrator._converse`, a bounded conversation rather than one call.

The problem it solves is the first run this loop ever made against
hybrid-forge's own tree rather than against `examples/sample-project`. Run 1 of
`HANDBACK-DASHBOARD.md` ended blocked after 3 cycles, 9 attempts and 2.3M
tokens **having written no files at all**: 83% of its prompt was a test suite
the ticket never mentioned, five files of it cut mid-line, the one file its
spec named was absent, and every executor reply was a hallucinated tool call
asking for a shell.

Both halves of that are the same mistake. `evidence.reading_scope` computes a
scope before anyone has read a line, and relevance is a function of the task —
so it pasted 156k characters nobody needed and omitted the file the spec named.
The rule that would have saved that run is real, cheap, and would have been the
third such rule; the next one is already waiting behind it.

**It has now answered the question it posed, narrowly.** The measurement that
mattered was HD-001 rerun with the tools on, and the tools shipped mid-run: the
first nine attempts of run 1 are the pre-tools failure — 44 calls, 2.25M tokens,
no files — and attempts 10-12 are the same ticket, spec, models and tree with
one variable changed. The executor opened attempt 10 by reading
`forge/ui/server.py`, `forge/routes.py` and four slices of `forge/state.py`,
then wrote a diff. No hallucinated shell call appears anywhere after the tools
became real, and every attempt from 10 on wrote files. Run 2, the same backlog
re-ingested an hour later against a clean control, landed both tickets: 2
attempts each, 80 calls, 3.94M tokens, `final-lint` and `final-test` green.

What run 1 did *not* do is land — attempts 10-12 died on `E741` and then three
cycles of `E501` in the tester's own file, which is the failure the convergence
work is about rather than this one. So the claim this entry can now make is the
smaller one: with the pile removed the executor reads the files the spec names
and writes the patch, and what was left between it and a green ticket was line
length in generated tests. The prompt-size numbers are still only prompt-size
numbers, and one of them — the ~9k cached prefix — stays unmeasured, because
both runs used local models through a provider that reports no cache counters.

---

## Adaptive ticket loop — built, armed once, trigger off

**Status:** built 2026-09-06 —
[ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md) §12 records what each step
turned into. The volume counters, the criteria audit, the ratification report
and ticket splitting all landed; `loop.volumeThreshold` ships `0`, so nothing
is decomposed until somebody sets it.

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

**What it asked for first was a live run**, not code, and that run happened
before any of this was written — two of them, on 2026-09-04, recorded in
[CONTEXT-TOOLS.md](CONTEXT-TOOLS.md). Neither stalled, so the ladder was still
never asked a question; what they establish is that the machinery underneath
does not misfire on a run that is going well.

**What landed, in the order §12 asked for it.** The objection splitter was
replayed before it was trusted, over every recorded review in this repository
and both evidence trees: 24 rejections carrying 61 objections, 23 of them
carrying more than one. So the volume axis counts review points as well as
tool classes, and `distinct_classes` and `new_classes` are recorded per cycle,
logged beside the convergence line and shown on the dashboard — read by
nothing. Completion is audited against the frozen criteria on any ticket whose
criteria moved, and reports `scope-reduced` without touching the status.
`forge signoff` counts what each role does in a sign-off pass and how much of
what failed afterwards named a file the roles already had. And splitting is
built to its invariant: the union of the children's covered criteria must be
the whole of the parent's contract, checked mechanically before a child
exists, with the parent becoming a gate that completes when its children do.

**The trigger ships off, and that is the finding this entry keeps.** No value
of `volumeThreshold` on the only data available separates a ticket worth
decomposing from one about to land — at the drafted 8, the tickets with 10 and
32 classes both passed and the one with 7 was the unsatisfiable one. The
counters are now visible enough that the person who arms it can do so against
their own numbers.

**It has now been armed, and that is where the value came from.** Five live
splits at `volumeThreshold: 2` against the fixture written to be unsatisfiable,
2026-09-07, and every one of them found a defect —
[ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md) §6.6 has all four. None was
in `split.py`. Each lived at a seam between the new mechanism and machinery
that predates it: the retry cycle's eligibility guard, the criteria ratchet's
notion of coverage, the scheduler's shared-file ordering rule, the finish
tally. Unit tests calling `_consider_split` directly could not reach any of
them, because each was in what a *caller* did with a correct return value.

The expensive one is worth stating plainly. `covers` was honoured as an index
and abandoned as an obligation: the planner claimed a parent criterion, restated
it as the half that could be satisfied, and the invariant approved it by
counting. Both children passed, the gate closed, and the backlog that exists to
be unsatisfiable was reported **done** — a green ticket over a criterion nobody
met, produced by the mechanism written to prevent exactly that. A covered
criterion now crosses to the child verbatim.

The fifth run is the one the fixture was written to produce, and it is better
than what the loop did before splitting existed: three of four criteria landed
on a child that passed, and the ticket parked naming the single obligation that
could not be met.

**What none of it says is where the threshold belongs.** A class is
`(step, code, file)`, so the count tracks how many files a project has as much
as how big a ticket is — 32 and 38 on the reference run's large two-language
tree, never more than 2 across fourteen runs of a four-file fixture. The number
is not comparable between repositories, which is a stronger reason to ship at
`0` than the original one.

Two things are deliberately not built. RESTART waits on a live run showing the
ladder answering `winnable` on a ticket that then stays flat, which has not
happened. Plan-time prediction waits on completed tickets across more than one
repository, and its config key is absent rather than present-and-ignored.

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

## Sign-off cost — replayed, and the knob is not free

**Status:** measured 2026-09-07 by `scripts/ratify_cost.py`, nothing built. The
*Reviewer cost* entry below found that sign-off rather than review holds most
of the paid role's tokens, and pointed at `loop.ratifyPasses` as an existing
knob rather than machinery anybody has to write. This is what turning it down
would actually cost and buy.

The counterfactual is computable rather than estimated, which is why this one
needs no live run. A pass is every role voting; `ratify.resolve` decides the
outcome from those votes alone; and a unanimous pass returns before any
revision happens. So running the production `resolve` over the recorded pass-1
votes *is* what `ratifyPasses: 1` would have decided for that ticket, and the
recorded status is what it actually decided. 16 sign-off passes across both
evidence trees, 8 of which reached a second vote — exactly the disputed half,
because the second pass is never paid on a ticket everybody signed.

**What it costs.** Sign-off is 55% of all 9.99M recorded tokens, the largest
single line in either tree. Within it, pass 1 takes 3,163,600 tokens and the
revision plus every later pass takes 2,114,032 — so 40.1% of sign-off, and
roughly a fifth of everything, is what `ratifyPasses: 1` would not have spent.

**What it buys, and the answer is not nothing.** Three of the eight second
passes changed whether the ticket ships. Two are the same fixture in two runs:
pass 1 returned `split`, which proceeds to build, and the second pass blocked a
contract two of four roles had refused. One is a rescue in the other direction
— `blocked` at pass 1, unanimous after the revision. Turning the knob to 1
saves the 2.11M and builds that first contract twice while parking the ticket
the revision saved, which is the trade the default was making all along. The
difference is that it is now a number.

**Where the waste is, is legible.** Pass-1 status predicts whether the second
pass earns anything. Every outcome that moved started from `split` or
`blocked`; all three tickets that reached pass 2 on a `majority` finished
exactly where they started, at a cost of 730,005 tokens. A second pass made
conditional on the pass-1 status — run it on `split` or `blocked`, skip it on
`majority` — keeps all three outcome changes and returns a third of the extra
spend. Unlike the diff-size threshold in the entry below, that rule has a
mechanism behind it rather than a correlation: `majority` means more than half
the roles have already signed, so a revision has fewer votes left to flip. It
is still fitted on the same eight rows that score it, with three on the
majority arm, and one counterexample would break it. What it is not is a
threshold read off a distribution. Read the next two paragraphs before building
it: they say what the outcomes it preserves were actually produced by, and it
is not the revision this rule pays for.

**That next replay has run, and it moved the argument.**
`scripts/ratify_value.py` follows each pass into the attempts the ticket then
spent, and reads revision failures out of the run log rather than inferring
them from an empty `changed` list — `_revise` returns nothing and logs a
warning when the planner's reply cannot be parsed or runs out of output room,
and the next pass then votes on text nobody has touched. The plex tree's
database predates sign-off, so this half is the forge tree alone: 12 tickets.

**Three of the twelve passes voted twice on the same text**, the revision
having failed — and those three are exactly the passes whose status moved.
Both `caught` rows are among them. So the safety argument above is not what it
looked like: the second pass did not block those contracts because the planner
had improved them, but because the same four roles, reading the same words,
voted differently the second time. Pass 1 said `split`; pass 2 said 0 of 4
signed. A person later agreed the block was right — run 5's ticket was
discharged by hand, *"after two ratification rounds correctly rejected the
criteria timings"* — so the outcome was correct and pass 1's verdict was
simply wrong. What that makes the second pass, on this evidence, is a re-roll
of a noisy vote that the loop only performs by accident when a revision fails.
Which is a different mechanism from the one the conditional rule above is
aimed at, and a cheaper one to test: a second vote costs the roles, not the
planner.

**And the revision itself shows no downstream payoff.** Grouped by what
happened between the votes, the tickets that shipped went: settled (pass 1
unanimous, no revision) 5 built at a mean of 1.40 attempts and 189,172 tokens
after sign-off; revised 3 built at 1.33 attempts and 224,901 tokens; re-voted 1
built at 3.00 and 363,934. The revised group is no cheaper, and the sign of the
difference is against it. At three tickets against five, all of different
difficulty, that rules out a large effect and nothing finer — but the *"a
revised contract may build better"* defence of the `same` rows now has data
against it rather than nothing either way. One ticket still spent 265,747
tokens being revised from `blocked` to `blocked`.

**The revision's shape is now the fix, and it is built.** A planner rewrite
comes back as labelled fenced blocks — the executor's own shape, a heading
naming the field and a variable-length fence holding it verbatim — instead of
one JSON object. Nothing inside a block is escaped, so the failure below that
cost a completed reply to a single quote has nowhere left to happen, and the
prompt now asks for only the fields that change, with `criteria` kept
all-or-nothing so a partial list cannot read as a drop. JSON is still accepted
when no block is present.

What this does not do is rescue the three recorded failures: they are replies
to the older question, so they stay unparseable, and the argument for the fix
is that the class is structurally gone rather than that the old replies now
work. No repair heuristic was added for unescaped quotes inside JSON — a
guessed repair produces a wrong spec silently, and a corrupted contract that
parses is worse than one that is refused.

**Run live on `GR-002`, and the first run found a defect the tests could
not.** The transport was never the problem: every revision parsed, and a
block-format `spec` applied correctly. What the change had introduced was a
*steer*. Asking the planner to emit only the fields it is changing makes it ask
which field the objection is about — and the objection names a criterion. So it
rewrote the criterion instead of the rule, replacing the impossible sum with
the three rounded values that cannot add up to it. The ratchet refused that
correctly, the pass was thrown away, and the run cost 30 calls and 213.3k
tokens against the control's 15 and 108.1k. Three earlier runs of this ticket
had all repaired the *spec* first; the difference was the prompt, not the
model.

The prompt now says to reach for the spec first and why, and the re-run is
`unanimous` in one sign-off with `changed=['spec']`, at 62,141 sign-off tokens
over the same 9 calls the control used — a 6.7% difference on a line that had
doubled. The planner still reached for the criteria as well, growing them from
7 to 8, and the ratchet refused that half while keeping the spec change, which
is the mechanism working rather than a second defect.

Two things worth carrying. The whole-ticket rewrite was doing work nobody had
credited it with: putting the spec in front of the planner every time is what
kept the repair there, and a prompt that saves tokens by narrowing what a model
is asked for can narrow what it *considers* along with it. And the run's total
is 148,958 against the control's 108,122 — the gap is outside sign-off, three
extra conversation turns inside the same three steps, which is a different spec
being read rather than anything this change touches. One run each way; not a
claim that the change is cost-neutral overall.

**Why the three revisions failed is worth reading before raising a budget.**
Only one is the runaway it was reported as: 93,615 characters, 98% duplicate
lines, a thirty-line block emitted fifty-five times, cut off at the ceiling. A
second was truncated without repeating at all — 6% duplication, a well-formed
opening, and a tail that had wandered into prose about an unrelated ticket. The
third was never truncated: it stopped on its own, closed its fences, and failed
because one `"` inside a Python string in the `spec` field was left unescaped,
at character 14,677 of 14,676. The successful revisions ran 3,982 to 14,871
completion tokens against a 32,768 ceiling, which is the strongest evidence
that size is not the constraint. What the three share is payload *shape*: one
all-or-nothing JSON blob carrying source code, where a single character voids
the reply. A revision that carried only the fields it changes would shrink all
three failure surfaces; a larger ceiling addresses none of them.

**The largest single number this replay found is not about passes at all.**
81 sign-off votes returned a reply under 200 bytes — the four-line
`SIGNOFF`/`BLOCKING`/`SUGGEST` protocol — and spent 503,732 completion tokens
between them, a mean of 6,218 per yes-or-no. 66 of those votes spent more than
3,000, which is 97% of all vote output. The models reason to their allotment
before answering, and the answer is four lines. That is roughly a quarter of
sign-off's whole cost and it is not a pass-count question.

**The obvious remedy is wrong, and checking it is what produced the finding
worth keeping.** Turning reasoning off for the vote assumes the reasoning is
not what finds the defects — and deciding whether a ticket can be built is a
judgement, which is the last place to remove thinking from on a cost argument.
So `scripts/vote_cost.py` joins each vote *call* to the note that call
produced, positionally within the pass and checked on the role name, and asks
what the reasoning bought. 96 votes join.

The relationship runs backwards from the assumption behind the cost argument.
A vote that raised a blocking point or a suggestion spent a mean of 5,316
tokens; a vote that said NONE to both spent 6,575. A vote that refused to sign
spent 4,616; one that signed spent 6,786. **Of the 49 votes that ran to their
reasoning allotment, not one raised a single point**, and only two refused to
sign. Of the 47 that stopped on their own, 27 raised something and 23 refused.
Every objection in the record came from a vote that reached a conclusion.

Which says what the ceiling actually is. Reasoning to the allotment is not a
model thinking harder about a hard ticket — it is a model that has not
concluded, emitting its verdict because it is out of room. So the remedy is a
**smaller allowance, not none**: give the vote less room and a model that has
not decided answers rather than grinding to the ceiling. On this record that
costs nothing visible, because no capped vote ever produced an objection.

**And not per role.** The seats disagree. The planner raises objections at a
mean of 2,831 tokens against 6,356 when it raises none, and the reviewer at
2,937 against 6,583 — but the executor is the other way round, 9,975 when it
objects against 6,197 when it does not. A blanket setting would take room from
the one seat that looks to be using it.

All of that is correlational, which is the whole of what an artifact tree can
say: a model given less room might conclude sooner and miss something real,
and votes that never had less room cannot show it. The experiment that settles
it is [BLIND-GRADING.md](BLIND-GRADING.md)'s — one ticket, one variable,
objections counted on both arms.

**Building the experiment found that the version proposed here cannot be
run.** A smaller allowance is not available per call: llama.cpp honours
`reasoning_effort` as on or off — `low` and `high` measure identically against
the server this was written on — and the graduated `reasoning-budget` is a flag
on the model server, shared by every call that model makes. Planner and
reviewer share one; executor and tester share the other. So lowering the budget
to shorten a vote also shortens the planner's *revision*, which is the call
already failing by running out of room.

What is runnable moves the vote alone: `loop.voteThinking`, default true,
sending `reasoning_effort: none` on the vote and leaving the revision as it
was. `scripts/vote_grading.py` builds the two arms over `GR-002` — the ticket
whose defect both earlier runs found, all four roles refusing on pass 1 and
naming the same rounding error, which makes the measurement a count rather
than a judgement.

**It has run, on 2026-09-08, and it reverses the reading above.** One ticket,
one variable, both arms to completion.

| | `arm-thinks` | `arm-blurts` |
|---|---:|---:|
| sign-off spend | 62,061 | 6,073 |
| roles refusing on pass 1 | 4 of 4 | 0 |
| passes | 2 | 1 |
| attempts | 1 | 3 |
| failed steps | 0 | 2 |
| **total tokens** | **111,529** | **184,569** |

`arm-thinks` reproduced what the two earlier runs of this ticket did, for the
third time: every role refused, each naming *each `1/3` share rounds to `33`,
so the displayed percentages sum to `99`*, the planner repaired the spec, pass
2 was unanimous and the build landed first attempt.

`arm-blurts` signed the ticket off unanimously on pass 1. No refusal, no
objection, no revision, at a mean of **20 completion tokens a vote**. The
defect went through untouched and surfaced two steps later as
`AssertionError: 99 != 100` — the same arithmetic, found the expensive way.

So the correlation in the paragraphs above does not carry the causal claim
built on it. It was true that no vote which ran to its allotment raised an
objection, and false to read that as reasoning being idle: taken away, the
votes stopped finding anything at all. **The cheaper sign-off made the run more
expensive** — 55,988 tokens saved on the pass, 73,040 more spent overall, two
failed steps, two extra attempts. Not a quality-for-cost trade; worse on both.

Two smaller things the arms showed. Both tickets finished `done`, so the missed
defect cost attempts rather than correctness — this executor found the
asymmetric rule on its own once a test failure pointed at it. And the tests are
not equally strong: the repaired spec let `arm-thinks` assert the exact rows
`["a 34%", "b 33%", "c 33%"]` as well as the sum, where `arm-blurts` asserts
only that the sum is 100 and never pins which entry takes the extra point.

**`loop.voteThinking` stays, defaulted true, as the record of that.** It ships
because the question was worth asking and may be worth asking again on other
models — the finding is about these two, at these budgets, on one ticket — not
because there is now a reason to turn it off. There is a reason not to.

**The other half of the pass has not been priced, and the arm is built.** The
run above settles what a vote's reasoning is worth. It says nothing about the
planner's *revision*, which is the single most expensive call in a sign-off
pass and which failed outright in three of the eight recorded passes. Those
three are the same three whose outcome moved — the roles voted again on words
nobody had touched and changed their verdicts anyway — so every outcome change
in the record happened without a working revision, and the revision's cost has
never been separated from the second vote's.

`loop.reviseBetweenPasses`, default true, is that separation:
`arm-revotes` in `scripts/vote_grading.py` runs the pass twice with the
objections still travelling and only the rewrite gone.

**It ran on 2026-09-08, and the second of the two predicted outcomes is what
happened.**

| | `arm-thinks` | `arm-revotes` |
|---|---:|---:|
| outcome | done | **blocked** |
| attempts | 1 | 0 |
| sign-off spend | 58,240 | **85,555** |
| total tokens | 108,122 | 85,555 |

Pass 1 was the control's pass 1 — all four roles refusing, each naming the
rounding defect. Pass 2, on text nobody had touched, **all four refused
again**, and two of them said so about the standing position rather than about
the ticket: *"The prior answer is wrong in treating the sum criterion as
settled"*. Not one verdict moved. The ticket parked, was respecced, went back
to the roles, and parked again.

So removing the revision does not save what it costs — **it made sign-off 47%
more expensive and delivered nothing**, because a ticket that cannot be
repaired cycles through the pass instead of leaving it. Sign-off was 100% of
that arm's tokens. Nothing reached a build call: `attempts` is 0.

Two things this settles beyond the setting. **The revision is what converts a
correct refusal into a shippable ticket** — the second vote alone does not, and
~26k tokens is the price of that conversion against a ticket the pass would
otherwise refuse forever. And the *vote instability* reading above is weaker
than the three accidental re-votes made it look: given a correctly stated
objection and nothing changed, these roles hold their position unanimously
rather than flipping. Whatever moved those three verdicts, it was not the mere
fact of being asked twice.

What the arm did do is show the pass working exactly as designed on a contract
it should refuse: it caught the defect, held under a second asking, and parked
with a note naming the fix — *"replace it with exact expected..."*. That is the
outcome an operator wants when no repair is available, and it is what
`ratifyPasses` is for.

**Three ways of making sign-off cheaper have now been tested and all three are
dead**: skipping review for trivial diffs has no safe threshold, taking
reasoning off the vote stops the pass finding anything, and removing the
revision stops the ticket landing.

**The fourth is built, and it is the one that removes nothing.**
`loop.majorityRepeats`, default true, declines to put a pass-1 `majority` to
the roles a second time. A majority already ships the ticket, and across every
recorded pass that reached a second vote on one — three tickets, 730,005
tokens — not one ended anywhere other than where it started. Unlike the two
settings above it takes no part of the mechanism away: the vote still reasons,
the revision still happens wherever the pass continues, and a `split` or a
`blocked` still gets its second look. It only declines to re-ask a question
whose answer has never changed.

**What it does not yet have is a live arm, and two attempts to build one
failed.** `examples/sample-project/SPLIT.md` was written to produce a pass-1
majority — ordinary work plus one criterion aimed at a single role — and has
been run twice, unanimous both times. The first version asked for a wall-clock
bound; all four signed, because a bound a test can approximate badly is not one
a tester refuses. The second asked for an implementation detail no assertion
can reach, and the tester **found exactly that** — *"only assertable by
source/mock inspection; consider removing it"* — and signed anyway, filing it
as a `SUGGEST`.

That is the protocol working and the fixture's premise being wrong, and it is
the more useful result. `BLOCKING` means *I cannot do my part*; `SUGGEST` means
*this could be better*; a role that can proceed while objecting signs. So a
pass-1 majority needs an impossibility landing on exactly one role, and the
impossibilities a spec actually contains are visible to all four at once —
which resolves `blocked`, not `majority`. Consistent with where the recorded
majorities came from: real backlog tickets against a large tree, not a
four-file fixture.

So this setting ships on replay evidence and unit tests, with its live arm
unbuilt and a note saying a third guess at the criterion is the least promising
route. What would settle it is a majority from a real backlog.

**One defect fell out of the join.** Two votes refused to sign while naming
nothing: `SIGNOFF: no` over `BLOCKING: - NONE`. `resolve` counts the refusal
against the ticket, but `_revise` builds the revision prompt from the blocking
points, so the planner is handed a rejection with nothing to fix. Both are
votes that ran to their allotment, which is consistent with the reading above —
a model out of room emits the protocol without having filled it in. One of the
two is run 5's planner, in one of the three passes where the revision then
failed. A refusal that names no objection is a vote the protocol cannot act on,
and the pass has no way to tell it apart from a signature.

**That is now fixed by asking again rather than by discarding the vote.** A refusal naming nothing is put back to the same role in the same thread — the question it was asked, its own reply, and a turn saying that reply refuses without naming anything and would park the ticket with no reason on it. A model that had a reason gives it; one that refused by accident signs. Two guards keep it from becoming an argument: a second empty refusal leaves the refusal exactly as it stood, and a *truncated* refusal is not re-asked at all, because running out of output room already has its own reading and is recorded as the blocking point.

---

## Next, in order

Four things this repository's own runs left open, in the order they are worth
doing. All of them are written up in
[EXECUTOR-CEILINGS.md](EXECUTOR-CEILINGS.md), which is the evidence for each.
The first two are done; the entries are kept rather than deleted, because what
was built is only worth reading beside the run that asked for it.

**1. Close the store's connections explicitly — done, 2026-09-09.** `Store`
kept a connection per thread in `threading.local()` and `close()` closed only
the caller's, so a `Store` left to the garbage collector held a Windows file
lock on its database. Run 11's test step failed four times with
`PermissionError: [WinError 32]` on a temp directory a loop-written test was
cleaning up, and any ticket whose tests build a `Store` in a temp directory met
the same thing.

Three closes now, at descending levels of promise. `close()` is unchanged and
still touches only the calling thread's connection — the dashboard calls it as
each connection thread finishes, which is where the leak was: a thread per
connection, a connection per thread, and nothing closing either. `close_all()`
closes every connection the store opened, for a caller that can promise nobody
else is using it, and `__enter__`/`__exit__` are that promise written down, so
a test can own a store with `with` and then delete its directory. A
`weakref.finalize` is the backstop for the store nobody closes at all, which is
the case that matters most: it fires when the store becomes unreachable rather
than when each thread's locals clear, and the tests the loop writes will not
know about the context manager. Closing another thread's connection at all is
what the new registry in `Store._open` is for.

The suite is the visible half of the result: `tests/test_forge.py` alone
emitted **721 `ResourceWarning`s** and now emits none, with the nine left
across the whole suite coming from `memory.py`'s subprocess pipes and test
sockets. Touched `state.py` and `ui/server.py`.

**2. Stop the executor narrating before it acts — done, 2026-09-09.** Run 11's
first attempt read successfully, identified everything it needed, and then
spent its whole output budget writing that summary down before emitting a
single edit. The prose was accurate and unnecessary.

The cheap version was the right one, and the expensive one turned out not to be
a fix at all: a parser that ignored everything before the first path line would
change nothing, because `parse_output` already does — the budget is spent
before the parser ever sees the reply. So `EXECUTOR_SYSTEM` states the rule
— *blocks first, prose never* — with the lost attempt as its reason.

What was missing beside it is the correction sent after a reply is cut off.
There was one message for every truncation, telling the model to send fewer
files per response, which is unusable advice for a reply that never named a
file. `names_a_file` splits the two cases, counting a path line whose fence was
cut off mid-block, so the narrating reply is now told that its budget went on
prose. Touched `prompts.py`, `patch.py` and `loop.py`.

**3. Reserve tool turns rather than announcing them.** `_converse` says *"That
was your last read. Answer the ticket now"* at `remaining == 2` and the model
reads anyway. Raising the cap is measured not to help: 8 turns produced 14
reads, 16 produced 28, and both ended identically, because the appetite scales
to whatever it is given. Hard-stopping reads at `N-2` makes exhaustion
structural instead of a request.

**4. Reference an example of anything a ticket has to write.** Of 280 reads
across runs 8-10, **38.6% were of `tests/`** — the executor working out house
conventions for a test file the ticket told it to write and gave it no example
of. This is a spec habit rather than a loop change, and it is the cheapest
thing on this list: `forge-spec` should ask for a reference test the way it
already asks for a test path.

**A type checker is unclaimed.** `.flake8` is the only static analysis
configured. Every shape defect this session cost a full test run to find — a
kwarg threaded through five providers, a `Callable` alias becoming a `Protocol`,
a dataclass field whose emptiness would have silently blinded three guards.
mypy and pyright are development dependencies, so the zero-runtime-dependency
rule never blocked them.

---

## Deferred from the review

Found while reviewing the loop, judged not worth building yet.

- **Retention for the step log — half built.** `Store.clear_step_detail` landed
  as RT-001, written by the loop itself against this repository, and nothing
  calls it yet. Measured on this repository's own database: `steps.detail` was
  1,871,779 bytes of 2,551,808 — 73% of the file, from 285 rows across seven
  runs. `PRUNE.md` is the wiring plus the `VACUUM` that makes the file actually
  shrink.
- **What stops the executor on a real repository** —
  [EXECUTOR-CEILINGS.md](EXECUTOR-CEILINGS.md). Four ceilings found by running
  one ticket against this tree, each hidden behind the last: whole-file output
  (fixed by replacement blocks), read exhaustion (measured, unfixed — raising
  `toolTurns` from 8 to 16 doubled the reads and changed nothing), a
  misdiagnosis that wasted the loop's one free correction (fixed), and tool
  insufficiency (fixed by `grep(context)` and `read_symbol`; `outline` retired
  at 0 calls in 49 across three probe arms). Its `ResourceWarning` finding on
  `Store`'s per-thread connections — measured *not* to be a leak, and breaking
  Windows test cleanup anyway — is fixed; see item 1 above.
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
- **Reviewer cost — replayed, and the premise was wrong about which step.**
  Review is the paid role on a hybrid run, and the two remedies this entry
  named were skipping review for tickets whose diff is trivial and batching
  several tickets into one review. It asked for evidence before either was
  worth the risk. `scripts/review_cost.py` is that evidence, and it cost
  nothing: every model call is recorded beside its step with its role, its
  verdict and its exact usage, so a candidate rule can be priced against
  reviews that already happened. Replayed 2026-09-07 over both evidence trees
  — 27 reviews, 11 of them rejections.

  **The money is not where the entry said.** `reviewer` is 18.9% of the 9.99M
  recorded tokens, and it spends them on two steps rather than one: sign-off
  takes 1,171,807 of them over 24 calls and the review step takes 721,683 over
  27. Sign-off is the majority of the paid spend at two thirds the call count,
  because its prompts are fatter — 48.8k a call against review's 26.7k. Both
  are billed at the same rate for the same reason, that they are the same role.
  So the lever this entry was written to justify building sits on the smaller
  half, while the larger half is already a config knob: `loop.ratifyPasses`,
  default 2, whose cost `docs/CONFIG.md` describes as a judgement rather than a
  measurement. That knob has since been replayed too — see *Sign-off cost*
  above, which is where the remaining money in this entry went.

  **Skip-if-trivial buys nothing, and the replay says so without a threshold
  argument.** A rule of the shape *skip review when `measure` < T* keeps every
  rejection exactly while `T` is at or below the smallest value any rejection
  took, so the script reports that ceiling rather than guessing a T. Over all
  27 reviews the ceiling is the floor of the distribution on every measure:
  rejections run down to 0 files written and 39 characters of executor output,
  and the most any safe rule skips is 2 reviews and 1.4% of review spend.

  The reason is the case the rule would skip first. Eight of the eleven
  rejections are HD-001 attempts that wrote **no files at all** — the pre-tools
  failure the *Context by retrieval* entry above is about. `Orchestrator`
  reviews an empty diff on purpose: it cannot tell "already on disk" from
  "never written", so it shows the reviewer the file state instead, after a
  real verdict read *"No build.sh, build.ps1, README.md exist"* about a
  repository where all three did.

  **Dropping those does not rescue the rule.** On the 19 attempts that wrote
  something, with 3 rejections left, the best safe rule is `files < 2`: 4
  reviews skipped, 14.4% of review spend, no rejection silenced. That number is
  not shippable and the script's own framing is why. The threshold was fitted
  as the minimum over the same three rejections it is then scored against, so
  it is safe by construction and says nothing about data it has not seen — and
  it has no margin, because the nearest rejection sits at exactly 2 files while
  four accepted reviews sit at 1. It is the *Adaptive ticket loop* entry's
  finding in a second place: size does not separate the work that needs
  judgement from the work that does not.

  **What the replay leaves open.** Batching was not tested — nothing recorded
  shows a reviewer holding several diffs at once, and the mechanical hazard is
  visible without data, since approval is inferred from the absence of `REJECT`
  and a batched verdict multiplies both the truncation risk the loop already
  guards and the difficulty of attributing a rejection to a ticket. Every
  recorded run used local models throughout, so `cost_usd` is 0 across both
  trees and token share is the proxy for the bill. An image ticket still makes
  this worse in the other direction — one vision call per *attempt*, because
  every attempt produces something only a judge can rule on.
