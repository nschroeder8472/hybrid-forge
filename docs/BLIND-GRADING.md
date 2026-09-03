# Blind grading: the experiment that puts the reference stall back

**Status:** run twice over — round one on 2026-09-03, then the two defects
it found were fixed and round two re-measured what the repairs had
invalidated. Both rounds' predictions are left where they were written. The predictions below were written before
the results and are left as they stood; what actually happened is in *The
results* at the bottom.

Build it with `python scripts/blind_grading.py <directory>`; the script writes
both arms and ingests the ticket, and prints the two `forge go` commands. It
does not run them.

## The problem this is for

Nine of the ten features in [CONVERGENCE.md](CONVERGENCE.md) shipped, and every
number in that document is a replay over recorded steps. `_convergence`, the
escalation ladder and `flatCycles` have never run on a live backlog.

Seven runs of `examples/sample-project` have tried to reach them by making the
work harder, and every one landed on the first attempt:

| backlog | runs | result |
|---|---:|---|
| `HARD.md` | 4 | done, first attempt |
| `GRIND.md` `GR-001` | 3 | done, first attempt |
| `GRIND.md` `GR-002` | 3 | done; one run cost a second attempt, from a review |
| `STALL.md` | 3 | blocked, on a guard, before the ladder starts |

The eighth attempt was to grade the work mechanically — `flake8` switched on in
both fixture builds on 2026-09-02. Every lint step passed, and the delivered
files were clean at 79 columns as well as at the configured 88.

**The mistake was in the premise.** The reference run these features come from
was never a run where the model wrote bad code. It was a run where the model
was graded against configuration no prompt contained: 512 `TS2532` from a
`noUncheckedIndexedAccess` in a `tsconfig.json` nobody was shown, 1,125
trailing-whitespace occurrences against an unseen `gdlintrc`, 117 of one
ticket's 160 lint failures with whitespace as their only problem. Feature 1 —
toolchain context — fixed exactly that, and it is *why* the fixture cannot
reproduce the stall. The brakes are for a car that has since been repaired.

So the experiment puts the defect back, under control, and changes one thing.

## The design

Same ticket, same model, same everything, one variable:

| arm | `loop.toolchainContext` | the executor |
|---|---|---|
| `arm-blind` | `false` | is graded by `.flake8` and never shown it |
| `arm-shown` | `true` | is shown it |

The ticket is `GR-001` from `GRIND.md`, lifted out of that file by the script
rather than copied, so the experiment cannot drift from the work three earlier
runs landed on the first attempt. The comparison is only worth something while
it is the same ticket.

The rule is `max-line-length = 50`. It has to be a number no default satisfies,
or the arms measure nothing: run 3 established that this executor writes clean
Python at 79 unprompted, so anything near that would be met by accident. 50 is
not. Checked against run 3's actual delivered files, the strict config reports
**17 findings**, so `arm-blind` fails its first lint and keeps failing until it
infers the number from `E501 line too long (73 > 50 characters)` — which is the
same inference Puzzle-Path's executor was asked to make about a compiler flag,
one attempt at a time.

The four committed root-build files are longer than 50 columns and are not the
ticket's work, so they are grandfathered by `per-file-ignores`. The baseline
has to be green or the run reports the fixture's own debt as the ticket's.

`retryCycles` is raised from the fixture's `1` to `4`. This matters more than
it looks: `_measure_cycle` runs over the tickets eligible for a **retry cycle**,
and `reviewWhenStuck` fires on the second *flat* one. A ticket burning three
attempts inside its first cycle reaches neither. Two cycles cannot reach rung
one. This was the reason run 3's second attempt measured nothing.

## What each arm is predicted to do

Written down first, so the run can disagree.

