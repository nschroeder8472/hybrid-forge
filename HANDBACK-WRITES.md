# Handback stage 7: the write endpoints, behind the localhost rule

`docs/HANDBACK.md` §7 orders the handback work in seven stages. One through six
have shipped: `forge advise`, `forge criteria --add`, `forge release`,
`forge discharge`, `withheld:<reason>` as a route distinct from `skipped`, and
the dashboard's read side — the evidence block under a parked ticket, added by
`HANDBACK-DASHBOARD.md`.

Stage 7 is the last one. A person reading a parked ticket on the dashboard can
now see everything the ladder saw — the withheld reason, the repeated failure
classes, the findings count, what was learned, what an earlier note said — and
still has to leave the page and find a terminal to answer any of it. This
backlog gives the three answers a network surface: a note, a criterion, and a
release. Each one already exists as a command and is already exercised; what is
added is a way to reach it from the page that showed the evidence.

`forge discharge` is deliberately not among them, and §6's three security
requirements are what the first two tickets are about.

## Design decisions

These are settled. A retry cycle may not revise them away.

Decision: `forge discharge` stays a command-line command. No endpoint in this
backlog runs the project's lint, typecheck, build or test commands, because a
verify started by an HTTP request runs against the same working tree the loop
is verifying, and then both answers describe a tree neither of them controls.

Decision: three endpoints, all POST, all per-ticket:
`POST /api/ticket/<id>/note`, `POST /api/ticket/<id>/criterion` and
`POST /api/ticket/<id>/route`.

Decision: every write goes through `Store.advise`, `Store.promote_criteria`,
`Store.set_route` and `Store.reset_tickets` — the same four methods the CLI
handback commands call. This backlog adds no SQL and no new column.

Decision: the exposure gate is a new configuration field
`ui.allowRemoteWrites`, default `false`. While it is false a write is served
when the dashboard is bound to a loopback address *and* the request arrives
from a loopback peer. While it is true a write is served whatever the bind
address and whatever the peer. The read endpoints and `POST /api/control` keep
the behaviour they have today, which the `exposure_warning` text covers.

Decision: the peer address is checked as well as the bind address, so a proxy
in front of a loopback dashboard forwards a write only when the operator has
set `ui.allowRemoteWrites`.

Decision: the deciding logic lives in a new module `forge/ui/writes.py` as
ordinary functions that take a `Store`, a `Config`, a request path, a decoded
payload and a peer address, and return a status code and a dictionary.
`forge/ui/server.py` decodes the request, calls one of them, and sends what it
returns. The behaviour is reachable from a unit test with no socket open.

Decision: a note and a criterion are each capped at 2000 characters. Longer
text is refused with 400 and the limit named, and text within the cap is stored
exactly as it was typed, character for character.

Decision: every accepted write records an event through `Store.log` with
`kind="ticket"` and a `data` dictionary carrying `ticket`, `author` set to
`"human"`, `via` set to `"dashboard"`, and `peer` set to the requesting
address, so the step log shows that a person intervened, through which surface,
and from where.

Decision: a write acts on the run the dashboard is showing — `_live_run(store)`
when it returns a run, and `store.latest_run()` otherwise, which is the rule
`snapshot()` already uses. A payload may name a `run`, and a payload naming a
different run than the one resolved is refused with 409 rather than applied to
whichever run happens to be current.

Decision: the page keeps its polling refresh. A write posts, shows a one-line
result beside the ticket it acted on, and calls `refresh()`.

## Out of scope

- **A discharge endpoint.** `forge discharge <id>` is how a hand-implemented
  ticket is recorded, and the dashboard shows that line for a person to copy.
- **Authentication.** There is none today and this adds none; the localhost
  rule is the whole of the access control, which is why the gate is the one
  ticket here a person writes.
- **Making a note safe to put in a prompt.** That is already true:
  `forge/prompts.py` renders `human_note` as its own user message under
  `ADVICE_HEADING`, never concatenated into a system message. Nothing here
  changes how a note reaches a model.
- **Editing or deleting a note.** `human_note` is append-only by design and
  stays that way.

## Running it

```
forge ui --open
```

