# <Feature name>

<One paragraph: what this is for and what changes for a user when it lands.
Not parsed — it is here so a human reviewing the backlog knows what they are
looking at.>

## Design decisions

<Every sentence under this heading is recorded as decided and cannot be revised
away when a ticket is respec'd on retry. Put the load-bearing choices here —
the library, the algorithm, the file layout, the format — one per line.>

- The renderer is `tiny-skia`; `resvg` was rejected for binary size.
- Randomness is a xorshift32 seeded from the caller, so runs are reproducible.

## Out of scope

<What a reader might reasonably expect and will not get. Not parsed; it stops
the next person from filing the gap as a bug.>

## Running it

<Delete this heading if the backlog produces nothing a person starts. Otherwise:
the command that starts it, the control they use to give it something to work
on, what the status readout says, and what the screen shows before anything is
loaded. Each of those needs a ticket that owns it — a backlog can pass every
check it has and still produce something nobody can open.>

---

# AB-001: <short imperative title>

**Route:** delegate
**Kind:** feature
**Needs:**

## Spec

<Behavior, signatures, error handling, exact libraries. Resolve every design
question — the executor implements, it does not decide.

Write what to do. "Keep `shout` exactly as it appears, character for character"
is followed far more reliably than "do not modify `shout`", which names
everything except the thing you want. Where a property is genuinely negative,
say what happens instead alongside it.>

## Allowed files

- `path/to/file.rs`
- `path/to/other.rs`
- `path/to/file_test.rs`  <- the tester needs a path in scope; a bug ticket does not

## Reference files

- `path/to/read_only.rs`

## Acceptance criteria

- <An assertion that would fail if the behavior were wrong. Wrap it if it is
  long — an indented continuation joins; a flush-left one is dropped.>
- <Name the input and the expected output, including the error case.>
- <Pin the property that would actually be wrong. Counts and indices are easy
  to assert and easy to satisfy while the thing is still broken.>
- <State it in the positive: "returns `{x0:0,y0:0,x1:7,y1:5}`" rather than
  "never returns an x1 greater than level.w". A criterion phrased as a bound is
  satisfied by an implementation that returns nothing at all.>
- <For an entry point, assert the wiring mechanically and say so: "imports
  `composeFrame` and calls it", "contains a control with id `open`". Shallow,
  checkable, and enough to catch a control that is missing entirely.>
- <Say nothing about `npm test` / `cargo test` / the build exiting 0. The
  harness runs those before anything is judged, and a criterion repeating it
  gets encoded as a test that shells out to run the command.>
- <State what a shared file must *declare*. "exactly" / "nothing else" /
  "no other" about a file a later ticket also writes makes the first ticket's
  own test fail forever, and ingest refuses it.>

## Context

<Prior decisions and conventions that constrain this ticket, retrieved from
project memory. Reaches the executor ahead of what the loop retrieves itself.>

## Notes

<For the reviewer. Last section on purpose: anything under an unrecognized
heading is absorbed into the section above it, so free prose goes at the end.>

---

# AB-002: <next ticket>

**Route:** withheld:security
**Needs:** AB-001

<The reason goes inside the route — security, concurrency, interface,
compliance, performance, unresolved. There is no `**Reason:**` line; nothing
parses one, so a reason written on its own line reaches nobody.>

## Spec

<Same shape. A withheld ticket is still specified in full: the loop parks it
for a human, and the spec is what lets that human act without reconstructing
the intent.>

## Allowed files

- `src/auth/session.rs`

## Acceptance criteria

- <Assertions, same standard. These are what review is measured against.>
