# Handback stage 6: the evidence a parked ticket already has

`docs/HANDBACK.md` §7 orders the handback work in seven stages. One through five
shipped — `forge advise`, `forge criteria --add`, `forge release`,
`forge discharge`, and `withheld:<reason>` as a route distinct from `skipped`.
Stage 6 is the dashboard's read side, and stage 7 is the write endpoints behind
the localhost rule.

This backlog is stage 6 and nothing else. It adds no endpoint that writes, no
free-text channel into a prompt, and no new column: every value it renders is
already recorded on the ticket and already read on every dashboard poll. What is
missing is that `snapshot()` throws all of it away.

## Why this and not the entries above it in the roadmap

`docs/ROADMAP.md` has four live entries, and three of them say in their own words
that what they need next is a live run rather than code — convergence has eleven
runs and has never reached the escalation ladder, the adaptive ticket loop says
outright that building split on nine unvalidated features makes a second layer
with no way to attribute a failure to either, and the image loop opens by
admitting it has no evidence at all and an unsettled backend.

The common shape is that the harness is now waiting on the operator's reading of
runs it has already made. Stage 6 is the one piece of unbuilt work that makes
that reading cheaper, and it is the one `docs/HANDBACK.md` calls "what makes the
write side worth having":

> Plus what the dashboard has to show before any of that is useful: a parked
> ticket's `blocked_note`, its withheld reason, its `learned` entries, and its
> repeated failure classes — the same evidence the ladder puts in front of the
> reviewer, in front of the person instead. A note written without the evidence
> is a guess, and the current dashboard renders the backlog, the log, the steps
> and token usage, and none of that.

The last sentence is still accurate. `forge/ui/server.py:117` sends
`blocked_note` and `forge/ui/index.html:195` renders it; nothing else on a parked
ticket reaches the page. A person deciding whether to write a note, add a
criterion, release a withheld ticket or discharge one is deciding from a status
pill and one sentence, with `learned`, `human_note`, `cycle_classes`,
`cycle_volume` and `flat_cycles` sitting unread in the same row the dashboard
already loaded.

## Design decisions

These are settled. A retry cycle may not revise them away.

Decision: this is read-only. No endpoint added by this backlog accepts a POST,
and `POST /api/control` keeps its four enumerated commands. Stage 7 is where a
network write surface is argued for, and `docs/HANDBACK.md` §6 gives three
requirements it has to meet first.

Decision: the evidence block is built for parked tickets only — the four statuses
in `Store.RETRYABLE`. A `done` ticket's payload keeps the exact shape it has
today.

Decision: the lists are capped and their text truncated in `snapshot()`, not in
the page. `learned` and `human_note` are append-only by design and `/api/state`
is polled for the life of a run, so an uncapped payload grows without bound on
the one ticket that is stuck longest.

Decision: the page receives display strings, not vocabulary it has to know.
`routes.describe` and `routes.REASONS` run server-side, so the JavaScript never
holds a copy of the withheld vocabulary that can drift from `forge/routes.py`.

## HD-001: Send a parked ticket's evidence in the state snapshot

### Spec

Add a module-level function to `forge/ui/server.py`:

```python
def evidence(ticket: Ticket) -> dict[str, Any] | None:
```

It returns the parked-ticket evidence for one ticket, or `None` when the ticket
is not parked. A ticket is parked when its `status` is one of the four in
`Store.RETRYABLE` — `failed`, `blocked`, `skipped`, `withheld`. Read
`Store.RETRYABLE` rather than restating the four values.

For a parked ticket it returns a dict with exactly these seven keys:

- `route` — `forge.routes.describe(ticket.route)`, which answers `delegate` for a
  delegated ticket and `withheld: security` for `withheld:security`.
- `reason` — the glossary sentence for the route's reason, looked up as
  `forge.routes.REASONS[forge.routes.reason_of(ticket.route)]`. An empty string
  when `reason_of` answers empty, which is what it answers for a delegated
  ticket.
- `learned` — the first five entries of `ticket.learned`, in the order they are
  stored, each as `{"text": str, "count": int}`. `learned` is already ordered
  commonest first.
- `notes` — the last five entries of `ticket.human_note`, keeping their stored
  order, each as `{"text": str, "at": str}`. Stored order is oldest first, so the
  newest note is the last element.
- `classes` — `ticket.cycle_classes`, as a list, copied rather than aliased.
- `volume` — `ticket.cycle_volume`, an int.
- `flat_cycles` — `ticket.flat_cycles`, an int.

Every `text` value in `learned` and `notes` is truncated to its first 400
characters, with nothing appended in place of what was cut.

Then include it in the ticket entries `snapshot()` builds
(`forge/ui/server.py:108-119`): add the key `"evidence"` to an entry whose ticket
is parked, carrying the dict. An entry whose ticket is not parked does not carry
the key at all.

Leave the eight keys those entries already carry exactly as they are, including
`note`, which stays the ticket's `blocked_note`.

Use only the standard library and what `forge/ui/server.py` already imports, plus
`forge.routes` and `forge.state.Store`.

### Allowed files

- `forge/ui/server.py`
- `tests/test_handback_ui.py`

### Reference files

- `forge/routes.py`

### Acceptance criteria

- For a `Ticket` with `status="blocked"` and `route="withheld:security"`,
  `evidence(ticket)["route"]` is `"withheld: security"` and
  `evidence(ticket)["reason"]` is
  `"authentication, authorization, session handling, secrets"`.
- For a `Ticket` with `status="blocked"` and `route="delegate"`,
  `evidence(ticket)["route"]` is `"delegate"` and `evidence(ticket)["reason"]` is
  `""`.