serves the dashboard on `127.0.0.1:8799` and opens it, without starting the
loop. Open a run holding a blocked, failed, skipped or withheld ticket: under
that ticket's evidence block are a note field with an `Add note` button, a
criterion field with an `Add criterion` button, and — on a withheld ticket only
— a reason field with a `Release` button.

Before anything is typed the fields are empty, the buttons are enabled, and the
result line beside them is blank. After a write the line reads `noted`,
`criterion added`, or `released`, and on a refusal it reads the server's error
sentence — `a note needs text`, or the sentence naming `ui.allowRemoteWrites`.
A ticket that is not parked shows no controls, and a run with nothing parked
shows the backlog exactly as it does today.

---

# HW-001: Record `ui.allowRemoteWrites` in the configuration

**Route:** delegate
**Kind:** feature

## Spec

Add the field that the exposure gate reads. Three edits to
`forge/config.py`, all in places that already handle `ui`:

1. The `UISettings` dataclass gains a field `allow_remote_writes: bool = False`.
2. `Config.load` builds `UISettings` from the file's `ui` block; it passes
   `allow_remote_writes=bool(ui.get("allowRemoteWrites", False))` alongside the
   `host`, `port` and `enabled` it already passes.
3. `Config.write` serialises the `ui` block as a dictionary literal; that
   dictionary gains `"allowRemoteWrites": self.ui.allow_remote_writes` beside
   the three keys it already writes.

The camel-case spelling in the file and the snake-case spelling on the
dataclass is the convention every other loop setting in this file follows —
`maxAttempts` and `max_attempts`, `flatCycles` and `flat_cycles`.

Keep `UISettings`'s existing comment about binding beyond loopback exactly as
it appears, character for character; it describes the same posture this field
extends.

## Allowed files

- `forge/config.py`
- `tests/test_ui_write_config.py`

## Reference files

- `forge/ui/server.py`

## Acceptance criteria

- `UISettings()` constructed with no arguments has `allow_remote_writes` equal
  to `False`.
- `Config.load` over a root whose `.hybridforge/config.json` has a `ui` block
  of `{"host": "0.0.0.0", "port": 8799}` returns a config whose
  `ui.allow_remote_writes` is `False` and whose `ui.host` is `"0.0.0.0"`.
- `Config.load` over a root whose `.hybridforge/config.json` has a `ui` block
  of `{"allowRemoteWrites": true}` returns a config whose
  `ui.allow_remote_writes` is `True`.
- A config whose `ui.allow_remote_writes` is `True`, passed through
  `Config.write()` and then read back with `json.loads`, has
  `payload["ui"]["allowRemoteWrites"]` equal to `True` and still carries
  `payload["ui"]["host"]`, `payload["ui"]["port"]` and
  `payload["ui"]["enabled"]`.
- A config written by `Config.write()` and loaded again with `Config.load`
  returns the same `ui.allow_remote_writes` value it was written with, for both
  `True` and `False`.

## Notes

**Landed by hand on 2026-09-05**, alongside HW-002, which cannot be verified
without the field this adds. `forge discharge HW-001` records it, or delete
this section before ingesting.

The round-trip criterion is the one that matters: the field is read on every
write request, and a field that loads but does not survive a `Config.write()`
turns itself off the next time anything saves the configuration.

---

# HW-002: The gate that decides whether a write may be served

**Route:** withheld:security
**Kind:** feature
**Needs:** HW-001

## Spec

This is the access control for every endpoint in this backlog, so it is written
by a person rather than delegated. It is small and completely specified here.

Create `forge/ui/writes.py`. It holds one function:

```python
def write_refusal(config: Config, peer: str) -> str:
```

It returns `""` when a write may be served, and otherwise a sentence explaining
why it was not — the sentence the caller sends back as the body of a 403 and
the page shows beside the ticket.

The rule, in order:

1. When `config.ui.allow_remote_writes` is `True`, return `""`.
2. Otherwise the write is served only when both of these hold: the bind address
   is loopback, which is `not is_exposed(config.ui.host)` using `is_exposed`
   from `forge/ui/server.py`; and the peer is loopback, which is `peer` with
   surrounding whitespace stripped being one of `LOOPBACK_HOSTS` from that same
   module, or the string `"::ffff:127.0.0.1"`, which is how a dual-stack socket
   reports a loopback IPv4 client.