**`arm-blind`** fails `lint` on its first attempt with `E501`, exhausts its
three attempts, and is requeued. `_measure_cycle` then runs for the first time
on a live backlog. The failure class is the same every cycle — one class,
`E501`, in one file — so `_convergence` should report `FLAT` rather than
`DESCENDING` or `CHURNING`, `flat_cycles` should climb, and rung one of the
ladder should fire on the second flat cycle: the reviewer is asked whether the
ticket is winnable at all. It is winnable, so the honest answer is yes, and the
interesting question is whether the reviewer says so or convicts an innocent
ticket.

The one place the prediction may be wrong in an uninteresting way: the executor
may simply read the number out of the failure text on attempt two and land it.
That would be a real answer — the two-failure window and the "you have seen
this before" paragraph doing their job — and it would mean this shape of stall
is no longer reachable at all with a capable model, which is worth knowing and
worth writing here rather than being explained away afterwards.

**`arm-shown`** lands, on the first attempt, exactly as the three earlier runs
of this ticket did. `.flake8` is in the prompt under its own heading, the
executor writes to 50 columns, and lint passes.

## What to record

Not the verdict. The verdict is predicted for both arms and neither is
interesting on its own — the comparison is the result.

- Attempts, cycles, calls, tokens and wall clock for each arm.
- Every `_convergence` verdict in order, and what `flat_cycles` reached.
- Which rung of the ladder fired, and what the role it asked actually said.
  A planner calling a satisfiable ticket `impossible` looks identical to a
  correct park in the log — the note is the thing to read, and that failure
  mode is already flagged as unmeasured in [CONVERGENCE.md](CONVERGENCE.md).
- Whether `arm-shown` landed first try. If it did not, the experiment has a
  confound and the arms are not comparable.

## The honest limit

This reintroduces a defect that is fixed, in a form chosen to be reproducible.
A result here says the brakes work — or do not — on *this* stall shape: an
invisible grading rule producing one repeating failure class in one file. It
says nothing about stalls with other causes, and the fixture still has no
example of one.

It is also, for the same reason, the strongest available evidence for Feature 1
itself, which has never been tested live either. If `arm-blind` stalls and
`arm-shown` lands, that is one controlled variable separating a run that
converges from one that does not.

---

## The results — 2026-09-03

Both arms ended `done`. That was predicted for `arm-shown` and is the boring
half of `arm-blind`'s prediction; the numbers between them are the result.

| | `arm-blind` | `arm-shown` |
|---|---:|---:|
| `loop.toolchainContext` | `false` | `true` |
| findings on the first lint | **24** | **4** |
| failed lint steps | 3 | 1 |
| charged attempts | **3** (the whole budget) | **1** |
| build steps | 4 | 2 |
| calls | 10 | 8 |
| tokens | 84.2k | 63.6k |
| seconds | 621 | 530 |
| delivered code | correct on all nine criteria | correct on all nine criteria |

### Feature 1 has live evidence now

Everything said about toolchain context in [CONVERGENCE.md](CONVERGENCE.md) was
a replay over recorded steps. One variable, two arms, same ticket and model:
showing the executor the `.flake8` it is graded by cut first-pass findings by
six times and charged attempts by three, at 25% fewer tokens. It did not
eliminate the failure — `arm-shown` still failed its first lint, on four lines
— so the claim the evidence supports is *substantially cheaper*, not *correct
first time*.

### `_convergence` would have called the converging arm flat

This is the finding worth keeping, and it was not predicted.

`arm-blind`'s lint failures went **24 → 7 → 7 → 0**. Its recorded failure class
was the same string every time:

```
lint : e5# line too long # # characters
```

Digits are normalised to `#`, so every `E501` at every width is one class. The
class *set* is therefore identical across attempts while the instance count
falls by more than two thirds. `_convergence` compares sets, so a ticket
visibly getting closer scores `FLAT`, `flat_cycles` climbs, and the escalation
ladder starts asking whether the ticket is winnable — on the run where the
executor is in fact converging fastest.

That is exactly the gap the *Adaptive ticket loop* entry in
[ROADMAP.md](ROADMAP.md) argues from — *"`_convergence` ... answers is the set
moving. Nothing reads how big the set is"* — and this is the first live case of
it. It is also the shape the reference run had: 1,125 whitespace occurrences
and 512 `TS2532` are enormous instance counts over tiny class sets.

