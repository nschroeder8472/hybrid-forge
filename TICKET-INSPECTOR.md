# A parked ticket answers in a footnote, and the answer is cut off

**Status:** shipped. Run 7 of this repository, 2026-09-07, built by the loop.
TI-001 landed on its first attempt; TI-002 spent three attempts returning a
file with elided sections — so nothing was written — named that itself, and
landed on the first attempt of the next cycle. Checked live afterwards against
a run that had actually parked: the panel showed a ratification failure whole,
1,038 characters of it, against the 60-character class the row had been
showing.

Two things below did not survive contact, and both are recorded rather than
edited away. The criterion demanding `renderWriteControls` appear **zero**
times assumed the helper had to be deleted; the sign-off pass revised it to
"exactly twice — the definition and the call inside `renderInspector`", which
is the better design and the one that shipped. And that same pass trimmed
`tests/test_inspector_render.py` out of `allowed_files`, so nothing was ever
asked to write tests and the criteria were settled by a reviewer reading the
diff. The assertions were written by hand afterwards, and ratification is now
held to the rule respec already had — see [RATIFY.md](docs/RATIFY.md).

The dashboard's backlog row is one line per ticket, and everything a person
needs in order to decide what to do with a parked one has been added to that
line as indented small print. Three things are wrong with it, and they are the
same thing seen from three angles.

**The evidence is truncated before it says anything.** A parked ticket renders
`classes: tests tester kept softening a criterion s expected value to what t` —
cut mid-word, because a failure *class* is a 60-character identity built by
`failures._message_of` for counting, not a description. The text that would
explain the failure is the failing step's own output, and the backlog never
shows it. A person reading the row is being shown the name of the thing rather
than the thing.

**The two writes are not equals, and they read as though one is an
afterthought.** `renderWriteControls` gives the note a full-width `textarea`
and the criterion a single-line `input` squeezed beside its button. A note is
advice the next attempt may or may not use; a criterion is a change to the
contract the ticket is judged against. The more consequential of the two is the
smaller field on the page.

**There is nowhere to read the ticket itself.** The row carries a title and a
blocked note. What the ticket actually asked for — its spec, its criteria, the
files it may write — is in `.hybridforge/tickets/<id>.md` and in the database,
and a person deciding whether to add a criterion has to leave the page to find
out what the criteria already are.

All three are the same missing thing: a parked ticket has more to say than a
row can hold, so it needs somewhere to say it. Clicking the ticket opens a
panel in front of the page holding what the ticket asked for, what actually
failed, and the two writes as equals at the bottom.

## Design decisions

These are settled. A retry cycle may not revise them away.

Decision: the panel's contents come from a new per-ticket endpoint,
`GET /api/ticket/<id>`, rather than from `/api/state`. The state snapshot is
polled for the life of a run and already carries every ticket; adding each
ticket's spec and the full text of every failure it recorded would multiply the
size of a payload that is fetched every few seconds, to serve a panel that is
open for one ticket at a time and usually not open at all.

Decision: the endpoint is a read and is served to any peer the dashboard is
already serving. The loopback rule in `forge/ui/writes.py` gates *writes*,
because a write can change a run; this returns what the backlog row already
shows plus the text under it.

Decision: failure text is the failing steps' recorded output, from
`Store.failed_steps`, capped per step at 4,000 characters — the same cap
`snapshot()` already applies to the step feed. Failure *classes* stay as they
are: they are counted, and a 60-character identity is what makes counting them
work.

Decision: the note and the criterion are both `textarea` elements, the same
width and the same height, stacked in that order, each with its own button
directly under it. The criterion stops being an inline `input`.

Decision: only a parked ticket opens a panel. A parked ticket is one carrying
`evidence` in the state payload — the same test `renderWriteControls` already
uses — so a `done` ticket's row behaves exactly as it does today.

## TI-001: Serve one parked ticket's whole story

**Route:** delegate

### Spec

Add a read endpoint to the dashboard server, in `forge/ui/server.py`.

Add a module-level function:

```python
def ticket_detail(store: Store, config: Config, ticket_id: str) -> dict[str, Any] | None:
```