3. When either of those fails, return a sentence that names the bind address,
   the peer address, and the setting `ui.allowRemoteWrites` that turns writes
   on.

Read `is_exposed` and `LOOPBACK_HOSTS` from `forge.ui.server` rather than
restating either, so the two lists cannot drift. Import the module —
`from . import server`, then `server.is_exposed(...)` — rather than the two
names, because HW-004 has `server` importing this module in turn, and a
`from .server import LOOPBACK_HOSTS` here fails whenever `server` is the module
loaded first: its own import of this one runs before `LOOPBACK_HOSTS` exists.
Use no third-party library:
`str` comparison is the whole of it, and `ipaddress` is not needed for a fixed
set of three literals.

## Allowed files

- `forge/ui/writes.py`
- `tests/test_ui_write_gate.py`

## Reference files

- `forge/ui/server.py`

## Acceptance criteria

- `write_refusal` with a config whose `ui.host` is `"127.0.0.1"` and
  `ui.allow_remote_writes` is `False`, and a peer of `"127.0.0.1"`, returns
  `""`.
- The same config with a peer of `"::1"` returns `""`, and with a peer of
  `"::ffff:127.0.0.1"` returns `""`.
- A config whose `ui.host` is `"0.0.0.0"` and `ui.allow_remote_writes` is
  `False`, with a peer of `"127.0.0.1"`, returns a string containing
  `allowRemoteWrites` and containing `0.0.0.0`.
- A config whose `ui.host` is `"127.0.0.1"` and `ui.allow_remote_writes` is
  `False`, with a peer of `"10.0.0.4"`, returns a string containing
  `allowRemoteWrites` and containing `10.0.0.4`.
- A config whose `ui.host` is `"0.0.0.0"` and `ui.allow_remote_writes` is
  `True`, with a peer of `"10.0.0.4"`, returns `""`.
- A config whose `ui.host` is `"127.0.0.1"` and `ui.allow_remote_writes` is
  `False`, with a peer of `""`, returns a string containing
  `allowRemoteWrites`.

## Notes

**Landed by hand on 2026-09-05** as `forge/ui/writes.py`, with its criteria in
`tests/test_ui_write_gate.py`. `forge discharge HW-002` records it, or delete
this section before ingesting.

For the person who writes this: the failure mode worth guarding against is the
one where a refusal is computed and then not returned — the gate that fails
open. The criteria above pin both directions, and the ticket that follows pins
that `apply_write` actually consults it, with the store read back to show no
write happened.

The last criterion is the empty peer. `BaseHTTPRequestHandler` reports
`client_address` from the socket and it is a string for both address families,
but a handler constructed in a test may carry an empty tuple; an empty peer is
not loopback and is refused.

---

# HW-003: Apply a note, a criterion and a release from a decoded request

**Route:** delegate
**Kind:** feature
**Needs:** HW-002

## Spec

Add to `forge/ui/writes.py` the function that turns a decoded request into one
of the three writes. Keep `write_refusal` exactly as HW-002 wrote it, character
for character.

```python
def apply_write(
    store: Store,
    config: Config,
    path: str,
    payload: dict,
    peer: str,
) -> tuple[int, dict]:
```

It returns an HTTP status code and the dictionary to send as JSON. It opens no
socket, reads no request object, and runs no subprocess.

The order of decisions:

1. `refusal = write_refusal(config, peer)`. When it is non-empty, return
   `(403, {"error": refusal})`. This happens before the path is parsed, so a
   refused caller learns nothing about which tickets exist.
2. Parse the path. It is served only when, after splitting on `/` and dropping
   empty segments, it is exactly `["api", "ticket", <id>, <action>]`. The id is
   percent-decoded with `urllib.parse.unquote`. Any other shape returns
   `(404, {"error": "not found"})`.
3. The action is one of `"note"`, `"criterion"`, `"route"`. Any other action
   returns `(404, {"error": "not found"})`.