### One build returned nothing and the loop paid for it

`arm-blind`'s third build step produced zero bytes. Nothing was applied, and
`lint` ran again over unchanged files and returned the **byte-identical** seven
findings. The step was not charged as an attempt, so the budget survived, but a
call was spent and the loop manufactured a repeat failure that no change caused
— the strongest possible false signal for any stall detector keyed on failures
repeating.

### And the machinery still has not run

`flat_cycles` is `0` and `cycle_classes` is empty in both arms.
`_measure_cycle` runs over tickets eligible for a **retry cycle**, and both
tickets finished inside their first one. `arm-blind` used its whole
`maxAttempts: 3` budget and landed on the third, which is as close as eight
runs have come.

So the next dial is `maxAttempts: 2`. At two, `arm-blind` fails its cycle on
the evidence above, is requeued, and `_measure_cycle` runs for the first time —
against a failure curve that is genuinely descending and a class set that says
it is not. That makes it a test of the detector rather than of the executor,
which is the more useful thing to be testing by now.

---

## The third run: `arm-blind` at `maxAttempts: 2`

Built with `python scripts/blind_grading.py <dir> --attempts 2 --only arm-blind`.
Nothing else changed. 11 calls, 97.2k tokens, 717 seconds, **1 retry cycle**,
`done` — two attempts spent and failed in cycle one, then one attempt in cycle
two. Delivered code correct on all nine criteria.

### `_measure_cycle` ran, for the first time in nine runs

The ticket failed its cycle, was requeued, and the measurement finally
happened. `cycle_classes` holds what it recorded:

```
["lint : e5# line too long # # characters"]
```

with `cycle_mark` at step 16. The verdict was `FIRST` — nothing to compare a
first measured cycle against — so `flat_cycles` stayed `0` and no rung of the
ladder fired. `FLAT` needs a *second* failed cycle, and there was not one,
because the loop fixed itself in between.

### Feature 6 worked, live, and it is the one that mattered

The ticket recorded what it had learned and carried it across the cycle:

```
The verify lint rejects E501 for lines longer than 50 characters
in this repository.
```

Respec then revised the context with the same diagnosis in its own words —
*"the failures are lint/formatting failures (E501), not behavioral ones: the
ticket omitted the repository's 50-character line limit"* — and cycle two
landed on its first attempt.

That is the exact mechanism whose absence this document's parent was written
about. On the reference run, `_preserve_plan_context` rebuilt the context from
the plan every cycle and **not one operational conclusion survived 18 hours**;
after 86 cycles the column held the plan's paragraph verbatim, twice. Here one
cycle was enough to discover an invisible rule, write it down, and hand it to
the next cycle, which used it.

### Which is why the ladder is still unreached, and that is the finding

Three runs have now tried to make this stall repeat and it will not. The
sequence is: the executor fails on a rule it cannot see, the failure text
carries the rule, the learning slot preserves it, respec states it, and the
next cycle lands. The escalation ladder sits below all of that and only sees
tickets those mechanisms failed to rescue.

So the honest reading is no longer *the fixture is not hard enough*. It is that
**`flatCycles` and the ladder are backstops for a failure the repaired loop
does not produce**, at least in this shape. Reaching them needs a defect the
failure text does not describe — where the executor cannot read the rule out of
what it is handed, so there is nothing for the learning slot to preserve. An
invisible *lint* rule is not that, because `E501 line too long (52 > 50
characters)` states the rule in the failure. The reference run's `TS2532` did
not: *"object is possibly undefined"* names a symptom whose cause is a compiler
flag two files away.

### Two defects the run exposed — both since fixed

**The compile gate charged an attempt on zero compile errors.** Attempt 2's
build returned no output, which was read as *did not compile*, and then:

> `0 compile error(s) left, against 0 before the last inner turn; charging the
> attempt instead of asking again.`

