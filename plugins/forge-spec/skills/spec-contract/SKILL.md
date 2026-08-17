---
name: spec-contract
description: Use when writing, editing, or reviewing a specification that will be handed to `forge ingest` — the exact markdown grammar the parser recognizes, which fields are load-bearing, and the checks that make ingest refuse a backlog. Read this before authoring a spec so the document takes the parsed path and the acceptance criteria reach the executor as written, rather than being rephrased by a planner model.
---

# The spec contract

`forge ingest` has two paths, and which one a document takes is decided by its
shape:

- **parsed** — the document already contains ticket sections. They are used
  verbatim. No model runs, nothing is rephrased, and the criteria you wrote are
  the ones the executor is judged against.
- **planned** — the document is freeform, so the planner model converts it. The
  criteria become the model's wording.

Authoring for the parsed path is the entire point of writing a spec here.
Preferring it is not an optimization: re-planning a document that was already
planned is how a carefully specified ticket quietly turns into a different one.

A document takes the parsed path when it contains **at least one ticket header
and at least one `Spec` heading**. Anything less falls through to the planner.

## Grammar

### Ticket header

    # AB-014: Add PNG export
    ## AB-014 — Add PNG export
    ### AB-014 Add PNG export

One to three `#`. The id is `[A-Z][A-Z0-9]*-\d+` — letters, then a dash, then
digits. `AB-014`, `X1-7`, `IMG-102` all parse; `ab-014` and `AB014` do not, and
a `####` header is not a ticket header.

Prefix every ticket in one document with the same id stem, and number them in
the order they must land. Document order is the tie-breaker the scheduler uses
when two tickets write the same file.

### Marker lines

Each must start at the beginning of its own line:

    **Route:** delegate          delegate | claude-only   (default: delegate)
    **Kind:** bug                bug | feature            (default: feature)
    **Needs:** AB-012, AB-013    ticket ids that must land first

`Kind: bug` puts the ticket through reproduce-before-fix. Omit it for ordinary
work rather than writing `feature` on every section.

### Sections

Headings at one to four `#`, matched case-insensitively:

    ## Spec                       required — its presence is what makes this a plan
    ## Allowed files              bullets; the only paths the executor may write
    ## Reference files            bullets; pasted read-only into the prompt
    ## Acceptance criteria        bullets; what the tester encodes
    ## Context …                  free prose; prefix match, so any suffix works

Bullets are lines starting `- `, `* `, or `+ `. A bullet that is nothing but a
single code span loses its backticks, so `` - `src/a.py` `` and `- src/a.py`
are the same path.

**One bullet, one line.** Continuation lines are not part of the bullet — they
are dropped. A criterion wrapped across two lines reaches the tester as its
first line only, and the half you wrote to make it precise is the half that
disappears. Let the line run long instead of wrapping it.

**Anything under an unrecognized heading is absorbed into the section above
it.** A `## Notes` block placed between `Allowed files` and `Acceptance
criteria` puts its bullets into the allowed-files list. Put free prose last,
after `Context`, or fold it into a section that is parsed.

## What each field has to carry

**Spec.** Behavior, signatures, error handling, and the exact libraries to use.
Name the library — the executor implements, it does not choose. Resolve every
design question here; an unresolved one comes back as a `BLOCKED:` ticket, and
that is a spec defect, not a transient failure.

Write instructions as the behavior you want, not the behavior you forbid. "Keep
the existing `shout` function exactly as it appears, character for character"
is followed far more reliably than "do not modify `shout`" — a prohibition
describes everything except what to do. Scope is the exception, and it is
enforced mechanically by the allowed-files list rather than by persuasion.

**Allowed files.** Exact paths, and they are writable. Anything the executor
edits outside this list is rejected on apply. Guessing slightly wide is better
than omitting a file the work genuinely needs.

**Reference files.** The executor has no filesystem — it sees only what the
ticket carries and returns whole files as text. Any file it must read to get an
export name, a signature, an enum order, or a type right belongs here. A spec
that says "read `src/api.rs`" without listing it is asking for something the
executor cannot do, so it will guess instead.

**Acceptance criteria.** Assertions that would *fail* if the behavior were
wrong. "Returns `Err(ParseError)` for input missing a closing brace" is a
criterion. "Handles malformed input gracefully" is not. Be specific about
inputs and expected outputs, including errors.

**You author these, always.** A model that writes both the implementation and
the criteria it is judged against encodes its bugs as passing tests.

**Context.** Prior decisions and conventions that constrain this work — see the
`project-memory` skill. It reaches the executor ahead of anything the loop
retrieves on its own.

## Decisions are protected, prose is not

A sentence under a heading about decisions — "Design decisions", "Already
settled", "Do not revisit" — or a line that marks itself:

    Decision: randomness is a xorshift32 seeded from JavaScript.

is recorded as a decision and cannot be revised away when a ticket is respec'd
on retry. Unmarked prose can. If a choice is load-bearing but is not a
criterion, mark it, or it will survive only by luck.

## What makes ingest refuse

Fix these before handing the document over; each one is free to fix now and
expensive to discover mid-run.

**A dependency that does not resolve.** A `Needs:` id not in the document, a
ticket needing itself, or a cycle.

**A whole-file claim about a shared file.** When more than one ticket writes a
path, neither may assert the file's entire contents — "exactly", "only these",
"nothing else", "no other", "must not contain/declare/export/include". The
second ticket's job is to add to that file, and verification is whole-project
and permanent: the first ticket's own test then fails every ticket that
follows. State what the file must *declare*, not what it must contain and
nothing more. A file only one ticket writes may be pinned as tightly as you
like.

Two tickets sharing a file is otherwise fine. They get an ordering edge derived
from document order, so the later one sees the earlier one's work — those
derived edges are reported at ingest, and are worth reading.