4. Resolve the run the same way `snapshot()` does: `_live_run(store)` imported
   from `forge.ui.server`, and `store.latest_run()` when that returns `None`.
   When both return `None`, return `(409, {"error": "no runs yet"})`.
5. When `payload` carries a `run` key, compare `int(payload["run"])` with the
   resolved run id and return `(409, {"error": ...})` naming both ids when they
   differ. A `run` that is not an integer is the same refusal.
6. Find the ticket: the entry of `store.list_tickets(run_id)` whose
   `ticket_id` equals the decoded id. When there is none, return
   `(404, {"error": f"run {run_id} has no ticket {ticket_id}"})`.
7. Read the text: `str(payload.get("text", "")).strip()`. It is required for
   every action. When it is empty, return `(400, {"error": ...})` with a
   sentence naming what was missing — `"a note needs text"` for `note`,
   `"a criterion needs text"` for `criterion`, and
   `"releasing a withheld ticket needs a reason"` for `route`. When it is
   longer than 2000 characters, return `(400, {"error": ...})` with a sentence
   containing `2000`.

Then, by action:

**`note`.** Call `store.advise(run_id, ticket, text)`. Log through `store.log`
with the message `f"{ticket.ticket_id}: a person advised — {text[:200]}"`,
`kind="ticket"`, and `data={"ticket": ticket.ticket_id, "author": "human",
"via": "dashboard", "peer": peer}`. Return `(200, {"ok": True, "notes":
len(ticket.human_note)})` — `advise` updates the ticket it was handed, so the
count is the count after this note landed.

**`criterion`.** Call `store.promote_criteria(run_id, ticket.ticket_id,
[text])`, which returns `(ticket, adopted)`. Log with the message
`f"{ticket.ticket_id}: a person added a criterion — {text[:200]}"` and the same
`kind` and `data` as a note. Return `(200, {"ok": True, "adopted": adopted})`.
A criterion the ticket already carries comes back as an empty `adopted` list
and is still a 200: the contract already says what the person wanted it to say.

**`route`.** The payload must carry `route` equal to `"delegate"`; anything
else returns `(400, {"error": ...})` naming `delegate` as the value this
endpoint accepts. When `routes.is_withheld(ticket.route)` is `False`, return
`(200, {"ok": True, "route": ticket.route, "released": False})` and write
nothing at all. Otherwise, holding `was = ticket.route`:
`store.advise(run_id, ticket, f"Released from {routes.describe(was)}: {text}")`,
then `store.set_route(run_id, ticket, routes.DELEGATE)`, then — when
`ticket.status` is `TICKET_WITHHELD` from `forge.state` —
`store.reset_tickets(run_id, ticket_ids=[ticket.ticket_id])`. Log with
`level="warn"`, the message `f"{ticket.ticket_id}: released from
{routes.describe(was)} by a person — {text[:200]}"`, `kind="ticket"`, and
`data={"ticket": ticket.ticket_id, "was": was, "author": "human", "via":
"dashboard", "peer": peer}`. Return
`(200, {"ok": True, "route": "delegate", "released": True})`.

This is the same sequence `cmd_release` in `forge/cli.py` performs, in the same
order, and the `data` keys `author` and `was` match what that command logs.

## Allowed files

- `forge/ui/writes.py`
- `tests/test_ui_write_apply.py`

## Reference files

- `forge/ui/server.py`
- `forge/routes.py`

## Acceptance criteria

- `apply_write` with the path `/api/ticket/T-1/note`, the payload
  `{"text": "  the fixtures exist now  "}` and the peer `"127.0.0.1"`, against
  a store whose newest run holds a blocked ticket `T-1`, returns status `200`
  and a body whose `ok` is `True`; reading the run's tickets back afterwards,
  `T-1`'s `human_note` has one entry whose `text` is
  `the fixtures exist now`.
- That same call, against a config whose `ui.host` is `"0.0.0.0"` and whose
  `ui.allow_remote_writes` is `False`, with the peer `"10.0.0.4"`, returns
  status `403` and a body whose `error` contains `allowRemoteWrites`; reading
  the tickets back, `T-1`'s `human_note` is empty.
- A note payload of `{"text": "   "}` returns status `400` and a body whose
  `error` is `a note needs text`; `T-1`'s `human_note` is empty afterwards.
