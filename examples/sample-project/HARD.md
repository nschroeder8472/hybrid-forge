# A backlog that is exacting, and lands anyway

`SPEC.md` is ordinary work. `STALL.md` cannot be landed at all. This one was
written for the space between them — several attempts, a failure set that
shrinks rather than churns, `_convergence` measuring it, the escalation ladder
climbing a rung — and it did not find that space. It is kept as the control
case instead: the most exacting spec in the fixture, landed first try.

Ingest it *instead of* `SPEC.md`, in a copy:

```
forge ingest HARD.md
forge go
```

Expect it to end **done**. It was written expecting several attempts, and it
has not taken them: four runs, two versions of it — four exact details, then
nine — and a local Qwen3.8 landed every criterion on the first attempt each
time, in seven model calls. That is the measurement rather than a
disappointment, and it is why this file stays in the fixture: it is the control
case, the run that says a well-specified ticket needs none of the machinery in
`docs/CONVERGENCE.md`.

If a change to the loop makes *this* backlog take two attempts, the change is
the thing to look at.

The difficulty is breadth, not obscurity. One function, seven independent
details, each easy to get slightly wrong and each pinned by a criterion that
says exactly what the answer is. Nothing here needs a library the spec does not
name, an algorithm, or a design decision — only care, and holding seven things
right at once.

An earlier version of this backlog named four details and was landed on the
first attempt, in seven calls. Withholding the library would have made it
harder and the spec worse; adding surface made it no harder at all, which is
the more interesting result: nine exact criteria, seven independent details,
right the first time. Difficulty that comes from care is not difficulty for
this executor. What defeats it is a spec that is *wrong* — which is what
`STALL.md` is for.

## HP-001: Report each word's share of the text

**Kind:** feature

### Spec

Add `wordcount/shares.py` with one function, `shares(counts, limit=0)`. It
takes the mapping `count_words` returns and returns a list of strings: one row
per word shown, then one summary line last.

**The rows.** Each row is `{label}{padding} {percent}` where:

- `percent` is that row's count as a percentage of the total of *all* counts —
  including the counts of words the `limit` leaves out — rounded to one decimal
  place, always written with exactly one decimal place and a trailing `%`:
  `50.0%`, not `50%`, and `16.7%`, not `16.67%`.
- Rounding is half away from zero: a value of exactly `16.25` becomes `16.3`.
  Python's built-in `round` does not do this, so use `decimal.Decimal` with
  `ROUND_HALF_UP` from the standard library.
- The percentage is **right-aligned in a field six characters wide**, counting
  the `%`. `100.0%` fills it exactly; `50.0%` is five characters and gets one
  leading space.
- `padding` is spaces enough to make every label occupy the width of the
  longest label *shown*, and exactly one space separates the padded label from
  the percentage field.
- Rows are ordered by count, highest first, ties broken alphabetically by
  label.

**The limit.** `limit` of `0` shows every word. A `limit` greater than zero
shows the top `limit` words, followed by one extra row labelled `other` whose
count is the sum of every word left out. That row is always last among the
rows, whatever its count, and its label counts towards the padding width. A
`limit` at least as large as the number of words adds no `other` row.

**The summary line.** The last element of the list is
`{words} word(s), {total} occurrence(s)` — the number of distinct words in
`counts`, then the total of all counts, each with its noun in the singular when
it is exactly one: `1 word, 1 occurrence`, but `2 words, 3 occurrences`. The
summary counts every word, including any the `limit` folded into `other`, and
it is not padded or aligned.

An empty mapping returns an empty list, with no summary line. Use only the
standard library, and import nothing from `wordcount.counter` — the function
takes the mapping it is given.

### Allowed files

- `wordcount/shares.py`
- `tests/shares_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `shares({"a": 1, "b": 1})` returns
  `["a  50.0%", "b  50.0%", "2 words, 2 occurrences"]` — one decimal place is
  written even when it is zero, and `50.0%` is right-aligned in six characters,
  so two spaces separate `a` from it.
- `shares({"apple": 2, "b": 1})` returns
  `["apple  66.7%", "b      33.3%", "2 words, 3 occurrences"]` — `b` is padded
  to the width of `apple`.
- `shares({"a": 1, "b": 2, "c": 3})` returns
  `["c  50.0%", "b  33.3%", "a  16.7%", "3 words, 6 occurrences"]` — highest
  share first.
- `shares({"b": 1, "a": 1, "c": 1})` returns
  `["a  33.3%", "b  33.3%", "c  33.3%", "3 words, 3 occurrences"]` — equal
  counts are ordered alphabetically.
- `shares({"a": 13, "b": 67})` returns
  `["b  83.8%", "a  16.3%", "2 words, 80 occurrences"]` — `16.25` rounds away
  from zero.
- `shares({"a": 1})` returns `["a 100.0%", "1 word, 1 occurrence"]` — the
  percentage fills the six-character field, leaving one space after the label,
  and both nouns are singular.
- `shares({"a": 5, "b": 3, "c": 2, "d": 1}, 2)` returns
  `["a      45.5%", "b      27.3%", "other  27.3%", "4 words, 11 occurrences"]`
  — `c` and `d` are folded into `other`, whose label sets the padding width,
  and the summary still counts all four words.
- `shares({"a": 2, "b": 1}, 5)` returns
  `["a  66.7%", "b  33.3%", "2 words, 3 occurrences"]` — a limit larger than
  the number of words adds no `other` row.
- `shares({})` returns `[]`.
