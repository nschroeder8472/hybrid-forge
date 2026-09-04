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

Expect it to end **done** — and not on the first attempt.

## The mechanism

`OP-001` asks for a second renderer in the plugin build whose bars must match
the bars `histogram.bars` already draws. It may not call `bars`, and
`plugin/histogram/bars.py` is not in its reading scope, so the rule that
decides a bar's length is nowhere in the prompt:

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

**This backlog has not been run.** Everything above is what it was designed to
do, which — as `docs/LOOP-INVARIANTS.md` §9 says about every argument derived
from reading the code rather than from watching it — is the weak kind of claim.
The first run of it is the evidence.

## OP-001: Draw the histogram with its counts

**Kind:** feature

### Spec

Add `plugin/histogram/legend.py` with one function, `legend(counts, width=20)`.
It takes a mapping of word to count and returns a list of strings, one per
word, formatted as `{word} {bar} x{count}` — the word, one space, the bar, one
space, the letter `x`, the count.

The bar is a run of `#` characters. Its length is scaled so that the word with
the highest count gets a bar of exactly `width` characters and every other word
gets a bar in proportion to it.

Order the lines by count, highest first, with ties broken alphabetically by
word. An empty mapping renders nothing.

`legend` is written as the replacement for `bars`, so it must not depend on it:
do not import from `histogram.bars` and do not call `bars`. Compute the bar
lengths in `legend` itself. Use only the standard library.

### Allowed files

- `plugin/histogram/legend.py`
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
