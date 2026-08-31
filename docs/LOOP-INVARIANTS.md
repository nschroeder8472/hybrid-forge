# Loop invariants

Rules that hold across the whole harness, extracted from the bug-loop repair of
2026-08-17. Every one of them was learned by breaking it, and most were broken
*again* while fixing something else — which is why they are written down here
rather than left in the commit that fixed each one.

Read this before adding a step, a role, or a check that attributes blame.

---

## 1. Reading and writing are different permissions

`allowed_files` is the ticket's contract. `reference_files` is what it can see.
Granting them as one list is how a role ends up diagnosing through a keyhole.

The case: a ticket scoped to `src/lib.rs`, which in that crate is four `pub mod`
lines and 62 bytes. Every role saw that file and nothing else, and the executor's
final answer was that the struct it had been told to fix "is likely defined in
`src/game.rs` ... outside the allowed scope I'm permitted to modify". It was
right, and it had no way to check.

**Widen reading freely; keep writing narrow.** `evidence.reading_scope` does the
first, and is wired into every path that sets a ticket's scope — `forge ingest`,
`forge bug`, and re-diagnosis. It costs nothing on a greenfield plan, because it
keeps only files that exist.

A corollary worth stating separately: **a scope that is only re-export files is
a mis-scope.** `lib.rs`, `mod.rs`, `__init__.py`, `index.js` declare modules and
hold no behavior to fix. `evidence.is_module_list` judges that on contents, not
on the name — an `__init__.py` with real code in it is a fine thing to scope a
ticket to.

---

## 2. Provenance decides what is immutable, not position

The loop protects a human's intent from its own drift. That protection is only
correct where the thing being protected actually came from a human.

`_merge_criteria` already had this right: criteria are scoped by who wrote them,
because a blanket freeze made a machine-invented criterion as immutable as a
human-authored one. The spec anchor did not get the same treatment, and it cost
a run.

Respec anchors on `original_spec` and is told "the original is the intent; treat
any difference as drift you should undo". On a **re-diagnosed bug ticket**,
`original_spec` is the *first hypothesis* — a cause the loop already disproved by
running a test. So the ratchet dragged the ticket back to the explanation it had
just ruled out. It re-diagnosed to `web/main.js`, reproduced the bug there, and
the next respec reverted the scope on the reasoning that "the previous revision
drifted into build/JS paths, but the original intent points to a Rust
initialization".

**Before treating any stored field as an anchor, ask who wrote it.** If the loop
did, it is a hypothesis and the anchor is somewhere further back — for a bug
ticket, the report, which no revision rewrites.

---

## 3. The party that benefits does not rule

Respec's job is making a failing ticket pass. That makes it the wrong role to
also decide that the assertion standing in its way is wrong.

So respec *proposes* retiring a stale assertion and the **reviewer** rules on it,
because the reviewer gains nothing from the ticket going green. Same shape
wherever a role would otherwise grade its own work: the executor does not author
the tests it is judged by, and the tester writes a bug's reproduction before any
fix exists.

**And an argument has to be enforced, not requested.** "Instruction-following is
not an access control" is already written in three places in this codebase about
*permissions*; it applies equally to *reasoning*. A `GRANT:` that never names the
file, or runs to two lines, is recorded as a refusal — because "the ticket cannot
pass otherwise" is true of every contradiction and settles none of them. The
argument goes to the run log verbatim either way: what a person wants later is
not that scope changed but why somebody thought the old assertion was wrong.

**Where this is knowingly bent, and why.** `loop.ratifyPasses` asks every role,
the reviewer included, to sign off on a ticket before it is built — so the
reviewer helps settle the contract it will later judge work against. That is a
weaker version of this rule, taken deliberately: the failure it removes (review
rejecting a diff over a bar the reviewer never agreed to, on a ticket that did
what it was told) has been observed, and the failure it risks (a reviewer
anchored on a contract it helped write) has not. The mitigations are that the
criteria may only move *before* any attempt exists — after that respec's
ratchet protects the ratified contract exactly as it protects a human's — and
that the whole argument is recorded on the ticket where a person can read it.

Being off by default used to be a third mitigation and is not any more: it is
`2` since the Puzzle-Path run of 2026-08-22/23, where two unbuildable tickets
reached the executor because nothing had asked whether they were buildable.
`ratifyPasses: 0` still restores the old behaviour, and the trade is argued in
[RATIFY.md](RATIFY.md). See [RATIFY.md](RATIFY.md) and
[CONVERGENCE.md](CONVERGENCE.md).

---

## 4. "Mentions the path" is not "is blamed for the path"

The most expensive bug of the repair, and it was in a helper three call sites
shared.