- A note payload whose text is 2001 `"x"` characters returns status `400` and a
  body whose `error` contains `2000`; `T-1`'s `human_note` is empty afterwards.
- A note payload whose text is exactly 2000 `"x"` characters returns status
  `200`, and `T-1`'s stored note text is 2000 characters long.
- After one accepted note, `store.events_after(0)` includes a row whose
  `kind` is `ticket` and whose `data`, parsed as JSON, has `author` equal to
  `human`, `via` equal to `dashboard`, `peer` equal to `127.0.0.1`, and
  `ticket` equal to `T-1`.
- `apply_write` with the path `/api/ticket/T-1/criterion` and the payload
  `{"text": "capture returns a seed of 3130775471"}` returns status `200` and a
  body whose `adopted` is `["capture returns a seed of 3130775471"]`; reading
  the tickets back, both `T-1`'s `criteria` and its `original_criteria` contain
  that string.
- Sending that identical criterion a second time returns status `200` and a
  body whose `adopted` is `[]`, and `T-1`'s `criteria` holds exactly one copy
  of that string.
- `apply_write` with the path `/api/ticket/T-1/route` and the payload
  `{"route": "delegate", "text": "the scope no longer touches auth"}`, against a
  ticket whose route is `withheld:security` and whose status is `withheld`,
  returns status `200` and a body whose `released` is `True`; reading the
  tickets back, `T-1`'s `route` is `delegate`, its `status` is `pending`, its
  `attempts` is `0`, and its `human_note` last entry `text` is
  `Released from withheld: security: the scope no longer touches auth`.
- The same route payload against a ticket whose route is already `delegate`
  returns status `200` and a body whose `released` is `False`, and that
  ticket's `human_note` is empty afterwards.
- A route payload of `{"route": "skip", "text": "why not"}` against a
  `withheld:security` ticket returns status `400`, and the ticket's `route` is
  still `withheld:security` afterwards.
- A route payload of `{"route": "delegate", "text": ""}` against a
  `withheld:security` ticket returns status `400` with a body whose `error` is
  `releasing a withheld ticket needs a reason`, and the ticket's `route` is
  still `withheld:security` afterwards.
- `apply_write` with the path `/api/ticket/T-9/note` and a valid note payload,
  against a run holding only `T-1`, returns status `404` and a body whose
  `error` contains `T-9`.
- `apply_write` with the path `/api/ticket/T-1/frobnicate` returns status `404`
  and a body whose `error` is `not found`; so does the path `/api/ticket/T-1`
  and the path `/api/state`.
- A note payload of `{"text": "ok", "run": 999}` against a store whose resolved
  run id is not 999 returns status `409`, and `T-1`'s `human_note` is empty
  afterwards.
- A note payload of `{"text": "ok", "run": <the resolved run id>}` returns
  status `200`.
- `apply_write` against a store holding no runs at all returns status `409` and
  a body whose `error` is `no runs yet`.

## Notes

The pairs matter more than the codes here. Every refusal criterion reads the
store back and asserts what is *not* there, because a status code is easy to
return correctly next to a write that already happened — the 403 case
especially, which is the whole of the security requirement in §6 of
`docs/HANDBACK.md`.

`store.advise` mutates the `Ticket` object it is passed as well as the row, so
the `notes` count in the response is read from that object. The criteria read
tickets back out of the store rather than from that object, which is what
catches a write that updated the object and not the row.

---

# HW-004: Serve the three endpoints from the dashboard

**Route:** delegate
**Kind:** feature
**Needs:** HW-003

## Spec

Wire `apply_write` into `forge/ui/server.py`. Two changes.

**`do_POST` gains the new paths.** It keeps the `/api/control` branch exactly
as it reads today — the same four enumerated commands, the same control-table
write, the same log line, the same JSON body. What changes is what happens for
every other path: instead of the current 404, read `Content-Length` bytes,
decode them as JSON, and call
`writes.apply_write(self.store, self.config, self.path, payload, peer)` where
`peer` is `self.client_address[0]` when `self.client_address` is a non-empty
tuple and `""` otherwise. Send the returned dictionary as JSON with the
returned status through the existing `_send_json`.