An empty build is not a compile failure, and a gate that reports `0` errors
before spending an attempt on them is measuring nothing. Same empty build as
the first `arm-blind` run, which suggests it is reproducible rather than a
one-off — and it is what makes a byte-identical lint failure appear with no
change behind it, which is the worst possible input to a stall detector.

**Cycle two started over a red baseline.** The failed ticket's two files stayed
on disk, and the loop said so:

> `no baseline tree was recorded, so its 2 file(s) stay in the tree as it left
> them. Whatever they break is now outside every later ticket's scope and will
> be excused rather than fixed.`

`baseline-lint` then failed with the ticket's own 7 findings at the start of
the next cycle. It recovered here because the same ticket owned those files and
fixed them, but a backlog where the next cycle belongs to a different ticket
inherits the debt as pre-existing and excuses it — which is exactly the amnesty
the comment in `state.py` around `charged_failures` warns about, now observed.

### What the repairs were

**The compile gate could not count what it was looking at.** The root cause was
not the gate: `signatures()` returned the empty set for the entire lint run,
because `_ERROR` in `forge/failures.py` had no pattern for
`path:line:col: E501 message` — the word *error* appears nowhere on such a
line, so a whole flake8 run parsed to zero diagnostic blocks. That module's own
docstring says an empty result means *cannot attribute*, never *no errors*, and
both callers were reading it the other way round. Two repairs:

- `_ERROR` learned the lint shape, case-guarded against the module's
  `IGNORECASE` so `notes.md:12:1: ab12` is still prose. Every finding is now a
  signature, distinct per file — which is what baseline amnesty rests on, and
  which was blind for every lint failure in every language until now.
- The gate stops treating an empty signature set as a count. Where nothing
  parses it compares the collapsed output against the previous turn's instead,
  so *the tool said the same thing twice* is the stall test rather than
  *0 >= 0*.

The fix reaches classification as well, and improves it: the run recorded every
finding as the single class `lint : e5# line too long # # characters`, a masked
message covering `E501` at any width in any file. Named from the block, two
files are two classes — `lint E501 in ./tests/stream_test.py` and
`lint E501 in ./wordcount/stream.py`. The blind spot above is narrower for it
and still real: classes still do not count instances.

**Quarantine was never on.** `_quarantine` refuses to revert without a
`baseline_tree`, `baseline_tree` comes from `_snapshot`, and `_snapshot`
returns `""` outside a git repository — which every copy of the fixture was,
because `sample_workspace.py` copies the tree and `.git` is in its skip list.
So all nine fixture runs ran with quarantine silently off, and the warning that
says so is only printed after a ticket has already failed. Two repairs:

- `copy_sample` now runs `git init`, `git add -A` and one commit, so a copy is
  a repository with the fixture as its first commit. `repo=False` is there for
  inspecting the copy itself.
- `forge doctor` reports a project that is not a repository, and says what
  stops working, before a run is started rather than after one has failed.

---

## Round two: what the repairs oblige us to re-measure

Written before the runs, same as round one.

All three repairs land on the mechanisms round one was measuring, so its
numbers describe a loop that no longer exists in exactly the respects the
experiment was about.

| repair | what it moves |
|---|---|
| `signatures` parses lint output | the compile gate counts findings instead of `0` |
| classes are per-code-per-file | `cycle_classes` is no longer one masked string |
| copies are git repositories | quarantine reverts a failed cycle instead of leaving it |

### What round one established that still stands

- **Feature 6.** The learning slot preserved *"the verify lint rejects E501 for
  lines longer than 50 characters in this repository"* across a cycle boundary
  and the next cycle used it. Nothing repaired since touches that path.
- **24 findings blind against 4 shown.** A property of the executor and the
  prompt, not of the parser.

### What it no longer establishes

- **The `FLAT` finding is retracted pending re-measurement.** It rested on
  every finding collapsing into the single class
  `lint : e5# line too long # # characters`. Named from a parsed block, 24
  findings across two files are **two** classes and 7 in one file is **one**,
  which reads `DESCENDING`. The general claim — that classes do not count
  instances, so 7 → 7 in one file is flat while the ticket may be converging —
  survives the repair. The demonstration does not.
