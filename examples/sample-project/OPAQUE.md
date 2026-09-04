# A backlog whose failures do not name their cause

Every other backlog in this fixture has now been run, and all of them landed:
`SPEC.md` and `HARD.md` on the first attempt, `GRIND.md` on the first attempt
twice and after one reviewer-spent attempt on the third run, `STALL.md` parked
in one attempt without ever failing a build. Eleven runs, and `_convergence`,
`flatCycles` and the escalation ladder have never been asked a question by any
of them.

The reading of that in `docs/ROADMAP.md` is not that the tickets were too easy.
It is that **every mechanism below the ladder absorbs the failure the ladder
exists to escalate**: the compile gate answers a lint failure inside the
attempt, the learning slot and the respec answer it across a cycle, and
ratification answers a defective spec before a build call is spent. What none
of them can absorb is a failure whose *text does not describe its cause*.

That is the difference between the two runs this repository learns from.
`E501 line too long (52 > 50 characters)` hands over the rule: the failure says
what is wrong, what the limit is, and where. `TS2532 object is possibly
undefined` names a symptom whose cause is a compiler flag two files away, and
an executor shown that repeatedly will keep repairing the line it points at.
The reference run spent 430 attempts on failures of the second kind.

This backlog is written to produce failures of the second kind. Ingest it
*instead of* `SPEC.md`, in a copy:

```
forge ingest OPAQUE.md
forge go
```

Expect it to end **done**, and expect the sign-off pass to refuse it on its
first pass and ask for the rule by name. It was written to end done *slowly*,
by failing in ways that do not explain themselves; three runs say the loop does
not let that happen. See the run sections below.

## The mechanism

`OP-001` asks for a second renderer in the plugin build whose bars must match
the bars `histogram.bars` already draws. It may not call `bars`, and
`plugin/histogram/bars.py` is not in its reading scope — it lives in a
different directory from every file the ticket may write, which is what it
takes (see *Run 1* below) — so the rule that decides a bar's length is nowhere
in the prompt:

```python
f"{word} {'#' * (count * width // tallest)}"
```

Multiply first, then floor. Every natural alternative — `round`, a float
division, clamping a visible word to at least one `#` — agrees with it on most
inputs and disagrees on a few, and the criteria name three mappings where the
disagreement shows. The sharpest of them is `{"a": 5, "b": 3, "c": 1}` at
`width=4`, where the rule renders `c` with **no bar at all**: a result most
implementations will treat as a bug in their own code and repair, which is the
wrong repair.

What the executor is shown when it gets this wrong is an assertion mismatch:

```
AssertionError: 'c  x1' != 'c # x1'
```

There is no rule in that line. There is no file named in it that the executor
can open, because the file that decides the answer is not in its scope. There
is one character of difference and a suggestion that something adjacent is
wrong — which is the shape the ladder exists for, and the shape no backlog here
has produced yet.

## What is deliberate, and what is not

**Deliberate:** `bars` is not a reference file, and calling it is forbidden.
The ticket's reason for that is a real one — `legend` is written as the
replacement for `bars`, so depending on it would defeat the exercise — and it
is also what withholds the rule.

**Deliberate:** `plugin/tests/bars_test.py` *is* a reference file. It gives the
ordering rule and the full-width rule, so the ticket is neither a guessing game
nor unsatisfiable: everything except the scaling arithmetic is in the prompt,
and the scaling arithmetic is decided by criteria the tester can assert
mechanically. A sign-off pass reading this should have nothing to object to,
which is the point — `GR-002` never reached a build call because ratification
correctly refused it.

**Not deliberate, and worth checking on any run:** nothing here is meant to be
unsatisfiable. If it parks, the finding is about the loop, not about the spec.
`legend` can be written correctly from the criteria alone by anything that
converges on floor division, and the guard suite pins that the three mappings
really do separate flooring from rounding — if somebody changes `bars` to round,
this backlog quietly stops testing anything.

## What to read afterwards

The run's own numbers, and then:

- **Attempts on `OP-001`.** One means it inferred the rule immediately and this
  backlog joins `HARD.md` as a control case rather than as the ladder's first
  live exercise. `maxAttempts` is 3 in this fixture's config, so a fourth
  failure is a requeue, and only a requeue reaches `_measure_cycle`.
- **Whether `_convergence` ever ran, and what it said.** A cycle of failures
  that are all the same assertion in the same file is one class, so the class
  set cannot move: what moves is the *number* of mismatching rows, which is the
  signal `cycle_volume` was added for. A cycle that fixes two of three
  disagreements should read `descending`, not `flat`.
