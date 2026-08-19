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

## What is bug-loop-specific and what is not

| Mechanism | Scope |
|---|---|
| `failures._blocks` / `_ERROR` / `_CONTINUATION` | every step, every language |
| `errors_naming` reading blocks | all three call sites |
| `_widen_scope` on a blocked ticket | every ticket |
| `_recover_unlabeled` | every ticket with one writable file |
| `evidence.reading_scope` | ingest, bug, re-diagnosis |
| `_ground_references` / `evidence.locate_named` | every respec |
| `_refuse_verification_waivers` / `_disarmed_context` | every respec |
| `_unverifiable` / `StepResult.halt` | every ticket |
| `_quarantine` / `_red_left_behind` | every ticket that gives up |
| `_green_baseline` | once, before the first delegation |
| `environment_failure` / `_note_toolchain` | every verify step |
| `resumable_runs` draining | every command that opens a run |
| Reproduction, re-diagnosis, contradiction | bug tickets only |