It returns the detail for one ticket of the run `snapshot()` is showing, or
`None` when that run has no ticket with that id. Find the run the same way
`snapshot()` does — `_live_run(store) or store.latest_run()` — and return
`None` when there is no run at all.

For a ticket that exists it returns a dict with exactly these nine keys:

- `id` — the ticket's `ticket_id`.
- `title` — the ticket's `title`.
- `status` — the ticket's `status`.
- `route` — `forge.routes.describe(ticket.route)`, the same value `evidence()`
  puts under `route`.
- `spec` — the ticket's `spec`, whole.
- `criteria` — the ticket's `criteria`, as a list of strings.
- `files` — the ticket's `allowed_files`, as a list of strings.
- `note` — the ticket's `blocked_note`.
- `failures` — a list, oldest first, of `{"step": str, "detail": str}`, one
  entry per failed step `Store.failed_steps(run_id, ticket_id)` returns, with
  each `detail` cut to its first 4,000 characters.

Then serve it. In `Handler.do_GET`, after the `/api/state` branch and before
the `/api/events` branch, handle a path of the form `/api/ticket/<id>`: decode
the id with `urllib.parse.unquote`, call `ticket_detail`, and send the dict as
JSON with code 200. When `ticket_detail` returns `None`, send
`{"error": "no such ticket"}` with code 404.

`do_GET` already splits the query off the path before matching; match on the
path part, so `/api/ticket/T-1?x=1` is the same request as `/api/ticket/T-1`.

### Allowed files

- `forge/ui/server.py`
- `tests/test_ticket_detail.py`

### Reference files

- `forge/ui/writes.py`

### Acceptance criteria

- `ticket_detail` for a ticket whose `spec` is `"build the thing"` returns a
  dict whose `spec` is `"build the thing"`.
- `ticket_detail` for a ticket with `criteria` `["a", "b"]` and
  `allowed_files` `["x.py"]` returns `criteria` `["a", "b"]` and `files`
  `["x.py"]`.
- `ticket_detail` for a ticket routed `withheld:security` returns `route`
  `"withheld: security"`.
- For a ticket with one failed step named `tests` whose detail is
  `"tester kept softening a criterion"`, `ticket_detail` returns `failures`
  equal to `[{"step": "tests", "detail": "tester kept softening a criterion"}]`.
- For a ticket with a failed step whose detail is 5,000 characters long,
  the `detail` in `failures` is 4,000 characters long.
- For a ticket with two failed steps, `failures` holds them oldest first.
- `ticket_detail` returns `None` for an id no ticket in the run has.
- A `GET` of `/api/ticket/T-1` against the running dashboard returns status
  200 and a JSON body whose `id` is `"T-1"`.
- A `GET` of `/api/ticket/NOPE` against the running dashboard returns status
  404 and a JSON body whose `error` is `"no such ticket"`.
- A `GET` of `/api/state` still returns a JSON body carrying a `tickets` list.

### Context

`tests/test_ui_write_endpoint.py` is the pattern for serving a real request in
a test: it starts a `ThreadingHTTPServer` on port 0 with a subclass of
`ui_server.Handler` carrying a `store` and a `config`, and calls it with
`urllib.request`. Follow it rather than inventing a second way.

`Store.failed_steps(run_id, ticket_id)` already returns `(name, detail)` pairs
for every failed step of one ticket, oldest first, skipping steps with no
detail. It is the read this endpoint needs; no new SQL belongs in this ticket.

`describe` and `snapshot`'s helper `_live_run` are both already imported or
defined in `forge/ui/server.py`.

## TI-002: Open a parked ticket in front of the page

**Route:** delegate

**Needs:** TI-001

### Spec

In `forge/ui/index.html`, give a parked ticket a panel that opens in front of
the page when its row is clicked.

The markup: add one container to the page, `<div id="inspector" hidden></div>`,
as the last element inside `<main>`. It holds the panel and the backdrop behind
it. Style it in the page's own `<style>` block with a rule opening
`#inspector {` that positions it over the page — `position: fixed`, covering
the viewport, above the rest of the content — with the panel itself in a rule
opening `.inspector-panel {` that is centred, at most `760px` wide, at most
`80vh` tall, and scrolls its own overflow.

The behaviour, as three functions:

- `openInspector(id)` — fetches `/api/ticket/<id>` with `encodeURIComponent` on
  the id, renders the panel from the JSON, and clears the `hidden` attribute on
  `#inspector`.
- `renderInspector(d)` — returns the panel's markup for one detail payload.
- `closeInspector()` — sets `hidden` on `#inspector` again.

Wire the opening to the row: in `renderTickets`, a ticket carrying `evidence`
gets `onclick="openInspector('<id>')"` on its `<li>`, and the class
`clickable`. A ticket with no evidence gets neither, so a `done` row is inert.
Wire the closing twice: a click on the backdrop area of `#inspector` closes it,
and so does the `Escape` key, through a `keydown` listener on `document`.

The panel's contents, in this order, top to bottom:

1. The ticket: its id and title as a heading, then `route` and `status`, then
   the `spec` text, then the criteria as a list, then the files as a list.
2. The failures: one block per entry in `failures`, each showing the step name
   and its `detail` inside a `<pre>` so the text keeps its own line breaks.
   When `failures` is empty, the words `No failures recorded.`
3. The writes: a `textarea` with id `note-<id>` and a button under it reading
   `Add note`, then a `textarea` with id `criterion-<id>` and a button under it
   reading `Add criterion`, then the release input and button exactly as they
   are today for a ticket whose route starts `withheld`, then the status span
   with id `status-<id>`.

`writeNote`, `writeCriterion` and `releaseTicket` keep working unchanged: they
read `note-<id>`, `criterion-<id>`, `release-<id>` and `status-<id>` by id, and
those ids now live in the panel. Remove the write controls from the backlog
row — `renderWriteControls` and its call in `renderTickets` — so each field id
exists once on the page.

Every value interpolated into the panel goes through the page's `esc()` helper,
as `renderEvidence` already does.

Keep the evidence lines on the backlog row as they are. The row keeps saying
what kind of failure and how many; the panel is where the text lives.

### Allowed files

- `forge/ui/index.html`
- `tests/test_inspector_render.py`

### Reference files

- `forge/ui/server.py`

### Acceptance criteria

- `forge/ui/index.html` contains the text `id="inspector"`.
- `forge/ui/index.html` contains the text `function openInspector(`, the text
  `function renderInspector(`, and the text `function closeInspector(`.
- `forge/ui/index.html` contains the text `/api/ticket/` and the text
  `encodeURIComponent`.
- `forge/ui/index.html` contains the text `onclick="openInspector(` inside the
  template `renderTickets` returns.
- `forge/ui/index.html` contains the text `#inspector {` and the text
  `.inspector-panel {`.
- `forge/ui/index.html` contains the text `d.spec`, the text `d.criteria`, the
  text `d.files`, and the text `d.failures`.
- `forge/ui/index.html` contains the text `<pre` within 400 characters after
  the text `d.failures`.
- `forge/ui/index.html` contains two occurrences of the text `<textarea`.
- `forge/ui/index.html` contains the text `criterion-${id}` and the text
  `note-${id}`, and each appears exactly once.
- `forge/ui/index.html` contains the text `Escape`.
- `forge/ui/index.html` contains the text `renderWriteControls` zero times.
- `forge/ui/index.html` still contains the text `function writeNote(`, the text
  `function writeCriterion(`, and the text `function releaseTicket(`.
- `forge/ui/index.html` still contains the text `function renderEvidence(` and
  the text `renderEvidence(t.evidence)`.

### Context

The page is a static asset with no Python interface, so its contract is read
straight off the file as text; `tests/test_handback_ui_render.py` is the
pattern — read `forge/ui/index.html` with `Path.read_text("utf-8")` and assert
on substrings.

The page has one CSS custom-property palette at the top — `--panel`,
`--panel-2`, `--line`, `--text`, `--muted`, `--accent`, `--warn` — and the
panel should be built from those rather than from new colour literals, so it
matches the rest of the dashboard.

`renderTickets` currently renders each ticket as an `<li class="ticket">`
carrying an id span, a title span and a status pill, with `renderEvidence` and
`renderWriteControls` appended inside the title span.

The three write functions each end by clearing their field and calling
`refresh()`; `refresh()` re-renders the backlog from `/api/state`. Leave that
as it is — the panel stays open, and its own contents are refreshed the next
time it is opened.