- **What the learning slot recorded.** A cycle that ends knowing "the bar
  length is the count times the width, floored, and a word can render no bar"
  has recorded the rule the failure text never carried. That is Feature 6 doing
  the job it was built for, on the only fixture that has ever asked it.
- **Whether the reviewer was asked.** Rung one of the ladder fires at
  `flatCycles` 2 by default. A `winnable` verdict there is correct and worth
  seeing; an `unwinnable` verdict on a ticket that is satisfiable is a finding
  about the reviewer.

## Run 1 (2026-09-03): the loop handed over the rule before the first attempt

**done, one attempt, 7 calls, 50.3k tokens, 468 seconds.** Ratification was
unanimous on pass 1, the build landed on its first try, the suite went green
with nine tests and the reviewer approved. The trap never fired, and why it did
not is the finding.

The ticket declared one reference file. What the executor was actually shown
was four:

```
reference_files: plugin/tests/bars_test.py, plugin/histogram/__init__.py,
                 plugin/histogram/bars.py, plugin/tests/__init__.py
```

`evidence.reading_scope` widens a ticket's read scope with **source siblings of
every writable file** — deliberately, and for a good reason its own docstring
gives: *a fix almost always has to stay consistent with the module next to it*.
`legend.py` was to be written in `plugin/histogram/`, so `bars.py` came with
it. The delivered file's docstring is `bars.py`'s docstring, near enough
verbatim, down to *"scaled against it and rounded down"*, and its arithmetic is
`count * width // tallest` on the first attempt.

Nothing about that is a defect. It is the loop being right about ordinary work,
and this backlog being wrong about how a file is kept out of a prompt: reading
scope is not what a spec declares, it is what a spec declares plus what the
directory implies. **A ticket cannot withhold a same-directory sibling.**

So `legend` moved to a package of its own, `plugin/histogram/render/`, where
sibling expansion reaches nothing. The guard suite now pins that by running
`reading_scope` over the ticket's own scope and asserting `bars.py` is not in
the result — the assumption this run disproved, checked against the code that
decides it rather than against the document that assumes it.

## Run 2 (2026-09-03): the sign-off pass asked for the rule, in writing

**failed, two cycles, six attempts, 27 calls, 1,778 seconds.** With `legend`
moved into `histogram/render/`, sibling expansion no longer reached `bars.py`
— and ratification refused the ticket on its first pass, naming exactly what
had been taken away:

> *The acceptance criterion requires matching histogram.bars for non-exact
> scaling, but the ticket neither lists plugin/histogram/bars.py as readable
> nor specifies the integer scaling/rounding rule; add the file to reference
> files or state the rule.* — the planner, blocking

The executor role blocked on the same point, the tester suggested it, and the
repair added `plugin/histogram/bars.py` to the reading scope. The criteria
ratchet did its own job in the same pass: the revision tried to grow the
criteria from five to six and was refused.

**So there is a second defence, and it is a better one.** Sibling expansion
hands over a neighbouring file silently; the sign-off pass reads the criteria,
notices that one of them cannot be met without a rule nobody stated, and says
so before a build call is spent. Between them, this loop will not accept a
ticket whose failures could not describe their own cause. That is the finding
this backlog exists to produce, and it is a stronger result than the stall it
was aiming for.

**What it failed on was a defect of this document's own making.** The spec
asked for an *empty* `__init__.py`. Written as a blank line that is
`W391 blank line at end of file`, and the executor's repair — a docstring —
was rejected by the reviewer for not being empty. Six attempts across two
cycles went almost entirely into that oscillation, and `blocked_note` ends on
it. The spec now asks for a one-line docstring, as every other package in this
fixture has.

Two things worth keeping from the wreckage:

- **The learning slot fired, and recorded a misgeneralisation.** What the cycle
  wrote down was *"the linter in this repo enforces W391; a file must not end
  with a trailing newline"* — W391 is about a *blank* line, not the newline
  every file here ends with. A learning that travels with every later attempt
  is worth reading for that reason as well as for the obvious one.
- **`_measure_cycle` ran on a live run for the first time**, recorded three
  classes and `cycle_volume` 1, and carried the volume across the requeue.
  `flat_cycles` stayed 0 because the class set changed between the cycles,
  which is the detector answering correctly.

## Run 3 (2026-09-03): done in one attempt, with the rule handed over again

**done, one attempt, 12 calls, 89.3k tokens, 850 seconds.** With the empty
`__init__.py` replaced by a one-line docstring, the oscillation was gone and
the ticket landed on its first attempt. Every criterion was checked
independently against the delivered file afterwards, including the line where a
counted word renders no bar at all, and all of them hold.