`errors_naming` scanned raw tool output for a filename. Every test runner
announces the targets it is about to run, and cargo does it by path:

```
     Running tests\bug_001_test.rs (target\debug\deps\bug_001_test-737.exe)
```

That line is printed whether the target passed or failed. So a ticket was found
in its own **success banner**, the amnesty concluded "the reproduction itself is
failing — not excused", and the ticket was failed **fifteen times across three
cycles** for a different ticket's red reproduction while its own was green.

**Attribute from diagnostic blocks, never from raw text.** `failures._blocks` is
the only correct input for any question of the form "is this file implicated".
`errors_naming` and `files_blamed` both read blocks now.

---

## 5. Every attribution needs the baseline

`_attempt` computes `already` (failing before this ticket started) and
`introduced` (this ticket's doing). Any new check that asks "is this file
implicated" must consume the same baseline, or it re-blames the whole repository.

The contradiction detector shipped without it and reported a *level* bug scoped
to `src/game.rs` as contradicted by an assertion about piece geometry — which had
been failing since before the ticket was filed, one line under an amnesty log
saying exactly that.

`files_blamed(output, exclude=already)` is the shape. It shares `_block_key` with
`signatures` so both agree on what counts as the same error; they have to, since
one is producing the set the other filters by.

---

## 6. A prompt is a promise the harness has to keep

The executor has been told for a long time that `BLOCKED:` "names the file you
need and why ... can widen the ticket". Nothing widened anything. The note
reached a human and the ticket parked, with the sentence naming the missing file
sitting in the block being read by nobody.

**Grep the prompts for capabilities before assuming they exist.** Where the
promise is worth keeping, keep it: `_widen_scope` now grants a named path that
already exists in the repository, once, without charging the attempt. Existence
is what makes it safe without a human — a model cannot invent its way into
scope — and `neverDelegate` is enforced exactly as everywhere else.

**A test file is never granted this way**, whatever the block says. The party
being judged does not get write access to the assertion judging it, and phrasing
the request as a block does not change that. That path goes through §3.

---

## 7. Recover the content before refusing the format

A reply that arrives unreadable may still contain the right answer.

The reprompt assumes the model misunderstood the format. Often it did not — it
reasoned at length about a hard ticket, quoted the current code in one fence,
emitted the whole corrected file in another, and left off the path line. Asked
again it produced *the same shape*, because the reasoning is what filled the
reply. Three of one ticket's five attempts, six of another's nine, every one
carrying a complete correct file.

Recovery is only safe with structural evidence, never on shape alone:

- The destination must be unambiguous — exactly one writable file, so nothing
  is guessed.
- The block must retain **80% of the top-level lines already on disk**. Measured
  against real replies the quoted fragment scored 17% and the whole file 100%,
  and one real reply held *only* fragments — writing it would have deleted three
  items from the file with a successful apply and nothing in the log.
- **Never** into a file that does not exist. With nothing to compare against,
  an illustrative snippet becomes the whole contents of a new module.

### The path inside the fence is the same lesson, at scale

Recovery above is for the reply that forgot its path. The commoner failure is
the reply that *has* the path and put it one line too low:

```java
// src/main/java/com/plexnamer/domain/MediaKind.java
package com.plexnamer.domain;
```

Sometimes bare rather than behind a comment marker; the two are one mistake.
Over a Java run of 36 executor attempts, **19 first replies were unusable and 7
attempts were lost outright**, every one of them to this shape. The model is not
disobeying — it is reproducing the most-attested shape in its training data, the
one every README snippet uses.

**Telling it made things worse, and that is the finding.** Corrected in as many
words that the path must go on its own line *before* the opening fence, one
reply moved the path from a `//` comment to a bare first line still inside the
fence — and dropped two files' `package` declarations while reformatting,
losing correct work to a header. Another dropped the path line entirely. The
correction reads as "put the path at the top" to anything that cannot already
see the boundary it is getting wrong.

So the harness absorbs it. `_paths_inside_fences` reads a block whose first line
names a file, strips that line, and writes the rest. Replayed over the run: 19
unusable first replies became 7, and 7 lost attempts became 3, with no change to
any reply that already parsed.

Two guards, and the first is not optional:

- **What is left after the path line must look like content.** One real reply
  was a fence holding the path and nothing else; reading it as that file would
  have truncated the file to empty. A fenced directory listing has the same
  shape. Every remaining non-blank line being itself a path means the block
  names files rather than being one.
- **Only when nothing parsed the ordinary way.** A reply that labelled even one
  file correctly is read as written. Mixing the readings would let a comment
  inside a properly labelled block invent a second edit out of the file's own
  first line.

Three things follow from the same evidence and are worth keeping together:

- **Show the format, do not only describe it.** `EXECUTOR_SYSTEM` and
  `TESTER_SYSTEM` had five prose bullets about the protocol and no example of
  it. They now carry a worked reply that parses — and the wrong shapes named
  explicitly, because a strong prior is better displaced than forbidden. A test
  runs the parser over the prompt itself: an example that does not parse teaches
  the wrong thing.
- **Correct with the ticket's own paths, not with prose.** The harness knows
  them — they are the scope it will enforce anyway — so the reprompt writes them
  out in the shape that parses, for the model to copy rather than construct.
  Globs are dropped: a scope rule is not a filename, and offering `src/**` as a
  line to copy invites a file called `src/**`.
- **A role whose output must parse should not inherit a sampling default.** The
  executor reached `temperature` 0.2 through `_call`'s default — the highest in
  the pipeline, on the one role whose reply has to be machine-readable before
  any of it counts, while the reviewer chose 0.0 and the tester 0.1. It now
  states 0.0. A model block's own `temperature` still overrides it, which is the
  point of `Provider.temperature`.

Related: `duplicate_paths` exists for a file that closes its own fence early and
gets re-parsed into blocks named from its prose — a spurious block that is never
identical to the first. A block repeated *byte for byte* is a model that
answered twice, and `apply_edits` writes in order, so the second write puts back
what the first did. Collapsing those keeps the guard pointed at conflicts.


---

## 8. One consumer taking the newest strands every other producer

Four commands open runs — `ingest`, `bug`, `go --plan`, `retry` — and `forge go`
took the highest id. Anything filed behind a run that then blocked waited for a
human to notice it, and `forge status` shows one run too, so it was not on screen
to be noticed. Two bug reports filed a minute apart: the first sat `pending`,
`attempts: 0`, for four days.

**A queue with several producers needs a consumer that drains it.** `forge go`
works every resumable run oldest-first, and `blocked` no longer abandons what is
behind it — only `stopped` (a person said so) and `failed` (something outside the
backlog is wrong) break off.

The same change breaks anything that assumed "newest run is the live run". The
dashboard did; it now follows the run the loop recorded itself entering, and
falls back to newest when that one is terminal.

---

## 9. Validate against recorded artifacts, not against reasoning

Every fix in this repair was replayed over the real step output in
`.hybridforge/artifacts/` and the `steps` table before it was believed. **Two of
them were wrong on the first attempt and only replay caught it:**

- The contradiction detector asked `errors_naming` whether the reproduction was
  implicated, and found it in its own `Running` banner — so it concluded the fix
  was not working and detected nothing. Synthetic fixtures passed.
- The unlabeled-file recovery first accepted a reply whose only block was a
  fragment. Replay showed it would have destroyed `src/piece.rs`.

A unit test asserts what you thought the output looked like. The artifacts hold
what it actually was. Write the fixture **from** the recording — the banner line
is in the regression test now because it is what broke the first attempt.

`forge replay` is this made routine:

```bash
forge replay                     # re-read every recording with today's parsers
forge replay --changed           # only what now reads differently
forge replay --run 3 --lens parse
```

It runs the current parsers over `.hybridforge/artifacts/` and, where the run
recorded what the parser produced at the time, says whether the answer changed.
Two lenses: **parse** reads model replies as files and compares against the
`written` list in the `apply` record beside them; **blame** reads command output
as diagnostics and compares `signatures(output) - pre_existing` against the
`introduced` set the run recorded. Exit status is 1 when anything reads
differently, so it drops into a pre-commit hook.

A difference is not automatically a regression. It is the set of past output
your change alters the reading of, which is the set worth looking at by hand.

Read-only, and honest about what it cannot reconstruct. Runs without artifacts
fall back to the `steps` table, which is clipped at 20k and records nothing
about what was read out of it — those can be re-read but not checked. Recorded
signature sets are clipped at twenty and say so rather than reporting a
difference nobody can act on. And the recovery of an unlabeled reply depends on
the ticket's scope and the file's contents *as they are now*, so it is labelled
rather than presented as what the run would have done.

Its own first real run is the argument for it: it reported three parser changes
that were not parser changes, because it paired each `apply` with the first
reply of the attempt rather than the nearest preceding one. An attempt holds
several replies and one apply. The tool caught that about itself, and the
pairing rule is now `pair_applies` with a test named for it.

---

## 10. A path handed to a role must resolve, or it is not a hint

`reference_files` exists to be read off disk. `sources_for` skips what it cannot
open, so a path that does not resolve does not reach the executor as a smaller
hint — it reaches it as nothing at all, and a reference the executor never saw
is indistinguishable downstream from one it read and ignored.

Respec names those files from memory. It is shown their contents, never the
tree, so it writes the package it remembers:

```
reference_files: src/main/java/com/plexnamer/DirectoryScanner.java
on disk:         src/main/java/com/plexnamer/domain/DirectoryScanner.java
```

with the rationale "added minimal stubs for these classes to reference_files".
The executor, shown nothing, imported `com.plexnamer.DirectoryScanner` — a
symbol that has never existed — and spent five attempts and the whole retry
budget being told so by javac, with the same wrong paths handed back each cycle.

**Ground a revised read scope against the tree.** `_ground_references` keeps
what exists, remaps a path whose filename matches exactly one file in the
repository, and drops the rest with the drop in the run log. One match only:
two is a guess, and the caller is better off told the path is wrong.

`allowed_files` is exempt and must stay exempt — a ticket's writable scope is
where its work is *going*, and most of it does not exist until the ticket runs.

**The document a backlog was planned from is a reference for every ticket in
it.** A planner reads a specification and writes a summary; the executor is
handed the summary and never sees the source, so anything the summary dropped
is gone. One run put a seven-hundred-line spec through that. Section 2 was
labelled normative and held the complete legal alphabet as a table of eighteen
characters, the seven exact error strings, and the order the checks run in.
What reached the executor was "reject bad input with exact error strings",
naming none of them, and every ticket in the backlog had `reference_files: []`.
The implementation shipped four of the eighteen characters, treated the exit
marker as a pushable block, and invented every message.

**A dependency's output is a reference for the ticket that depends on it.**
The mirror of the rule above, and it fails for the opposite reason: not a path
that does not resolve, but one that *will* resolve by the time the ticket runs
and is therefore never handed over at all. `reading_scope` keeps only files
that exist and expands siblings only into directories that exist, and it runs
once, at ingest — before a single ticket has produced anything. So a dependent
is scoped against a tree that does not yet contain what it depends on.

One backlog paid for that twice. PF-003 declared `needs: ["PF-002"]`, and PF-002
wrote the `LevelModel` type PF-003 exists to serialize:

```
PF-003  needs      : ["PF-002"]
        reference  : scripts/loaders/level_loader.gd, tools/path_forge/tests/smoke.test.ts
```

Four objections across two runs named it — the reviewer and the executor in one,
the executor and the tester in the other. Every one was correct and none was
actionable, and the ticket parked without an attempt. Ratification was working;
the loop had no remedy to offer it.

`_inherit_dependency_reads` recomputes the read scope when a ticket is picked
up, seeded with the `allowed_files` of everything in `needs`. Per ticket rather
than at ingest, because that is the first moment those files are on disk.
`allowed_files` is already exempt from the existence filter — "most of it does
not exist until the ticket runs" — and its readers inherit that exemption here.
A dependency that never ran contributes nothing, because `reading_scope` still
drops what it cannot open.

**A file the ticket's own words name is a file the ticket may read.** The same
gap without a dependency to open it: the read scope is derived from
`allowed_files`, which is what a ticket may *write*, and a ticket routinely
names files it must read and will never touch.

PF-009 of the next run paid for that one. Its spec told `_initialize` to load
`worlds/dragon_forest/world.json` and four levels by id; none of the five was in
its scope, and the executor refused sign-off on exactly that — correctly. What
followed is the part worth recording: the planner revised the **spec** to say
the level texts must be embedded as string literals, which made the ticket
genuinely impossible, and the two passes after it objected to the clause that
revision had introduced. The reviewer parked it as unwinnable, citing a
sentence no human wrote. Nineteen calls, a hundred and eleven minutes, no
attempt ever made, and every one of those files was in the repository
throughout.

An objection about missing evidence must be answered by widening what a role
can see, never by writing the gap into the contract. `evidence.named_paths`
reads the ticket's spec and criteria for two spellings — an explicit path, and
a bare name carrying a digit or underscore that exactly one file in the
repository has as its stem — and feeds them through the same existence filter
as everything else, so this cannot smuggle a path into a prompt either. It is
still bounded by `MAX_READING`: PF-009's scope goes from 7 files to the cap of
12, which is four of its five named files and all three it was refused over.

`cmd_ingest` now puts the source document first in every ticket's
`reference_files` when the backlog was **planned** from one — first because
`reading_scope` takes `reference` in order and caps the rest at twelve. A
backlog *parsed* from a ticket-shaped document gets nothing, because it already
carries that document's words verbatim and attaching it would show every ticket
every other ticket's spec.

The planner is told the other half: **never paraphrase a table.** A legend, an
alphabet, an error-message list or a status mapping goes into the ticket
verbatim, every row. Summarising one is not compression, it is deletion — the
executor cannot recover a row it was never shown.

---

## 11. Amnesty is measured, never asserted

What pre-dates a ticket is decided by `_baseline_failures`, per signature, from
a baseline the harness takes itself and records. A role writing the same claim
in prose is guessing about a tree it cannot see, and respec did:

> MainPanel.java ... currently has a pre-existing compilation error regarding
> com.plexnamer.model. Do not modify it ... **Ignore this pre-existing
> compilation error during verification.**

Every clause was false. The package had never existed, and the error was the
ticket's own scope failing to compile.

A bad spec is re-derived from the next cycle's failures. A waiver is not: it
lives in `context`, which survives every revision, and it teaches each new
attempt to discard the one diagnostic the next revision would be made from — so
the ticket stops accumulating evidence at all.

**Refuse a waiver on the way in and clear one already in.** Both:
`_refuse_verification_waivers` drops a revised `spec` or `context` that
introduces one, judged against the plan's text so a ticket legitimately talking
about tolerating errors can still be revised; `_disarmed_context` resets a
context poisoned by an earlier cycle back to the plan's paragraph. Only the
context — a spec legitimately evolves away from the plan's wording.

---

## 12. A step excused whole is not evidence

The amnesty stops one abandoned file failing an entire backlog, and its cost is
that an excused step ran no assertion about the ticket in front of it. On a
compiled language a red typecheck means the test binary was never built: the
suite did not run, the reviewer read a diff and said yes, and `done` came to
mean "a model liked the look of it".

One run marked five tickets done that way over a tree where `compileJava` failed
on the first file it read — each one logging `typecheck still failing, but only
on errors that pre-date this ticket` — and took 168 minutes and 2.4M tokens to
do it. The end-of-backlog check caught it, which is to say it caught it after
every ticket had already been reported green.

**When nothing was verified, stop the run, not the ticket.** `_unverifiable`
gates on two conditions, and the second is what keeps it off ordinary work:

- Not one verify step passed. A green typecheck beside an excused suite still
  means the code compiled.
- Some ticket that may write a red file has already given up. Red owned by a
  *pending* ticket is a backlog mid-flight — a JVM plan is routinely red between
  the ticket that calls a class and the one that writes it — and red owned by
  nobody is an orphan, which `_sweep_orphan_tests` and `_finish` handle. Red
  owned by a ticket that is out of attempts is the one case where nothing
  coming will clear it.

The run ends there because the next ticket meets the same wall, and a retry
cycle only requeues tickets into it. `StepResult.halt` is checked before
`blocked` on purpose: the note names the red files, and `_widen_scope` reads a
block note for exactly that.

---

## 13. "The command failed" is not "the code is wrong"

Every diagnosis in the loop assumes the failing command reached the source. A
launcher that never started breaks that assumption silently, because what it
prints is not a diagnostic:

```
FAILURE: Build failed with an exception.
* What went wrong:
Gradle requires JVM 17 or later to run. Your build is currently configured to use JVM 8.
```

`signatures` returns the empty set, so there is nothing to attribute and the
baseline treats it as excusable; `distill` keeps it whole; and it arrives in the
executor's prompt as the thing to fix. The executor answers that the build
environment is misconfigured and writes no files — which is recorded as a reply
that did not parse, and costs a reprompt and then an attempt. It is right every
time, and it is charged for being right.

A run did this for ten minutes, thirty model calls and 131k tokens before a line
of code was written. It also looked like a model problem in the log: 31 replies
that "did not parse into files", 23 of them from a model refusing to invent
code for a broken JAVA_HOME.

**Name the environment failure and end the run on it.** `environment_failure`
holds the spellings — command not found in each shell's wording, a JVM or
runtime that will not start, a missing interpreter module — and
`_note_toolchain` records the first. Two conditions, and the second is the
guard: the output must match, **and** `signatures` must be empty. A suite
asserting on the text of a shell error is real output about real code and prints
a diagnostic block beside it.

All three places a verify command runs consult it, and the third matters as much
as the first: the baseline before a ticket is delegated, the verify step after
one is, and `_finish`'s final check over a completed backlog — where every
ticket is green and `backlog complete but typecheck still fails` would read as
work the loop left undone rather than as a command that never started.

The run ends `failed`, not `blocked`, and the ticket goes back to `pending`:
nothing is wrong with the backlog, and `forge go` should resume it unchanged
once the machine is fixed.

---

## 14. Red the backlog did not start with, and did not cause, is nobody's

Invariant 12 stops the run once a ticket has been checked by nothing. It is the
last line, not the first: by the time it fires, a ticket has been delegated,
tested, verified and reviewed to establish something that was true before it
started. The same red has two earlier moments where it is cheaper to catch, and
each one was open.

**A ticket that gives up leaves its breakage in the tree.** Nothing reverted a
failed ticket, on the grounds that a human may want to salvage what it wrote.
The argument is good; the placement was not. Verification is whole-project, so
an abandoned file that does not compile is reported to every later ticket, and
because it is outside their scope the baseline excuses each of them for it —
which is exactly the state invariant 12 exists to stop the run over. One
abandoned file therefore ends the run, and everything downstream of it in the
backlog is unreachable no matter how independent it was.

A Java run went that way. `PN-003` spent five attempts and finished with
`variable resultBaseName might not have been initialized` in its own file.
`PN-004` was skipped for depending on it. `PN-005` — a filesystem ticket with no
relationship to any of it — inherited a red typecheck, therefore a test suite
that was never built, therefore two excused steps and no evidence, and the run
stopped there with two of seven tickets done.

**Take the work out of the tree and keep it out of the way.** `_quarantine`
restores each file the ticket's own `apply` steps wrote to its version in
`ticket.baseline_tree` — removing it where the baseline had none — and copies
what was there to `.hybridforge/abandoned/run-N/<ticket>/` first. Salvage
survives; the compiler stops seeing it. Three details carry the weight:

- **The paths come from the applies, not from a diff.** `baseline_tree` is
  pinned for the ticket's whole life, so a diff against it on a retry cycle
  also names work other tickets landed in between. And a glob in
  `allowed_files` is a scope rule rather than a filename — expanding one to
  decide what to delete reaches further than the ticket ever did.
- **No copy, no revert.** A file whose quarantine copy could not be written
  stays where it is. Losing the work is worse than leaving the tree red, and a
  red tree is a state the loop already detects.
- **No baseline tree, no revert.** A repository without git has no version to
  restore to, and deleting on a guess takes a hand-written file the ticket was
  asked to extend. This is why the charged-failure subtraction in invariant 11
  stays load-bearing rather than becoming redundant.

**Ask about the tree where the ticket gave up, not at the next one.**
`_red_left_behind` runs the verify plan the moment a ticket fails or blocks,
using invariant 12's ownership rule with the ticket that just gave up counted
among the owners. Usually a no-op, because quarantine has already made the tree
green; it is what catches the cases quarantine could not fix. It is skipped when
nothing runnable is left, because `_finish` runs the same commands next.

**A tree that was red before the run is red for every ticket at once.** This is
the second gap, and invariant 12 cannot see it at all: red in files no ticket
owns has no exhausted owner to point at, so `_unverifiable` returns nothing and
the whole backlog runs verified by nothing until `_finish` says so — after every
ticket has been spent. `_green_baseline` runs the verify commands once before
the first delegation and refuses to start.

Two failures are deliberately not gated on, and both follow rules already
written down here:

- A command that never reached the code. That is invariant 13, and
  `_stop_for_toolchain` says what is actually wrong instead of blaming the tree.
- A failure naming no file. `pytest` exits 5 on a repository with no tests and
  `npm test` fails with no script — a greenfield project is the normal way a
  backlog starts, and the run that produced this document began on an empty
  repository. Gating there would make the check fire hardest on the runs it has
  nothing to say about, so an unattributable failure is reported and the run
  continues.

The first unit of work on a red repository is fixing the red, and that is a
ticket a human writes: the loop cannot scope it, because the files it would have
to authorise are precisely the ones no ticket claims. `requireGreenBaseline` and
`quarantineFailed` both have off switches for the repository whose red is what
the backlog is there to fix.

---

## 15. Code that cannot be loaded is not code the loop can check

Every gate downstream of the executor assumes the file can at least be read by
the toolchain. A module that imports something which does not exist breaks that
assumption before any of them run, and each one then reports the absence of
evidence as the absence of a problem: the apply step succeeds because the file
landed, verification is silent because a suite that cannot import a module
reports no failures about it, and the reviewer reads a diff that looks entirely
correct.

One run went fifteen tickets that way. Sixteen relative imports across eight
invented module paths — `../types`, `../geometry`, `../model/rect`,
`../models/level_model` — each ticket reaching for a shared module it had been
told to use and was not allowed to create, and no ticket in the backlog owning
any of them. The run finished `done`, "all tickets complete", over 4,000 lines
that had never been compiled.

**Check that a relative import resolves, at apply, before anything expensive.**
`imports.unresolved` reads the files the attempt just wrote, extracts every
relative import, and asks whether any spelling of it is a file on disk or a
path some ticket in this backlog is allowed to write. It is a regex and a
`stat`: no toolchain, no model, and no ecosystem knowledge beyond one pattern
per language family. It fails that run's first ticket on its first attempt.

Three properties keep it from being noise:

**Only relative imports.** A bare specifier is a package or resolves through
configuration this cannot see — a `tsconfig` alias, a `PYTHONPATH`, an include
directory. A false failure here costs an attempt and tells the executor to fix
code that was never broken, so the check declines to have an opinion.

**A module another ticket will write is not a miss.** It is a declared future
file, and failing over it would make a correct plan unrunnable: writing the
caller before the callee is ordinary, and `needs` is what sequences them. When
the sequencing has *not* been declared the run log says so, because until the
callee arrives the caller is verified against a module that is not there.

**It is guidance, not a park.** The import may be a typo the next attempt
fixes. Where it is not, the executor already has a `BLOCKED:` protocol for
asking for the file — which is the right request, and one a human can act on.
Exhausting the attempts parks it the ordinary way.

Python is read with `ast` rather than a pattern, because the standard library
parses it exactly and a regex finds `from .` inside a docstring. A file that
does not parse yields nothing: it has a worse problem than an unresolved
import, and the toolchain will say so.

---

## 16. A green from a command that read none of the tests is not a green

Verification passing is taken as evidence about the ticket in front of it. That
holds only while the command actually collected the ticket's tests, and nothing
checked. One run recorded fifteen greens on a command that had read none of
them: the tester wrote `node:test` suites into a directory a gdUnit4 launcher
globbed and ignored, the launcher exited 0 every time, and each `05-verify-test`
artifact says `"status": "ok"`.

The preflight canary (§`WORKSPACES.md` phase 3) closes most of this before the
first ticket — it proves the command reads the language at the place
`_test_target` puts files. What it cannot prove is that *this* file was
collected, because a planner may designate a test path of its own that sits
somewhere the runner never looks. `_test_was_collected` asks that, once per
passing test step, and only when the tester wrote a file that did not exist
before.

Three answers, and the third is what keeps it honest:

- **The output names the file.** Collected. Most runners print what they run,
  and this is the ordinary case — no counting required.
- **The suite did not grow.** A file that did not exist was written, the runner
  reported a count before and after, and the number is the same. Nothing else
  explains that.
- **No count either way.** `go test` prints `ok pkg 0.01s` and no number at
  all, and a runner this does not recognise prints one it cannot read. *Cannot
  tell* is a real answer and is reported as one, once per run. Reading it as
  "fine" is the failure being fixed; reading it as "broken" would fail every Go
  project in existence.

**The retry is where this goes wrong, and the guard is `authored_now`.** On a
second attempt the previous attempt's test file is already on disk and already
counted in the baseline, so the suite does not grow and is entirely correct not
to. The check applies only to a file that did not exist when the baseline was
taken.

`_baseline_counts` is captured in `_baseline_failures` whether or not the step
passed — it is not about failures at all — and re-taken per ticket, because a
count carried from the previous ticket is a count from a different suite.

---

## 17. A language the repository cannot build is a hole no gate downstream sees

A backlog creating a whole new language tree in a repository with nothing to
build it is a shape worth naming on its own. Fifteen tickets wrote 4,000 lines
of TypeScript into a repository with no `package.json`, no `tsconfig.json`, and
no ticket owning either. Nothing could compile, type-check or test a line of
it, and every gate downstream read the absence of complaints as the absence of
a problem.

`toolchain.manifest_gaps` asks it at ingest and again at run start: for each
language this backlog writes whose ecosystem cannot build without a manifest,
does the owning workspace have one — on disk, or created by a ticket in the
same backlog? Writing the build file and the first module it builds is an
ordinary way to start, so a ticket that creates it closes the question.

**The table is deliberately short.** `LANGUAGE_MANIFESTS` lists only the
ecosystems where the answer is unambiguous — npm/deno, cargo, go, the JVM
builds, SwiftPM, mix, pub, sbt. Python is the instructive omission: a directory
of standalone `.py` files with no `pyproject.toml` is ordinary and runs
perfectly, so listing it would report a hole in half the repositories that
exist. Ruby, PHP, C and shell are out for the same reason.

**It warns; it does not refuse.** Every other check of this family refuses, and
this one cannot, because a refusal here has no escape hatch. `commands` has an
exemption spelling for a language nothing runs — there is none for "this
project builds its TypeScript with a Makefile and no `package.json`", which is
unusual and not wrong. The gates that *do* refuse catch the consequences
anyway: an unowned file is refused at ingest, and a canary that stays green
over a file that cannot parse stops the run. What this adds is the **cause**,
named at the one moment fixing it costs a single extra ticket.

It is keyed by the manifest rather than the extension, because `.ts` and `.tsx`
are one ecosystem with one `package.json` and reporting them separately says
the same sentence twice about the same missing file.

**A backlog where nothing is built on anything is the same hole, one level up.**
`ingest.undeclared_order` reports a plan of four or more tickets, mostly
creating files that do not exist, with no `needs` between any of them. That is
either genuinely parallel work or a planner that decomposed by file instead of
by unit of work — and the second is what shipped the defect. Nothing sequenced
the shared type ahead of its consumers and no ticket owned it, so each module
in turn reached for it, invented its own name for it, and imported a file
nothing would ever write.

Greenfield is the discriminator, and it has to be a majority of the files: a
batch of independent fixes to code that already exists is genuinely parallel
and stays quiet. `derive_needs` has already run by then, so a shared *writable*
file has been ordered and is not what this is about; what is left is the
dependency nobody could see from the paths alone.

This is the one check in the family reasoning from the **absence** of
something. The evidence is circumstantial — the tickets in that backlog did not
name each other's files or ids anywhere in their prose, so the shape was all
there was to go on — and it warns for that reason. Being told about a real
parallel backlog costs a reader five seconds; not being told about the other
kind cost fifteen tickets.

---

## 18. A declaration deleted by a whole-file rewrite is invisible to verification

The executor returns whole files. That is what makes its output parseable
without a diff format, and it is also the one edit shape that can delete
something by omission: a file reproduced from memory comes back missing
whatever the model did not think to copy.

Most of the time something downstream notices. The exception is a build
manifest, and the reason is structural: **verification runs where the
dependencies are already installed.** They have to be, or the commands could
not run at all. So a `package.json` that lost its `devDependencies` passes
lint, typecheck and the whole suite exactly as it did before.

One run lost six of them out of a ticket that was adding two scripts. Every
command exited 0, the reviewer read a diff that added what the ticket asked
for, the ticket was recorded `done`, and on a clean checkout `npm ci` installed
one package and every command in the project failed to start. Each of those
tickets carried the criterion "lint, typecheck and test all exit 0" — true on
the machine that verified it and false everywhere else.

`forge/manifests.py` reads the manifest before `apply_edits` writes over it and
compares what it declares afterwards. Before, not from git: a manifest an
earlier attempt on the same ticket already rewrote has no clean version left to
diff against.

**Names, never versions.** A ticket that bumps or loosens a constraint is doing
ordinary work. A ticket that drops the entry is not, and the difference is the
whole check.

**An unreadable manifest reports nothing.** A syntax error is a defect the
language's own tooling describes far better than this can, and reading it as
"declares nothing" would turn every such failure into a dropped-dependency
complaint pointing at the wrong file. The same silence covers a TOML manifest
on Python 3.10, which has no TOML reader in the standard library.

**It fails the attempt rather than parking the ticket.** The fix is the
smallest there is — the executor emits whole files and has to send the block
back — and the guidance names the lost entries so it does not have to
reconstruct them from the memory that lost them.

The general rule this is one instance of: **a check that runs in an environment
the deletion does not affect cannot see the deletion.** The same shape exists
for anything else a whole-file rewrite can drop silently — a barrel module's
re-exports, an ignore file's entries, a CI matrix leg. Only the manifest case is
enforced, because only there is the invariant mechanical enough to compute
without guessing.

---

## What is bug-loop-specific and what is not

| Mechanism | Scope |
|---|---|
| `failures._blocks` / `_ERROR` / `_CONTINUATION` | every step, every language |
| `errors_naming` reading blocks | all three call sites |
| `_widen_scope` on a blocked ticket | every ticket |
| `_recover_unlabeled` | every ticket with one writable file |
| `_paths_inside_fences` / `_drop_repeats` | every executor and tester reply |
| `evidence.reading_scope` | ingest, bug, re-diagnosis |
| `_ground_references` / `evidence.locate_named` | every respec |
| `_refuse_verification_waivers` / `_disarmed_context` | every respec |
| `_unverifiable` / `StepResult.halt` | every ticket |
| `_quarantine` / `_red_left_behind` | every ticket that gives up |
| `_green_baseline` | once, before the first delegation |
| `_canary` | once, before the first delegation, per build per language |
| `imports.unresolved` / `_dangling_imports` | every attempt that writes a file |
| `_source_reference` | every planned ingest |
| `_trim_reference` | every reference file over the source limit |
| `failures.test_count` / `_test_was_collected` | every passing test step on a ticket that authored one |
| `toolchain.manifest_gaps` | every ingest, and once at run start |
| `ingest.undeclared_order` | every ingest |
| `environment_failure` / `_note_toolchain` | every verify step |
| `resumable_runs` draining | every command that opens a run |
| `ratify.resolve` / `_scope_gate` re-run | every ticket, when `ratifyPasses` is on |
| Reproduction, re-diagnosis, contradiction | bug tickets only |
