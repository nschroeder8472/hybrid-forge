# The dashboard says how many tokens, and not which kind

**Status:** shipped. Run 6 of this repository, 2026-09-07, built by the loop:
both tickets done on the first attempt, no failed step, 328 calls and 15.5M
tokens. Nothing below was revised on the way — the delivered code sums the
cache counters into `input`, reuses `format_tokens`, and leaves the four
existing keys alone. Kept as the record of what was asked for.

The dashboard's *Token usage* table has three columns — model, calls, tokens —
and the third is `prompt_tokens + completion_tokens + cache_creation_tokens +
cache_read_tokens` added together by `Store.usage_summary` and formatted once
by `format_tokens`. Every number a person reads about a run is that sum.

The two halves of it behave nothing alike. Input is what the loop *sends*: it
grows with the repository map, the reference files, the prior attempts replayed
as turns, and the failure classes carried forward — and on a hybrid run it is
where nearly all of the money goes. Output is what a model *writes*, it is
bounded by `maxOutputTokens` per call, and a run whose output share climbs is a
run generating more code rather than one carrying more context. Collapsed into
one number, a prompt that doubled and a model that started writing twice as
much are the same reading.

The counts are already stored per call and already summed per model. Nothing
new is measured here: `usage_summary` returns `prompt_tokens`,
`completion_tokens`, `cache_creation_tokens` and `cache_read_tokens` per model
today, and `snapshot()` throws three of them away on the way to the page.

## Design decisions

These are settled. A retry cycle may not revise them away.

Decision: input is `prompt_tokens + cache_creation_tokens + cache_read_tokens`,
and output is `completion_tokens`. Cache tokens are prompt-side charges — they
are the prompt, billed at a different rate — so they belong on the input side
of the split. A separate cache column is not added: two of the five configured
provider kinds report cache counters at all, and the local ones report zero for
every call, so a column that is empty on most runs costs a reader more than it
tells them.

Decision: the existing keys on each usage row — `total`, `display`, `cached`,
`cost_display` — stay exactly as they are, carrying exactly what they carry
now. `forge status` prints `row["display"]` and the page reads `u.display`, and
this backlog is adding a split rather than replacing a total.

Decision: the new numbers are formatted by `forge.tokens.format_tokens`, the
same function the total already goes through, so `1500` reads as `1.5k` in
every column rather than in one of them.

Decision: no new database column, no migration, and no change to
`Store.usage_summary`. Everything this needs is in the row that method already
returns.

## US-001: Carry input and output separately in the dashboard payload

**Route:** delegate

### Spec

In `forge/ui/server.py`, `snapshot()` builds one dictionary per row returned by
`store.usage_summary()`. Add four keys to each of those dictionaries, beside
the `total`, `display`, `cached` and `cost_display` keys it already adds, and
leave those four exactly as they are:

- `input` — the integer `prompt_tokens + cache_creation_tokens +
  cache_read_tokens` for that row.
- `output` — the integer `completion_tokens` for that row.
- `input_display` — `format_tokens(input)`, using the `format_tokens` already
  imported at the top of the module.
- `output_display` — `format_tokens(output)`.

`format_tokens` is imported in `forge/ui/server.py` as
`from ..tokens import format_tokens`; keep using that import rather than
adding another.

A row whose counters are all zero carries `input` `0`, `output` `0`, and both
display strings as `"0"`.

### Allowed files

- `forge/ui/server.py`
- `tests/test_usage_split.py`

### Reference files

- `forge/tokens.py`

### Acceptance criteria

- For a store holding one call with `prompt_tokens` 100, `completion_tokens`
  20, `cache_creation_tokens` 5 and `cache_read_tokens` 7, the usage row in
  `snapshot()` has `input` 112 and `output` 20.
- For that same row, `input` plus `output` equals the row's existing `total`.
- For a store holding one call with `prompt_tokens` 1500 and every other
  counter 0, the usage row has `input_display` `"1.5k"` and `output_display`
  `"0"`.
- For two calls against the same model, the usage row's `input` is the sum of
  both calls' input counters and its `output` is the sum of both calls'
  completion counters.
- The usage row still has its `total`, `display`, `cached` and `cost_display`
  keys, carrying the same values they carried before this ticket.
- A snapshot taken with no run and no calls returns an empty `usage` list.

### Context

`snapshot()` is polled for the life of a run, so the four keys are computed
from the row already in hand rather than by asking the store anything further.
`Store.usage_summary()` returns one dictionary per model, already summed, with
the keys `model`, `calls`, `prompt_tokens`, `completion_tokens`,
`cache_creation_tokens`, `cache_read_tokens`, `cost_usd` and `total_tokens`.

The dashboard tests in this repository build a `Store` against a temporary
path and call `ui_server.snapshot(store, config)` directly; see
`tests/test_handback_ui.py` for the shape.

## US-002: Show input and output as their own columns

**Route:** delegate

**Needs:** US-001

### Spec

In `forge/ui/index.html`, the *Token usage* section holds a table whose header
row reads `Model`, `Calls`, `Tokens`, and whose body is filled by the block
that assigns to `$("usage").innerHTML`.

Give the table five columns, in this order: `Model`, `Calls`, `In`, `Out`,
`Total`. Keep the existing `Model`, `Calls` and total cells rendering exactly
what they render now — the total cell keeps reading `u.display` — and add two
cells between the calls cell and the total cell, reading `u.input_display` and
`u.output_display`. Both new values go through the page's own `esc()` helper,
the way `u.display` already does.

The empty-state row that reads `No calls yet.` currently spans three columns;
give it `colspan="5"` so it still spans the whole table.

### Allowed files

- `forge/ui/index.html`
- `tests/test_usage_split_render.py`

### Reference files

- `forge/ui/server.py`

### Acceptance criteria

- `forge/ui/index.html` contains the text `<th>In</th>` and the text
  `<th>Out</th>`.
- `forge/ui/index.html` contains the text `esc(u.input_display)` and the text
  `esc(u.output_display)`.
- `forge/ui/index.html` still contains the text `esc(u.display)`.
- `forge/ui/index.html` contains the text `colspan="5"` and does not contain
  the text `colspan="3"`.
- The header cells `Model`, `Calls`, `In`, `Out` and `Total` appear in
  `forge/ui/index.html` in that order.

### Context

The page is a static asset with no Python interface, so its contract is read
straight off the file as text — `tests/test_handback_ui_render.py` is the
pattern to follow: read `forge/ui/index.html` with `Path.read_text("utf-8")`
and assert on substrings.

The row is rendered by one template literal per model inside a `.map(...)`
call; the cells are `<td>` elements and the escaping helper is `esc`, defined
near the top of the page's script.

The header currently reads:

    <thead><tr><th>Model</th><th>Calls</th><th>Tokens</th></tr></thead>

`Tokens` becomes `Total` in the five-column version, because the column beside
`In` and `Out` that holds their sum is a total rather than an unqualified
count.
