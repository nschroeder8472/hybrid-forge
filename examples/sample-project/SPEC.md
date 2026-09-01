# Sample backlog

Three tickets, written for the parsed path: every criterion here reaches the
tester as written, and no planner model rephrases any of it. Two of them land
in the root build and one in `plugin/`, so a run has to resolve a ticket to its
own workspace and run that workspace's command from that workspace's
directory.

Keep this document small. Its job is to exercise the loop end to end in
minutes, not to be a realistic project.

## SP-001: Rank the counted words

**Kind:** feature

### Spec

Add `top_words(counts, n)` to `wordcount/counter.py`, beside the existing
`count_words`. It takes the mapping `count_words` returns and an integer `n`,
and returns a list of `(word, count)` tuples: the `n` most frequent words,
highest count first, ties broken alphabetically by word.

`n` larger than the number of distinct words returns every word. `n` of zero
returns an empty list. A negative `n` raises `ValueError` with the message
`n must not be negative`.

Keep the existing `count_words` function exactly as it appears, character for
character. Use only the standard library.

### Allowed files

- `wordcount/counter.py`
- `tests/top_words_test.py`

### Reference files

- `wordcount/counter.py`
- `tests/counter_test.py`

### Acceptance criteria

- `top_words({"a": 3, "b": 1}, 2)` returns `[("a", 3), ("b", 1)]`.
- `top_words({"b": 2, "a": 2}, 2)` returns `[("a", 2), ("b", 2)]` — equal
  counts are ordered alphabetically by word.
- `top_words({"a": 1}, 5)` returns `[("a", 1)]`.
- `top_words({"a": 1}, 0)` returns `[]`.
- `top_words({"a": 1}, -1)` raises `ValueError` whose message is
  `n must not be negative`.
- `count_words("Apple apple")` still returns `{"apple": 2}`.

## SP-002: Report the ranking from a file

**Kind:** feature
**Needs:** SP-001

### Spec

Add `wordcount/report.py` with one function, `report(path, n=5)`. It reads the
UTF-8 text file at `path`, counts its words with `count_words`, ranks them with
`top_words`, and returns a list of strings, one per word, formatted as
`{word}: {count}` — the word, a colon, one space, the count.

A file that does not exist raises `FileNotFoundError`, which is what `open`
already does; add no handling of your own. An empty file returns an empty list.

Import both functions from `wordcount.counter`. Use only the standard library.

### Allowed files

- `wordcount/report.py`
- `tests/report_test.py`

### Reference files

- `wordcount/counter.py`

### Acceptance criteria

- `report` over a file holding `a b a` with `n=2` returns `["a: 2", "b: 1"]`.
- `report` over an empty file returns `[]`.
- `report` over a file holding `a b a` with `n=1` returns `["a: 2"]`.
- `report` raises `FileNotFoundError` for a path that does not exist.

## SP-003: Cap the histogram's labels

**Kind:** feature

### Spec

Add a `label_width` keyword argument to `bars` in `plugin/histogram/bars.py`,
defaulting to `0`. When it is greater than zero, every word is padded with
spaces on the right to that many characters before the space and the bar, so
the bars line up. A word longer than `label_width` is truncated to it.

`label_width` of `0` renders exactly what it renders today: the word, one
space, the bar.

Keep the existing ordering and scaling exactly as they are. Use only the
standard library.

### Allowed files

- `plugin/histogram/bars.py`
- `plugin/tests/label_width_test.py`

### Reference files

- `plugin/histogram/bars.py`

### Acceptance criteria

- `bars({"ab": 1}, width=2, label_width=4)` returns `["ab   ##"]` — the word,
  padded to four characters, then a space, then the bar.
- `bars({"abcdef": 1}, width=1, label_width=3)` returns `["abc #"]`.
- `bars({"a": 2, "b": 1}, width=2)` still returns `["a ##", "b #"]`.
- `bars({}, label_width=4)` returns `[]`.