- **Three attempts against one.** One of `arm-blind`'s three was charged by the
  `0 >= 0` bug, so the ratio is contaminated and Feature 1's cost saving has to
  be re-measured on a gate that can count.

### The predictions

**`arm-blind` at `maxAttempts: 3` — 1 charged attempt, not 3.** This is the
sharp one. The gate can now count, so the turn sequence should be: turn 1 sees
two signatures and does not charge; turn 2 sees one, which is fewer, so it is
not a stall and does not charge either; turn 3 lands. `inner_turns` is 3 and
all of this happens inside one attempt. If instead turn 3 still fails, `spent`
reaches the limit, the attempt is charged, and the next runs ungated — so 2
charged attempts is the other plausible outcome and the fork worth watching.

**`arm-shown` at `maxAttempts: 3` — 1 charged attempt, as before.** Four
findings in one file, fixed in one gated turn. What is being re-measured here
is the *margin*: with the gate no longer inflating the blind side, the honest
statement of Feature 1's value is inner turns and tokens rather than attempts.

**`arm-blind` at `maxAttempts: 2` — no requeue, and `_measure_cycle` does not
run.** This follows from the first prediction and is worth stating baldly
because it is unflattering: the only time this fixture has ever reached
`_measure_cycle` was on the strength of a bug, and fixing the bug takes it
away. If `arm-blind` now lands inside one charged attempt, it lands at
`maxAttempts: 2` as well, cycle one, nothing requeued.

That would leave the convergence machinery unexercised again, and for a better
reason than any of the previous eight runs: not that the work is too easy, but
that the gate below it now absorbs the failure it was meant to escalate.
Quarantine being on for the first time is the other half — a failed cycle no
longer leaves its own breakage behind for the next one to inherit.

**What would change the conclusion.** If `arm-blind` at 2 does requeue, the
things to read are the `_convergence` verdict and `cycle_classes`. Two classes
in cycle one against one in cycle two is `DESCENDING`, and the ladder correctly
leaves a converging ticket alone — which is the detector working, and the
opposite of round one's reading.

---

## Round two: the results

Every prediction held, including the unflattering one.

| | `arm-blind` @3 | `arm-shown` @3 | `arm-blind` @2 |
|---|---:|---:|---:|
| findings on the first lint | 24 | 4 | 24 |
| gated inner turns | 2 | 1 | 2 |
| **charged attempts** | **1** (was 3) | 1 | **1** |
| builds | 3 | 2 | 3 |
| calls | 9 | 8 | 9 |
| tokens | 79.0k | 63.9k | 76.7k |
| seconds | 642 | 541 | 617 |
| retry cycles | 0 | 0 | **0** (was 1) |
| delivered code | all nine criteria | all nine criteria | all nine criteria |

### The gate can count now, and that was the whole of it

```
inner turn 1 of 3, 24 error(s) to answer.
inner turn 2 of 3, 7 error(s) to answer.
```

Round one logged `0 error(s)` for both. `7 < 24` is not a stall, so the second
turn is granted instead of the attempt being charged, and the third build
lands. Two of `arm-blind`'s three charged attempts in round one were the
parser's blindness, not the executor's.

### `_measure_cycle` did not run, and the reason is the repair

`arm-blind` at `maxAttempts: 2` produced byte-identical build sizes to the same
arm at 3 — `[4468, 4942, 5185]` — one charged attempt, no requeue,
`flat_cycles: 0`, `cycle_classes: []`, `learned: []`.

So the prediction stands as written: **the only time this fixture has reached
`_measure_cycle` was on the strength of a bug, and fixing the bug took it
away.** Feature 6's live evidence goes with it — there is no failed cycle for
the learning slot to record from, so `learned` is empty where round one had the
E501 rule in it. Round one's observation of both mechanisms was real; what
produced the conditions for it was a defect.

