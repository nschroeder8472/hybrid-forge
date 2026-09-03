# A backlog that was ground down before it started

`SPEC.md` is ordinary work and lands. `HARD.md` is exacting work and lands
first try. `STALL.md` cannot be landed at all and stops in one attempt. This
backlog was written for the middle of `docs/CONVERGENCE.md` that none of them
reaches — a ticket that fails several times in *different* ways, so the failure
set has somewhere to descend from, `_convergence` has something to measure, and
the escalation ladder has a reason to climb a rung.

It did not reach that middle either, and how it missed is the finding. Ingest
it *instead of* `SPEC.md`, in a copy:

```
forge ingest GRIND.md
forge go
```

Expect it to end **done**, both tickets — and expect the sign-off pass to
refuse `GR-002` on its first pass.

## What the three runs of it did

All three ended `done`, both tickets, and every delivered file was checked
against every criterion independently afterwards and is correct on all of them.

| | 1 (2026-09-01) | 2 (2026-09-01) | 3 (2026-09-02) |
|---|---|---|---|
| lint | `skip` | `skip` | `flake8`, both builds |
| `GR-001` attempts | 1 | 1 | 1 |
| `GR-002` attempts | 1 | 1 | **2** |
| calls | 19 | 19 | 21 |
| tokens | 129.8k | 132.7k | 156.9k |
| seconds | 1081 | 1116 | 1417 |

`_convergence`, the ladder and `flatCycles` have still never been asked a
question. Run 3's second attempt does not reach them: `_measure_cycle` runs
over tickets eligible for a *retry cycle*, and `GR-002` finished inside its
first one.

The second run was ordered to test whether the first was luck, and it was not.
Same step sequence, same ratification shape, and three of the four delivered
files byte-identical to run 1's — including `wordcount/table.py`, which means
the planner's spec revision converged on the same asymmetric rule twice. The
only difference is in one test file: run 2's tester pinned the exact rows
`["a 34%", "b 33%", "c 33%"]` beside the sum assertion where run 1 asserted the
sum alone.

## Run 3: the linter was switched on, and it never fired

`lint` was `skip` for `.py` in both workspaces until 2026-09-02. That was the
one grading the fixture did not do, and the run every convergence feature was
derived from failed overwhelmingly on lint output — 1,125 trailing-whitespace
occurrences on one ticket, 117 of another's 160 lint failures with whitespace
as their only problem. So the expectation was that a graded run would finally
produce failing attempts.

**Every lint step passed.** Start, baseline, per-attempt, final, both builds,
both tickets — not one finding. The delivered files are clean at the configured
`max-line-length = 88` and clean at `79` as well. Whatever this executor's
weakness is, writing lint-clean Python is not it, and the hypothesis that
mechanical grading was the missing difficulty is now answered: it was not.

**What did cost an attempt was the reviewer.** `GR-002`'s first attempt met all
seven criteria and was rejected anyway. The reviewer read the ratification
settlement record, found that the tester had accepted the repaired spec by
promising *"the exact expected rows for `table({"a": 1, "b": 1, "c": 1})` so
the sum criterion can be checked deterministically"*, and observed that the test
on disk asserted only the sum:

```python
def test_equal_thirds_sum_to_one_hundred(self):
    rows = table({"a": 1, "b": 1, "c": 1})
    self.assertEqual(sum(_percent(row) for row in rows), 100)
```

— which would pass with the shortfall added to the wrong label, or the rows
misordered, so long as they summed to 100. The second attempt added
`self.assertEqual(rows, ["a 34%", "b 33%", "c 33%"])` and was accepted. That is
the `weakened_criteria` failure caught by judgement rather than by the
mechanical net, on a ticket the mechanical net had passed.

Run 3 also broke the byte-identical convergence of runs 1 and 2: both delivered
files differ from theirs. `table` grew a `_rounded_percent` helper and keyed its
shares by label rather than by position; `Stream` stored each text's token
counts in the window instead of the text. Runs 1 and 2 agreeing character for
character was a property of those two runs, not of the ticket.

