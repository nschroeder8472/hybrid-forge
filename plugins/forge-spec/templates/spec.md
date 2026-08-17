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

---

# AB-001: <short imperative title>

**Route:** delegate
**Kind:** feature
**Needs:**

## Spec

<Behavior, signatures, error handling, exact libraries. Resolve every design
question — the executor implements, it does not decide. Write what to do, not
what to avoid: "keep `shout` exactly as it appears, character for character"
beats "do not modify `shout`".>

## Allowed files

- `path/to/file.rs`
- `path/to/other.rs`

## Reference files

- `path/to/read_only.rs`

## Acceptance criteria

- <An assertion that would fail if the behavior were wrong. One line — a bullet that wraps loses everything after its first line.>
- <Name the input and the expected output, including the error case.>
- <Avoid "exactly" / "nothing else" / "no other" about any file a later ticket also writes — ingest refuses that, and rightly.>

## Context

<Prior decisions and conventions that constrain this ticket, retrieved from
project memory. Reaches the executor ahead of what the loop retrieves itself.>

## Notes

<For the reviewer. Last section on purpose: anything under an unrecognized
heading is absorbed into the section above it, so free prose goes at the end.>

---

# AB-002: <next ticket>

**Route:** claude-only
**Reason:** <which triage category — auth, concurrency, migration, public API,
crypto, payments, or an unresolved design question>
**Needs:** AB-001

## Spec

<Same shape. A claude-only ticket is still specified in full: the loop parks it
for a human, and the human should not have to reconstruct the intent.>

## Allowed files

- `src/auth/session.rs`

## Acceptance criteria

- <Assertions, same standard. These are what review is measured against.>