A body that is not valid JSON keeps the answer `/api/control` already gives it:
status 400 and the body `{"error": "invalid JSON"}`. An empty body decodes as
`{}`, as it does today.

Import the new module as `from . import writes` so the two names in it stay
addressable as `writes.apply_write` and `writes.write_refusal`. At the top of
the file is fine, and the reverse import is already written to make it fine:
`writes` reaches back through the module object rather than by name, so it does
not need anything from `server` to exist at the moment it is loaded.

**A handler class a test can bind.** `serve` currently builds its handler with
`type("BoundHandler", (Handler,), {"store": store, "config": config})` inline.
Move exactly that expression into a module-level function:

```python
def bound_handler(config: Config, store: Store) -> type[Handler]:
```

which returns the class, and have `serve` call it. `serve` keeps everything
else it does, character for character: the exposure warning printed to stderr,
`ThreadingHTTPServer`, `daemon_threads`, and the named background thread.

## Allowed files

- `forge/ui/server.py`
- `tests/test_ui_write_endpoint.py`

## Reference files

- `forge/ui/server.py`

## Acceptance criteria

- `bound_handler(config, store)` returns a class whose `store` attribute is
  that store and whose `config` attribute is that config.
- A `ThreadingHTTPServer` built from `bound_handler(config, store)` and bound to
  `("127.0.0.1", 0)`, against a store whose newest run holds a blocked ticket
  `T-1`: a POST to `/api/ticket/T-1/note` with the JSON body
  `{"text": "the fixtures exist now"}` answers status `200` with a JSON body
  whose `ok` is `True`, and `T-1`'s `human_note` read back from the store has
  one entry whose `text` is `the fixtures exist now`.
- On that same server, a POST to `/api/ticket/T-1/note` whose body is the bytes
  `not json` answers status `400` with a JSON body whose `error` is
  `invalid JSON`.
- On that same server, a POST to `/api/control` with the body
  `{"command": "pause"}` answers status `200` with a JSON body whose `ok` is
  `True`, and `store.get_control(CONTROL_KEY, CONTROL_RUN)` afterwards equals
  `CONTROL_PAUSE`.
- On that same server, a POST to `/api/nope` answers status `404`.
- On that same server, a GET of `/api/state` answers status `200` with a JSON
  body carrying a `tickets` list holding one entry whose `id` is `T-1`.
- On that same server, a POST to `/api/ticket/T-1/route` with the body
  `{"route": "delegate", "text": "answered"}` against a ticket routed
  `withheld:security` answers status `200`, and the ticket read back from the
  store has route `delegate`.

## Notes

The tests here bind a real socket on `127.0.0.1` with port 0 and read the port
back off `server.server_address`, so nothing depends on 8799 being free. Serve
the requests from `server.handle_request()` on a background thread, or start
`serve_forever` on one and shut it down in `tearDown`; either is fine as long
as the test does not leave a thread running after it returns.

The `/api/control` criterion is here because this ticket rewrites the method
that serves it. It is a regression assertion, not new behaviour.

---

# HW-005: The controls under a parked ticket

**Route:** delegate
**Kind:** feature
**Needs:** HW-004

## Spec

Add the controls to `forge/ui/index.html`. Endpoints with nothing that calls
them are the failure this ticket exists to prevent.

`renderTickets` already renders each ticket's title, its blocked note, and
`renderEvidence(t.evidence)`. Add a call to a new function
`renderWriteControls(t)` immediately after the evidence block, inside the same
`<span class="title">`.

`renderWriteControls(t)` returns `""` when `t.evidence` is absent, so only a
parked ticket carries controls. Otherwise it returns a `<div class="writes">`
holding:

- a `<textarea>` with id `note-${t.id}` and a placeholder of
  `Tell the loop something it cannot see`, followed by a `<button>` reading
  `Add note` whose `onclick` calls `writeNote('<the escaped id>')`.
- an `<input>` with id `criterion-${t.id}` and a placeholder of
  `State a criterion the plan should have had`, followed by a `<button>`
  reading `Add criterion` whose `onclick` calls `writeCriterion(...)`.
