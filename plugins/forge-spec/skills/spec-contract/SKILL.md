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

    **Route:** delegate            delegate | withheld:<reason>  (default: delegate)
    **Kind:** bug                  bug | feature                 (default: feature)
    **Needs:** AB-012, AB-013      ticket ids that must land first

The reason travels inside the route, because every gate in the loop is written
against the whole value: `withheld:security`, `withheld:concurrency`,
`withheld:interface`, `withheld:compliance`, `withheld:performance`,
`withheld:unresolved`. The `delegation-protocol` skill says which is which. A
route the parser cannot read withholds the ticket rather than delegating it —
the gate fails closed.

`claude-only` still parses and still withholds, but it names the party who
decided instead of the objection and displays as `withheld:unspecified`. Write
the reason. There is no separate `**Reason:**` line; nothing parses one.

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

**A wrapped bullet joins, as long as the continuation is indented.** Indent it
by two spaces — what every markdown formatter produces — and the lines are read
as one criterion:

    - `screenToCell` with `origin {x:0,y:0}`, `scale 16` returns `{x:-1,y:-1}`
      for the point `{x:-1,y:-1}`.

A continuation written **flush left** is dropped, and that is deliberate: an
unindented sentence after a bullet is as likely to be a new paragraph as a
continuation, and joining it would fold prose nobody marked into a criterion.
So the criterion above, wrapped without the indent, reaches the tester as
everything up to "for the point" — precise-looking, and missing the point it
names. `/forge-spec-check` warns on exactly that case.

This one cost a backlog. A spec wrapped at 95 columns lost 31 of its 51
criteria to a parser that read one physical line per bullet; the sign-off pass
could not turn the fragments into assertions, respec filled the holes by
inventing values, and the run parked without a single attempt.

**Anything under an unrecognized heading is absorbed into the section above
it.** A `## Notes` block placed between `Allowed files` and `Acceptance
criteria` puts its bullets into the allowed-files list. Put free prose last,
after `Context`, or fold it into a section that is parsed.

## What each field has to carry

**Spec.** Behavior, signatures, error handling, and the exact libraries to use.
Name the library — the executor implements, it does not choose. Resolve every
design question here; an unresolved one comes back as a `BLOCKED:` ticket, and
that is a spec defect, not a transient failure.

Write it in the positive, as the section below sets out — this is the field
where it matters most.

**Allowed files.** Exact paths, and they are writable. Anything the executor
edits outside this list is rejected on apply. Guessing slightly wide is better
than omitting a file the work genuinely needs.

**List the ticket's test file here too.** The tester writes into a path the
ticket designates; with none designated the loop invents one beside the
ticket's workspace, which is outside the ticket's own scope — so if what the
tester writes there does not compile, the executor is refused every time it
tries to repair it and the ticket parks. One ticket died exactly there, on a
test asserting DOM properties its project does not define. A `bug` ticket is
the exception: its reproduction goes to a derived path granted as extra scope,
so it needs no entry. `/forge-spec-check` reports a ticket missing one.

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

**Leave the project's own commands out of them.** The harness runs lint,
typecheck, the build and the suite before anything is judged, and review only
happens on a tree where they passed — so "`npm run test` exits 0" is settled by
the run itself. Writing it down is not free: the tester's job is to turn every
criterion into an assertion, and one backlog that put that line on all five of
its tickets got a suite which shelled out to run all four commands and invoked
itself behind an environment guard to avoid recursing. It took ten times as
long and measured a different suite from the one that runs.

**Pin the property that would actually be wrong.** Counts, indices and
orderings are easy to assert and easy to satisfy while the thing is still
broken. A ruler shipped green against six criteria about how many labels are
emitted and which indices they carry; none of them said where a label sits
relative to the column it names, and every label was displaced by the
viewport's origin. Ask what a screenshot would show and write the criterion
that catches it.

**Context.** Prior decisions and conventions that constrain this work — see the
`project-memory` skill. It reaches the executor ahead of anything the loop
retrieves on its own.

## Write every ticket in the positive

State what the executor must do. A prohibition names everything in the world
except the thing you want, and leaves the model to infer the target from its
absence — which it does unreliably, and silently when it gets it wrong.

    do not modify `shout`
    keep the existing `shout` function exactly as it appears, character for
      character

Both describe the same outcome. The second is followed far more often, and when
it is not, the diff shows plainly what went wrong.

This applies to all three fields a person writes:

**Spec.** Say what the code does, what it returns, and what it does on the error
path. "Handles a missing file" is a gap; "returns `Err(NotFound)` with the path
in the message" is an instruction.

**Context.** Say what the next attempt should do with what was learned, not what
it should stop doing. "Imports in this package resolve with an explicit `.js`
extension" beats "do not omit the extension" — the first is a fact the executor
can act on before it makes the mistake.