**`GR-001` landed first try, three times.** It was written on the theory that difficulty the
executor cannot absorb is state over time rather than breadth: a sliding window
whose evictions have to reduce counts, drop a word that falls to zero from both
`top` and `distinct`, treat two identical texts as two entries, and leave a
list handed out earlier alone. Nine criteria, every one a *sequence* of calls.
The delivered `Stream` used a `deque`, computed each text's token counts once,
subtracted them on eviction and deleted the key when the remainder was zero,
and returned a fresh sorted list from `top` every call. All nine criteria, one
attempt, no failed step, on all three runs — and the same file, character for
character, on the first two.

So it joins `HARD.md` as a control case rather than replacing it, and it
extends that file's conclusion: difficulty that comes from care is not
difficulty for this executor, and neither is difficulty that comes from state.

**`GR-002` was not impossible, and the sign-off pass proved it.** The ticket
was written to be jointly unsatisfiable — six ordinary criteria and a seventh
asking the percentages of `table({"a": 1, "b": 1, "c": 1})` to sum to `100`,
where three equal shares of a total of three each round to `33` and sum to
`99`. The argument for impossibility was that reaching `100` requires giving a
row something other than its own rounded share, and the two criteria pinning
rows that sum to `101` forbid that.

That argument assumed the adjustment had to be symmetric, and it does not.
A rule that adds a shortfall when the rounded shares sum to *less* than `100`
and leaves them alone when they sum to more satisfies all seven: the equal-
thirds case becomes `34/33/33`, and both pinned `101` cases are untouched.

**What the loop did with it is the result worth keeping, and it reproduced.**
On both runs all four roles refused to sign the ticket on pass 1, and each
named the same defect in its own words — *each `1/3` share rounds to `33`, so the displayed percentages sum to
`99`*. The planner revised the **spec** to state the asymmetric rule, and the
criteria ratchet held: seven criteria before ratification, the same seven
after, not one of them reworded. Pass 2 was unanimous, and the build met all
seven on its first attempt. Both times, and the delivered `table.py` is
byte-identical between the runs.

That is a spec defect of a third kind, and the cheapest one to hold: `STALL.md`
is a criterion that contradicts code the ticket may not write, which no
revision inside the ticket can fix; this is a *rule* that cannot produce a
criterion, which a revision of the rule fixes without touching what the ticket
promises. The loop caught it before spending a single build call, which is the
correct place to catch it — and it is also why this backlog never reached the
convergence machinery. A defective spec that ratification can repair never
becomes a failing attempt.

**So the middle is still unexercised**, and the reason has narrowed. Reaching
it needs a defect that ratification cannot repair by revising the spec and that
the ratchet will not let it repair by revising the criteria — and that is not
this one, and not `STALL.md`, which is caught for the opposite reason. What is
left is a ticket whose spec and criteria are mutually consistent and whose
*work* the executor gets wrong several times over. Seven runs at that shape
(four of `HARD.md`, three of `GR-001`) have now landed first try, and the one
attempt any of this has ever cost came from a reviewer's judgement about a
weak test rather than from the work being hard.

## GR-001: A sliding window over recent texts

**Kind:** feature

### Spec

Add `wordcount/stream.py` with one class, `Stream`.

`Stream(window)` takes a positive integer: how many *texts* the stream
remembers. A `window` less than one raises `ValueError` whose message is
`window must be positive`.

`add(text)` splits `text` on whitespace, lower-cases each token, and adds one
to that word's count for each occurrence. It returns nothing. When the stream
already holds `window` texts, the oldest is dropped before the new one is
counted, and every word in the dropped text has its count reduced by the number
of times that text contained it. A word whose count reaches zero is no longer
a word the stream knows: it must not appear in `top`, and it must not be
counted by `distinct`.

Two identical texts are two texts. Adding `a` twice to a `Stream(2)` fills the
window with two entries, and the next `add` drops only one of them.

`top(n)` returns a list of `(word, count)` tuples: the `n` most frequent words
the stream currently holds, highest count first, ties broken alphabetically by
word. `n` larger than the number of words returns every word; `n` of zero
returns an empty list. The list belongs to the caller — a list `top` returned
earlier must not change when the stream is added to afterwards.

`distinct()` returns how many words the stream currently holds.

Use only the standard library, and import nothing from `wordcount.counter`:
this class does its own splitting.

### Allowed files

