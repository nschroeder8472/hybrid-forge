# A backlog that cannot succeed

The other spec in this fixture is work the loop can do. This one is not, and
that is its whole purpose: every brake in the loop — the sign-off pass, the
attempt budget, convergence, the respec, the retry cycle, the park — only ever
runs on a ticket that is going nowhere, and a fixture whose runs all finish
green never asks any of them a question.

Ingest this one *instead of* `SPEC.md`, in a copy, when a change touches
retries, respec, convergence or how a ticket is parked:

```
forge ingest STALL.md
forge go
```

Expect it to end **blocked**, not done. What is worth reading afterwards is
which brake stopped it, how much it spent getting there, and whether the note
it leaves a human names the real problem. `docs/CONVERGENCE.md` is the
document those answers belong to.

The defect is deliberate and it is a *spec* defect, not a trick: the ticket's
acceptance criteria demand behaviour from a file the ticket is not allowed to
write. That is one of the commonest ways a real backlog parks, and it is
invisible to a reader who checks the criteria and the scope separately.

## ST-001: Summarise a word count

**Kind:** feature

### Spec

Add `wordcount/summary.py` with one function, `summarize(counts)`. It takes the
mapping `count_words` returns and returns a single string:
`{distinct} word(s), {total} occurrence(s)` — the number of distinct keys, then
the sum of the values. An empty mapping returns `0 word(s), 0 occurrence(s)`.

Import nothing from `wordcount.counter`; the function takes the mapping it is
given. Use only the standard library.

### Allowed files

- `wordcount/summary.py`
- `tests/summary_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `summarize({"a": 2, "b": 1})` returns `2 word(s), 3 occurrence(s)`.
- `summarize({})` returns `0 word(s), 0 occurrence(s)`.
- `summarize({"a": 1})` returns `1 word(s), 1 occurrence(s)`.
- `count_words("Hello, world!")` returns `{"hello": 1, "world": 1}`, so that
  `summarize(count_words("Hello, world!"))` returns `2 word(s), 2
  occurrence(s)`.

## Why the last criterion cannot be met

`count_words` strips no punctuation — that is the fault `BUG.md` reports — so
it returns `{"hello,": 1, "world!": 1}`, and the fourth criterion is false. The
file that would have to change, `wordcount/counter.py`, is a reference file
here: readable, not writable. So the executor can write a perfect
`summarize` and still fail, every attempt, in exactly the same way.

Nothing about the ticket is malformed. The criteria are specific, testable and
individually reasonable; the scope is tight and correct for the work described.
Only the pair is wrong, which is what makes it a fair test of the brakes rather
than of the parser.