- For a `Ticket` with `status="withheld"` and `route="claude-only"`,
  `evidence(ticket)["route"]` is `"withheld: unspecified"` and
  `evidence(ticket)["reason"]` is `"no reason was recorded"`.
- `evidence(ticket)` returns `None` for a `Ticket` with `status="done"`, and
  returns a dict for each of `status="failed"`, `"blocked"`, `"skipped"` and
  `"withheld"`.
- For a `Ticket` whose `learned` holds seven entries,
  `evidence(ticket)["learned"]` has length 5 and its first element's `"text"` is
  the first stored entry's text.
- For a `Ticket` with `learned=[{"text": "x" * 900, "count": 2}]`,
  `evidence(ticket)["learned"][0]["text"]` is exactly `"x" * 400`, and that
  entry's `"count"` is `2`.
- For a `Ticket` whose `human_note` holds seven entries with texts `"n0"` through
  `"n6"` in that stored order, the `"text"` values of `evidence(ticket)["notes"]`
  are `["n2", "n3", "n4", "n5", "n6"]`.
- For a `Ticket` with `cycle_classes=["lint", "tests"]`, `cycle_volume=3` and
  `flat_cycles=2`, `evidence(ticket)` has `"classes"` equal to
  `["lint", "tests"]`, `"volume"` equal to `3` and `"flat_cycles"` equal to `2`.
- Appending to the list returned as `evidence(ticket)["classes"]` leaves
  `ticket.cycle_classes` holding the two entries it held before.
- In the state `ui_server.snapshot(store, config)` returns for a run holding one
  `blocked` ticket and one `done` ticket, the entry whose `"id"` is the blocked
  ticket's carries `"evidence"` as a dict, and the entry whose `"id"` is the done
  ticket's has no `"evidence"` key.
- In that same state, the blocked ticket's entry still carries its `"id"`,
  `"title"`, `"route"`, `"status"`, `"attempts"`, `"files"`, `"criteria"` and
  `"note"` keys, and `"note"` equals the ticket's `blocked_note`.

### Notes

`tests/test_forge.py` already builds a `Store` and a `Config` for
`ui_server.snapshot` in `TestTheDashboardFollowsTheRunTheLoopIsIn`; the same
shape works here.

## HD-002: Render the evidence under a parked ticket

**Needs:** HD-001

### Spec

Render the evidence block HD-001 added to the payload, in `forge/ui/index.html`.

Add a function `renderEvidence(e)` beside `renderTickets`. Given a falsy argument
it returns the empty string. Given an evidence object it returns one
`<div class="evidence">` holding, in this order, only the parts that have
something to say:

- the route, as `${e.route} — ${e.reason}`, rendered whenever `e.route` is not
  `"delegate"`; the em dash and the reason are included only when `e.reason` is
  non-empty.
- a line reading `classes: ` followed by `e.classes` joined with `", "`, rendered
  when `e.classes` has at least one entry.
- a line reading `findings: ${e.volume}`, rendered when `e.volume` is greater
  than zero.
- a line reading `flat cycles: ${e.flat_cycles}`, rendered when `e.flat_cycles`
  is greater than zero.
- one line per entry of `e.learned`, reading the entry's text followed by
  ` ×` and its count.
- one line per entry of `e.notes`, reading the entry's `at` then `: ` then its
  text, under a heading line reading `notes`, rendered when `e.notes` has at
  least one entry.

Call it from `renderTickets` (`forge/ui/index.html:190`) as
`renderEvidence(t.evidence)`, placing its output inside the ticket's
`<span class="title">` element, after the existing `note` span, so an entry with
no evidence renders exactly the markup it renders today.

Every value taken from the payload passes through the existing `esc` helper
before it reaches the markup — `e.route`, `e.reason`, each class, each learning's
text, and each note's text and timestamp. The numbers may be interpolated
directly.

Add CSS for `.evidence` and for the lines inside it, following the file's
existing style: `12px` text in `var(--muted)`, `display: block`, and a left
margin that indents it under the title. Add no new CSS variable.

Write no new script tag and no dependency: this is one function and one rule in
the file that is already there.

### Allowed files

- `forge/ui/index.html`
- `tests/test_handback_ui_render.py`

### Reference files

- `forge/ui/server.py`

### Acceptance criteria

- `forge/ui/index.html` contains the text `function renderEvidence(`.
- `forge/ui/index.html` contains the text `renderEvidence(t.evidence)`.
- `forge/ui/index.html` contains each of the texts `e.route`, `e.reason`,
  `e.classes`, `e.volume`, `e.flat_cycles`, `e.learned` and `e.notes`.
- `forge/ui/index.html` contains each of the texts `esc(e.route)` and
  `esc(e.reason)`.
- `forge/ui/index.html` contains the text `.evidence {`, which is how every
  other selector in that file's stylesheet opens its rule.
- `forge/ui/index.html` contains the text `renderTickets(` and the text
  `${esc(t.note)}`, so the block was added beside the note rather than in place
  of it.

### Notes

These criteria assert the wiring, and that is deliberate: `index.html` is the one
file in the repository nothing in the suite can execute, and the failure this
stage is most likely to have is the one wiring assertions catch — a field added
to the payload in HD-001 and never rendered.

What they do not settle is whether the block reads well on a real parked ticket,
and no criterion here can. A person answers that by running it:

```
forge ui --open
```

which serves the dashboard on `127.0.0.1:8799` without starting the loop. Open a
run holding a blocked or withheld ticket and read the block under its title; a
run with nothing parked shows the backlog exactly as it does today.