- when `t.evidence.route` starts with `withheld`: an `<input>` with id
  `release-${t.id}` and a placeholder of `Why the objection is answered`,
  followed by a `<button>` reading `Release` whose `onclick` calls
  `releaseTicket(...)`.
- a `<span class="write-status">` with id `status-${t.id}`, empty until a write
  answers.

Every interpolation of a ticket id or of any string from the payload goes
through the existing `esc` helper, exactly as `renderEvidence` does. Ids are
interpolated into element ids as well, so build them with the same escaped
value.

Three functions perform the writes. Each reads its field, posts, writes the
outcome into the ticket's status span, clears the field it read on success, and
calls `refresh()`:

```javascript
async function writeNote(id)      // POST /api/ticket/<id>/note
async function writeCriterion(id) // POST /api/ticket/<id>/criterion
async function releaseTicket(id)  // POST /api/ticket/<id>/route
```

Each builds its URL with `encodeURIComponent(id)`. The bodies are
`{ text, run }` for a note and a criterion, and
`{ route: "delegate", text, run }` for a release, where `run` is
`state.run && state.run.id` — the run the page is currently showing, which is
what makes the server's 409 possible. Each posts with
`method: "POST"` and the header `Content-Type: application/json`, as `control`
already does.

The status span reads `noted` after a note, `criterion added` after a
criterion, and `released` after a release. When the response body carries an
`error`, the span shows that error text instead and the field keeps what was
typed, so a refused note is not lost. When a criterion comes back with an empty
`adopted` list, the span reads `already a criterion`.

Add a stylesheet rule opening `.writes {` and one opening `.write-status {`,
in the same file's `<style>` block, in the style of the neighbouring rules.

## Allowed files

- `forge/ui/index.html`
- `tests/test_ui_write_render.py`

## Reference files

- `forge/ui/index.html`

## Acceptance criteria

- `forge/ui/index.html` contains the text `function renderWriteControls(`.
- `forge/ui/index.html` contains the text `renderWriteControls(t)`, and the
  index of that text is greater than the index of the text
  `renderEvidence(t.evidence)`, so the controls sit after the evidence block.
- `forge/ui/index.html` contains each of the texts `async function writeNote(`,
  `async function writeCriterion(` and `async function releaseTicket(`.
- `forge/ui/index.html` contains each of the texts `/note`, `/criterion` and
  `/route`, and contains the text `encodeURIComponent(`.
- `forge/ui/index.html` contains the text `/api/ticket/` — written without a
  surrounding quote character, so a template literal and a concatenation both
  satisfy it.
- `forge/ui/index.html` contains each of the texts `note-`, `criterion-`,
  `release-` and `status-`, which are the id prefixes the three fields and the
  status span carry.
- `forge/ui/index.html` contains each of the texts `Add note`, `Add criterion`
  and `Release`.
- `forge/ui/index.html` contains each of the texts `noted`, `criterion added`,
  `released` and `already a criterion`.
- `forge/ui/index.html` contains the text `esc(t.id)`.
- `forge/ui/index.html` contains the text `state.run && state.run.id`.
- `forge/ui/index.html` contains the text `.writes {` and the text
  `.write-status {`, which is how every other selector in that file's
  stylesheet opens its rule.
- `forge/ui/index.html` contains the text `renderEvidence(t.evidence)` and the
  text `${esc(t.note)}`, so the controls were added beside the evidence and the
  note rather than in place of either.

## Notes

These criteria assert the wiring, and that is deliberate: `index.html` is the
one file in the repository nothing in the suite can execute, and the failure a
page ticket is most likely to have is the one wiring assertions catch — a
button that was never rendered, or a handler that posts to a path the server
does not serve.

What they do not settle is whether the controls read well against a real parked
ticket, and no criterion here can. A person answers that by running it:

```
forge ui --open
```

which serves the dashboard on `127.0.0.1:8799` without starting the loop. Open
a run holding a blocked or withheld ticket, type a sentence into the note field
under it, press `Add note`, and the status line beside the field reads `noted`
while the note appears in that ticket's evidence block on the next poll. On a
withheld ticket, `Release` returns it to the executor; `forge go` then picks it
up.