**Acceptance criteria.** This is where a negative costs the most, because a
prohibition is usually satisfiable by doing nothing at all. One backlog shipped:

    `visibleCells` never returns `x1` greater than `level.w` nor `y1` greater
    than `level.h`, for a 7×5 level at `scale` 16 with `size {w:4096,h:4096}`

An implementation returning an empty window for every input passes that, every
time, forever. The positive form cannot be satisfied by a stub:

    `visibleCells` for a 7×5 level at `scale` 16 with `size {w:4096,h:4096}`
    returns `{x0:0, y0:0, x1:7, y1:5}`

Ask of every criterion: what does the code have to *produce* for this to hold?
If the answer is "nothing", it is a bound rather than an assertion, and a bound
catches no bug on its own.

### When the prohibition is the requirement

Some properties are genuinely negative — a key that is never written to disk, a
function that must not allocate, a migration that leaves existing rows alone.
Write those, and write them plainly. Two things make them carry:

- Pair each with a positive statement of what happens instead. "The key stays in
  the environment variable and only its name is written to config" says where
  the value goes, which "never write the key" does not.
- Give the criterion an observation that fails when the property is violated —
  a value read back, a count, a returned error. A criterion asserting an absence
  with nothing to measure passes on an empty implementation.

Scope is the case that needs no prose at all: `Allowed files` is enforced on
apply, mechanically, and a sentence asking the executor to stay inside it adds
nothing the harness is not already doing.

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

## Anything that can be run has to be runnable

If the backlog produces something a person starts — an app, a page, a server, a
CLI, a game — then **starting it, giving it something to work on, and seeing
what it did are part of the deliverable**, and some ticket has to own each one.
This is the requirement most often left out, because every other check passes
without it.

One backlog built a level editor: pure geometry, a draw list, a canvas backend,
a page, a ruler. Every criterion passed, 146 tests green, four commands clean.
The page opened to `no level loaded` and there was no way past it — the file
input was `hidden` and nothing on the page opened it. The editor could not be
used at all, and nothing in the run could tell.

Name all four:

**How it starts.** The exact command, declared in the project's own manifest so
it is the same command every time: `npm run dev`, `cargo run`, `python -m app`.
A thing started by a command that lives only in someone's memory is started by
nobody the following week.

**How a person gets data into it.** Name the visible control, not the
mechanism. "Offer a file input" is what produced the hidden input with no
trigger — the executor satisfied it exactly. "A button reading `Open level…` in
the palette pane, which opens a file picker filtered to `.txt`" leaves nothing
to interpret.

**How a person sees what happened.** The status line, the output, the exit
code, the rendered result. Say what it reads in each state, including the
states nobody enjoys writing down: nothing loaded yet, the input was rejected,
the operation is still running.

**What it shows before it has been given anything.** Every runnable thing has an
empty state, it is the first thing anyone sees, and it is the one screen a spec
never mentions. Say what it says and say what the way out of it is.

### Criteria for the parts a suite cannot reach

The entry point is usually the one file nothing in the suite can call — it is
where the DOM, the process, the sockets live. That is a real limit and pretending
otherwise produces criteria nobody can settle. Two things work:

- **Push the logic out of it.** Whatever the entry point does that is arithmetic
  or composition belongs in a module a test can call. What is left is wiring,
  and wiring is small.
- **Assert the wiring mechanically, and say that is what you are doing.** "the
  entry point imports `composeFrame` and calls it", "the page contains a control
  with id `open` inside the element with id `palette`", "clicking it opens the
  file input". These are shallow, they are checkable, and they catch the failure
  above — which was not a subtle bug but an absent control.

Then say in the ticket's Notes that the remaining question is whether it feels
right, and that a person answers that by running it. A criterion pretending to
settle that is a criterion nobody can settle.

### Hand over the command

When the backlog lands, the last thing the person needs is how to start what
they now have. Put it in the final ticket's Notes, or in the document's closing
prose: the command, the URL or entry point it produces, and what they should see
when it comes up.


## Give every call site an owner

A ticket can only write the files in its own `Allowed files`. So when ticket A
produces something ticket B has to call, **some ticket must be allowed to write
the file holding the call** — and it has to be a ticket that runs after A.

This is the failure that looks most like success. One backlog's last ticket
added a coordinate ruler and its spec said "the shell paints it after the level"
— a true sentence, in the only ticket that could not act on it, because the
shell's file belonged to a ticket that had already landed. The module was
written, tested, reviewed and recorded done, and nothing ever imported it.

Two habits close it:

- Write the integration into the ticket that owns the call site, not into the
  one that owns the thing being called. A sentence about file X belongs in the
  ticket allowed to write X.
- Prefer a shape where the join is itself testable. Composing two draw lists in
  a pure function that a test can call beats composing them inside the one file
  nothing in the suite can reach.

`/forge-spec-check` lists files a ticket writes that no other ticket names.
Entry points and config are excluded; what is left is usually a leaf module,
and occasionally this.
