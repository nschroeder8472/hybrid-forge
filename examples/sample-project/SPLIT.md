# A backlog whose roles should not all agree

`GRIND.md`'s `GR-002` is refused by every role for one reason, and `HARD.md` is
signed by every role on the first pass. Both are unanimous, and a unanimous
pass returns before a second one happens. So neither can exercise the case
`loop.majorityRepeats` is about: a pass-1 **majority**, where the ticket ships
on three votes of four and one role says it cannot do its part.

That case is the most common non-unanimous outcome in the recorded evidence —
three of the eight passes that reached a second vote started from it, and not
one of them ended anywhere other than where it started. Whether re-asking is
worth its cost cannot be measured on a fixture that never produces one.

```
forge ingest SPLIT.md
forge go
```

## What it is built to do

The work is deliberately ordinary: one function, standard library, no rounding
trap, no interaction with anything else in the project. Nothing here should
give the executor, the reviewer or the planner any reason to refuse.

The last acceptance criterion is the whole fixture. It asks for an
*implementation detail* — which library function the body uses — and nothing
about that choice is visible in what `longest_run` returns, so no assertion
over its behaviour can tell a `groupby` implementation from a hand-written
loop. The executor can comply without difficulty. The reviewer can check it by
reading the diff, which is what a reviewer does. The planner wrote it. Only the
role that has to turn it into a test has nowhere to go.

## It does not work, and why is the finding

**Two versions, two unanimous passes.** The fixture has not produced a majority
and is kept for what it shows about why.

The first version asked for a wall-clock bound — `longest_run` returns within 5
milliseconds for a list of 1,000 entries — on the theory that a timing
assertion is a property of the machine rather than the code and a tester would
refuse to encode it. All four roles signed on pass 1. A bound a test can
*approximate* badly is not a bound a tester refuses.

The second is the criterion above, and it failed more usefully. The tester
found exactly the flaw it was planted to find:

> The `itertools.groupby` criterion is implementation-detail and only
> assertable by source/mock inspection; consider removing it

and then **signed**, filing that as a `SUGGEST` rather than a `BLOCKING`. Which
is the protocol behaving correctly and the fixture's premise being wrong.
`BLOCKING` means *I cannot do my part as written*; `SUGGEST` means *this could
be better*. A role that can proceed while objecting signs, and a criterion no
test can assert does not stop a tester writing the other five.

So a pass-1 majority needs a genuine impossibility that lands on exactly one
role — and the impossibilities a spec actually contains, like `GR-002`'s
arithmetic, are visible to all four at once, which resolves `blocked` rather
than `majority`. That is consistent with where the recorded majorities came
from: real backlog tickets against a large tree, not a four-file fixture.

Anyone picking this up should treat a third guess at the criterion as the least
promising route. The evidence for `loop.majorityRepeats` is the replay over
recorded passes, and what would improve it is a majority from a real backlog,
not a manufactured one.

## SP-001: A longest-run report

**Kind:** feature

### Spec

Add `wordcount/runs.py` with one function, `longest_run(words)`. It takes a
list of strings and returns a tuple `(word, length)` naming the longest run of
consecutive identical entries.

A run is one or more entries in a row that are equal. The longest run wins;
ties are broken by whichever run starts earliest in the list. An empty list
returns `("", 0)`.

Use only the standard library, and import nothing from `wordcount.counter` —
the function takes the list it is given.

### Allowed files

- `wordcount/runs.py`
- `tests/runs_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `longest_run(["a", "a", "b"])` returns `("a", 2)`.
- `longest_run(["a", "b", "b", "b", "a"])` returns `("b", 3)`.
- `longest_run(["a", "a", "b", "b"])` returns `("a", 2)` — equal-length runs
  are broken by whichever starts earliest.
- `longest_run(["a"])` returns `("a", 1)`.
- `longest_run([])` returns `("", 0)`.
- `longest_run` is implemented using `itertools.groupby`.