- `wordcount/stream.py`
- `tests/stream_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `Stream(0)` raises `ValueError` whose message is `window must be positive`.
- On a `Stream(2)` that has been given the one text `a b`, `top(5)` returns
  `[("a", 1), ("b", 1)]` and `distinct()` returns `2`.
- On a `Stream(2)` given the texts `a`, then `b`, then `c`, `top(5)` returns
  `[("b", 1), ("c", 1)]` and `distinct()` returns `2` — the first text was
  dropped, and `a` fell to zero, so the stream no longer holds it.
- On a `Stream(2)` given the text `a` twice, `top(5)` returns `[("a", 2)]`;
  after that same stream is given `b`, `top(5)` returns
  `[("a", 1), ("b", 1)]` — the two identical texts were two entries and only
  one of them was dropped.
- On a `Stream(2)` given the text `a`, the list returned by `top(5)` is
  `[("a", 1)]`, and it is still `[("a", 1)]` after that stream is given `a` a
  second time, when a fresh `top(5)` returns `[("a", 2)]`.
- On a `Stream(1)` given the one text `b a a c`, `top(5)` returns
  `[("a", 2), ("b", 1), ("c", 1)]` and `top(2)` returns
  `[("a", 2), ("b", 1)]`.
- On a `Stream(1)` given the one text `A a`, `top(5)` returns `[("a", 2)]`.
- On a `Stream(3)` that has been given nothing, `top(5)` returns `[]` and
  `distinct()` returns `0`.
- On a `Stream(1)` given the one text `a`, `top(0)` returns `[]`.

## GR-002: A percentage table

**Kind:** feature

### Spec

Add `wordcount/table.py` with one function, `table(counts)`. It takes the
mapping `count_words` returns and returns a list of strings, one per word.

Each row is `{label}{padding} {percent}%` — the word, spaces enough to make
every label occupy the width of the longest label, exactly one space, then that
word's share of the total written as a whole number and a `%` sign.

A share is that word's count as a percentage of the total of all counts,
rounded to a whole number half away from zero: a value of exactly `12.5`
becomes `13`. Python's built-in `round` rounds that to `12`, so use
`decimal.Decimal` with `ROUND_HALF_UP` from the standard library.

Rows are ordered by count, highest first, ties broken alphabetically by label.
An empty mapping returns an empty list.

Use only the standard library, and import nothing from `wordcount.counter` —
the function takes the mapping it is given.

### Allowed files

- `wordcount/table.py`
- `tests/table_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `table({"a": 1, "b": 1})` returns `["a 50%", "b 50%"]`.
- `table({"apple": 3, "b": 1})` returns `["apple 75%", "b     25%"]` — `b` is
  padded to the width of `apple`.
- `table({"b": 1, "a": 1})` returns `["a 50%", "b 50%"]` — equal counts are
  ordered alphabetically whatever order the mapping is in.
- `table({"a": 1, "b": 1, "c": 4})` returns `["c 67%", "a 17%", "b 17%"]`.
- `table({"a": 1, "b": 7})` returns `["b 88%", "a 13%"]` — `12.5` rounds away
  from zero, which `round` does not do.
- The percentages `table({"a": 1, "b": 1, "c": 1})` shows sum to exactly
  `100`.
- `table({})` returns `[]`.

## Why GR-002 reads as impossible and is not

Kept because the reasoning is the fixture's, not the loop's, and the loop
disproved it.

The sixth criterion asks for three equal shares of a total of three. Each is
`33.333…`, each rounds to `33`, and three of those sum to `99`. The fourth and
fifth criteria pin rows whose shares sum to `101` — `67 + 17 + 17` for a total
of six, and `88 + 13` for a total of eight — so a rule that scales every row
towards a total of `100`, largest-remainder apportionment included, has to
lower one of those pinned numbers and fails.

The step missing from that is that no criterion requires the correction to run
in both directions. Add the shortfall when the rounded shares fall short of
`100`, do nothing when they overshoot, and all seven hold at once: `34/33/33`
for the equal thirds, `67/17/17` and `88/13` untouched. The rule is asymmetric
and slightly ugly, and the spec as written did forbid it — which is a defect in
the *spec's rule*, not in the criteria, and is repairable without weakening
anything the ticket promises.

`tests/test_sample_project.py` pins that: the seven criteria are proved jointly
satisfiable there by the asymmetric rule, and the `99` and the two `101`s are
computed to show why the symmetric reading fails.