Quarantine had nothing to revert for the same reason: no cycle failed. Both
blind arms end with nine files changed against their first commit, which is the
ticket's own delivered work.

### What Feature 1 is actually worth

With the gate no longer inflating the blind side, the honest comparison is not
attempts — both arms take one. It is everything upstream of that:

- **six times the findings** to clear, 24 against 4
- **one extra gated turn and one extra build**, 3 against 2
- **19% more tokens** and **19% more wall clock**

Real, and a good deal smaller than round one's `3 attempts against 1` made it
look. That number was measuring the parser defect.

### Where this leaves the convergence machinery

Unexercised, after eleven runs, and now for the most interesting reason yet.
It is not that the work is too easy and not that the specs are too good. It is
that every mechanism below the ladder absorbs the failure the ladder was built
to escalate: the compile gate answers a lint failure inside the attempt, the
learning slot and respec answer it across a cycle, and ratification answers a
defective spec before a build call is spent.

The remaining route to it is the one round one already named — a defect whose
**failure text does not describe it**, so there is nothing for the gate to
count down and nothing for the learning slot to write. `E501 line too long
(52 > 50 characters)` hands over the rule. `TS2532 object is possibly
undefined` names a symptom whose cause is a compiler flag in another file.

---

## Round three: stop asking a model to fail

Eleven live runs landed on the first attempt. That is not a fixture that needs
sharpening — it is the answer. Every mechanism below the ladder absorbs the
failure the ladder exists to escalate, and getting a repeating failure past all
of them needs an executor bad enough that the result says nothing about the
loop.

So the failures are scripted and the *output* is recorded. `tests/recorded.py`
drives a real `Orchestrator` over a real `Store` with the model and shell
replaced, and feeds it details lifted verbatim from these runs' databases by
`scripts/harvest_recording.py`. Neither half is new — the suite has scripted
models throughout, and §9 of [LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) has
always said to write fixtures from recordings. Putting them together is what
was missing, and it is exactly the seam both defects above hid in: every
existing fixture said `src/a.ts(4,1): error TS2532: x`, which parses, while a
real `flake8` run parsed to nothing.

`tests/recordings/blind-lint-stall.json` holds four steps of `arm-blind`: the
24-finding lint, the 7-finding one, the build that returned zero bytes, and the
byte-identical 7 that followed it.

### The retracted finding, re-established in the form that survives

Replaying those three failures as three cycles:

| cycle | recorded detail | classes | verdict |
|---|---|---|---|
| 1 | 24 findings, 2 files | 2 | `FIRST` |
| 2 | 7 findings, 1 file | 1 | `DESCENDING` |
| 3 | the same 7, after an empty build | 1 | `FLAT` |

So round two's correction holds: named per code and per file, a file leaving
the set is `DESCENDING`, and round one's `FLAT` reading really was an artifact
of the parser.

**The general claim survives, and now has a demonstration.** Take that same
recorded 7-finding output and keep 3 of its lines, then 1 — the same real
findings, fewer of them, in one file:

| cycle | findings | classes | verdict |
|---|---:|---:|---|
| 1 | 7 | 1 | `FIRST` |
| 2 | 3 | 1 | `FLAT` |
| 3 | 1 | 1 | `FLAT` |

`flat_cycles` reaches **2**, which is `reviewWhenStuck`'s default, so the
ladder escalates a ticket that is converging as fast as anything in this
repository ever has. `_convergence` compares sets and nothing reads their size.

### And the ladder climbs

Driven through `_retry_cycle` with the reviewer scripted and the failures real:
three cycles of one recorded lint output fire rung one exactly once, and two
cycles of a genuinely descending pair fire it not at all.

### What this does not buy

A scripted run says the loop handles a stall correctly. Only a live run says
stalls happen. After eleven of those the honest answer is that they do not —
not with this executor, on this fixture, against any defect we have been able
to write. That is the result of the blind-grading work, and it is worth more
than another attempt at a harder ticket would have been.