Ratification refused it on pass 1 again — the executor role blocking with the
same objection run 2 produced, in almost the same words — and the repair added
`plugin/histogram/bars.py` to the reference files again. Pass 2 carried on a
**majority**: the reviewer still dissented, on the ground that the scaling rule
is not pinned for inputs outside the three listed mappings, which is a fair
reading of a spec that deliberately does not state it.

So the executor was shown `count * width // tallest` before it wrote a line,
for the third time and by the third different route. Its docstring says
*"scaled against it and rounded down"*.

## What three runs establish

The backlog set out to produce a failure whose text does not name its cause,
and could not. Not because the executor is too good — run 2 shows it grinding
for six attempts on a contradiction — but because **this loop will not accept a
ticket whose failures could not describe their own cause**. It has two
independent ways of refusing:

| | route | when | evidence |
|---|---|---|---|
| 1 | `evidence.reading_scope` adds the source siblings of every writable file | before the first prompt | run 1 |
| 2 | the sign-off pass blocks and names the missing rule | before the first build call | runs 2 and 3 |

The first is silent and the second is not, which makes the second the better
one. Neither is a defect: the first is right about ordinary work, and the
second is ratification doing exactly the job `docs/CONVERGENCE.md` built it for
— on a spec defect that no test could have caught, because the spec was
internally consistent and merely incomplete.

**What this means for the ladder.** The escalation ladder is still unexercised
by any live run, and this backlog is now evidence about *why* rather than
another failed attempt to reach it. The shape that would reach it — repeated
failures whose text misdescribes their cause — is a shape the loop refuses at
ingest-adjacent stages. Reaching it live would take turning ratification off
(`loop.ratifyPasses: 0`) and putting the module somewhere sibling expansion
cannot see, which is a fixture arguing with two of its own safety mechanisms;
whether that is worth running is a decision for whoever picks this up. The
replay in `tests/test_recorded_output.py` exercises the ladder directly and
costs nothing.

**Expect this backlog to end done, in one or two attempts, with a ratify
refusal on the first pass.** That refusal is the interesting artifact — read
`ratify_notes`, not the verdict.

**Everything above the run sections is what the backlog was designed to do,**
which — as `docs/LOOP-INVARIANTS.md` §9 says about every argument derived from
reading the code rather than from watching it — is the weak kind of claim.
Three runs in, its central assumption has been disproved three times, by two
different mechanisms. That is what the runs were for.

## OP-001: Draw the histogram with its counts

**Kind:** feature

### Spec

Add `plugin/histogram/render/legend.py` with one function,
`legend(counts, width=20)`, importable as `histogram.render.legend`. Add
`plugin/histogram/render/__init__.py` beside it so the new package imports
cleanly; it holds a one-line docstring and nothing else, as the packages beside
it do.

`legend` takes a mapping of word to count and returns a list of strings, one
per word, formatted as `{word} {bar} x{count}` — the word, one space, the bar,
one space, the letter `x`, the count.

The bar is a run of `#` characters. Its length is scaled so that the word with
the highest count gets a bar of exactly `width` characters and every other word
gets a bar in proportion to it.

Order the lines by count, highest first, with ties broken alphabetically by
word. An empty mapping renders nothing.

`legend` is written as the replacement for `bars`, so it must not depend on it:
do not import from `histogram.bars` and do not call `bars`. Compute the bar
lengths in `legend` itself. Use only the standard library.

### Allowed files

- `plugin/histogram/render/legend.py`
- `plugin/histogram/render/__init__.py`
- `plugin/tests/legend_test.py`

### Reference files

- `plugin/tests/bars_test.py`

### Acceptance criteria

- `legend({"a": 2}, width=4)` returns `["a #### x2"]`.
- `legend({"b": 1, "a": 1}, width=1)` returns `["a # x1", "b # x1"]` — equal
  counts are ordered alphabetically by word.
- `legend({})` returns `[]`.
- `legend({"a": 4, "b": 2}, width=4)` returns `["a #### x4", "b ## x2"]`.
- For each of the mappings `{"a": 3, "b": 2}`, `{"a": 5, "b": 3, "c": 1}` and
  `{"a": 3, "b": 1, "c": 2}` at `width=4`, every line of
  `legend(counts, width=4)` carries the same run of `#` characters, in the same
  order, as the corresponding line of `bars(counts, width=4)` from
  `histogram.bars`. The test may import `bars` to check this; `legend` may not.
