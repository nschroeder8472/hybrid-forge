# Loop repair plan

Derived from the `alllocal` run of 2026-08-12 (`.hybridforge/run.db`, run 1):
six tickets, three cycles, 27 executor attempts, 12 planner calls, zero tickets
completed. Every file anchor below is against `hybrid-forge` at `961b06b`.

The run failed for four independent reasons that compounded. Ordered here by
what unblocks what, not by severity.

**Status.** Everything in Phase 1, plus 2.1, 2.2, 3.4 and 1.6, has landed on
`fix/loop-repair`. As of run 5 the `alllocal` backlog completes: all six tickets
`done`, lint and typecheck clean, 36 tests passing across four files. Each fix
below records what it was replayed against, and where a fix could not be
verified against surviving data that is said plainly.

Everything else has since landed on `fix/loop-repair-open-items`: 3.2 and 3.5,
then 2.3, 3.3, 3.6, 3.1 with its promotion path, and 4.1 behind a flag that is
off. All of it is unverified against a run — written from run 5's evidence and
covered by tests, not by a replay.

Still open: the conditional 2.4, and the measurement 4.1 asks for. A clean
run from an empty repository — no drifted specs carried over — is the validation
that has not been done yet.

---

## Phase 0 — Baseline: done

Re-run completed 2026-08-12 with `reviewer` pointed at `local-plan`. Fresh
`run.db`, same `plan.md`. Result: TT-001 and TT-005 **done in one attempt each**;
TT-002 and TT-006 failed; TT-003/TT-004 skipped; stopped at cycle 3 on the
evidence fingerprint.

Every Phase 0 prediction held.

| Predicted | Outcome |
|---|---|
| TT-001 false rejection disappears | Gone — `review ok`, one attempt (step 10) |
| TT-005 false rejection disappears | Gone — `review ok`, one attempt (step 35) |
| TT-006 false rejection disappears | Gone as a *false* rejection; TT-006 is now rejected for a defect that is genuinely on disk |
| "no file edits" survives | Survives — TT-002 and TT-006 both end on it |
| ratchet warnings survive | Survives, model-independent — event 38 is 13/13 marker echo |

The reviewer swap was decisive and is not in question. Its rejections were
checked against disk and are accurate.

**What the clean baseline then exposed:** two failure modes that the old run's
reviewer noise was hiding. Both are more severe than anything originally in this
plan, and one of them destroys files. They are 1.0 and 1.5 below, and the
original 1.2 has been rewritten because its premise was wrong.

---

## Phase 1 — Unblock the loop

### 1.0 The output parser shreds correct executor output — **done** (`308fb6a`)

**Highest severity item in this document. It loses files silently.**

Replayed both
parsers over every real executor response in `alllocal` and `alllocal2`: 14
identical, 6 changed, and all six are the TT-006 corruption — steps 42 and 59
(the destructive phantom-only parse) now yield no edits, and steps 57, 58, 79,
80 keep the two genuinely complete files while withholding the truncated README
and its phantom.

One correction to the fix as originally written below: item 2, *"prefer the
longest fence"*, was dropped. There is no longer fence to prefer — the model
emitted three backticks and nothing else, and the regex is already
CommonMark-correct in requiring a closer at least as long as its opener. The
burden is on the *opening* fence, which the parser cannot repair after the fact.
Detection and refusal are the whole fix.

`_BLOCK` (`forge/patch.py:32-38`) requires a bare path line followed immediately
by a fence. Run 2 produced three distinct failures of that contract, and the
loop reported all three as the same thing: `"executor returned no file edits"`.

**Mode A — outer fence closes early, phantom edit applies.**

TT-006's executor emitted all three files correctly. `README.md` was opened with
a three-backtick fence and its body contains ` ```bash ` blocks, so the outer
fence bound to the first inner closer. README was truncated to ~200 bytes and
the remainder was rescanned as loose text, yielding a fourth, invented edit:

```
path './build.sh', 57 bytes:
"\n### Using build.ps1 (Windows)\n```powershell\n.\\build.ps1\n"
```

Steps 57, 58, 79, 80 parse to four edits — the three real files plus this
phantom. `duplicate_paths` catches that, correctly, because `./build.sh`
normalises onto `build.sh`.

Steps 42 and 59 parse to **the phantom alone**. Nothing collides, so nothing is
caught: `apply ok :: ./build.sh`, `build.sh` is overwritten with a fragment of
the README, and `build.ps1` and `README.md` are never written at all. On disk
right now, `build.sh` is 61 bytes of markdown and the other two files do not
exist.

The system prompt does tell the executor to use a longer fence
(`forge/prompts.py:44-47`). It did not comply. The harness converting that into
a silent overwrite is the defect.

**Mode B — fenced content with no path line.** TT-002 steps 51, 52, 53, 74, 75
each contain a complete, syntactically valid `src/board.rs` inside a fence, with
no path line before it. Zero edits parsed. Reported as "no file edits."

**Mode C — path line inside the fence.** Step 81: ` ``` ` / `build.sh` /
content / ` ``` `. Zero edits parsed. Same message.

**Mode D — path line, contents not fenced at all.** Found while implementing:
TT-006 steps 40 and 41 name each of the three files on its own line and then
follow it with the raw file, no fence anywhere. Zero edits parsed, same message
again. Diagnosed separately, because the reply is otherwise correct and telling
it "no file edits" sends the next attempt rewriting code that was already right.

**Why this matters beyond the lost files.** The reviewer's rejection (steps 47,
64) is *accurate about disk* — `build.sh` really does contain markdown. Respec
read that as a model-behaviour problem and wrote the misdiagnosis into the
ticket (event 68), so TT-006's spec now opens:

> "Create exactly three files with raw contents below, not markdown prose
> describing them."

The executor was never producing prose. A parser bug propagated into permanent
spec corruption, through a reviewer that was telling the truth.

**Fix.**

1. **Refuse to apply a suspect parse.** After matching, rescan each captured
   body for an unbalanced fence. A body containing an opener with no closer
   means the outer fence closed early; fail the attempt with the existing
   four-backtick guidance rather than applying anything. This is mechanical and
   catches Mode A before it reaches disk.
2. ~~**Prefer the longest fence.**~~ Dropped — see the note above.
3. **Say which failure it was.** Split `"executor returned no file edits"` into
   distinct messages for *nothing at all*, and for modes B, C and D, and feed
   the specific complaint back as `failure_context`. The single message sent
   respec chasing spec defects that did not exist (events 36, 64) when the fix
   was a missing header line.
4. ~~**Not done, still open:**~~ **Landed** — when a ticket authorises exactly
   one file and the reply is a fenced block with no path line, infer the path.
   Safe for a one-file ticket; not attempted for TT-002's two. It needed two
   guards the original sketch did not have; see *Recovering a whole file that
   forgot its name* below.

**Landed as.** `_fence_is_too_short` and `ParsedOutput.truncated` in
`forge/patch.py`; `describe_unparsed` in the same module; the refusal ahead of
`duplicate_paths` in `forge/loop.py`.

**Tests.**

- `test_a_file_cut_short_by_its_own_fence_never_becomes_an_edit`
- `test_the_phantom_alone_is_still_refused`
- `test_a_file_whose_fences_are_shorter_than_its_wrapper_is_kept`
- `test_a_file_cut_short_by_its_own_fence_never_reaches_disk` (end to end,
  asserting the file on disk is byte-identical afterwards)
- `TestUnparsedOutput`, one case per mode plus the two negatives

**Risk.** Low, and strictly protective — item 1 only ever refuses work it
currently applies wrongly.

**Open question, since resolved — partial application.** 1.0 originally refused
a whole response when any block was truncated, on the `duplicate_paths`
precedent. Run 3 showed that was wrong, and that the precedent does not apply:
duplicates mean one path has two candidate contents and there is no way to tell
which was meant, while the *non*-truncated blocks of a mixed response are
unambiguous.

The cost was concrete. TT-006 steps 126 and 127 each carried a correct
`build.sh` and `build.ps1` beside a truncated README. Refusing all three left
the 61-byte corrupt `build.sh` — written before 1.0 landed — with no way to be
replaced, so the ticket could not finish whatever the executor sent. 1.0
prevents new corruption but cannot heal old corruption, and all-or-nothing
removed the only route back.

Now only the truncated paths are withheld; the clean ones are written and the
attempt fails naming both halves, so the next attempt sends only what is
missing. It stops after apply rather than continuing to review — the response is
known to be incomplete, and asking a reviewer to confirm that costs two model
calls.

---

### 1.7 Respec must not write the executor's reply format — **done**

**Found in run 3.** TT-006's spec, after respec:

> "Output their raw contents directly in your response, one per file, prefixed
> by the filename. **Do not wrap file contents in markdown code fences** or
> surround them with prose."

`EXECUTOR_SYSTEM` requires a fenced block after each path line and `_BLOCK`
cannot match without one, so the spec instructed the executor to guarantee that
nothing parses. Meanwhile 1.0's Mode D message told it the opposite. The ticket
was unsatisfiable by construction, and the criteria guard did not cover any of
it because none of it was a criterion.

The mechanism is the same one behind the run-2 misdiagnosis, one level worse.
Respec sees a ticket that failed and reaches for the nearest cause; when the
failure is "your output did not parse", the nearest cause looks like the output
format. But the format is the harness's contract — stated to the executor
directly, parsed on the way back — and respec has never been shown it, so every
sentence it writes about it is a guess that can only contradict.

**Landed as.** A rule in `RESPEC_SYSTEM` saying the reply format is not the
planner's to describe, and `_refuse_protocol_edits` in `forge/respec.py`
enforcing it — because the prompt is not an access control, the same reasoning
that put the criteria guard in code.

Judged on what a revision *introduces*, measured against `original_spec`, so a
ticket whose plan legitimately discusses fences — a markdown renderer, a docs
generator — stays revisable. The offending field is dropped and the previous
value kept, matching how a refused criterion is handled.

Replayed against the spec respec actually wrote in run 3: both `spec` and
`context` dropped, on `'code fence'` and `'markdown prose'`.

**Tests.** The real spec dropped, context guarded the same way, an ordinary
revision untouched, and a fence-flavoured plan still revisable.

**Risk.** Low-moderate. The phrase list is a heuristic and will not catch every
paraphrase. It is a floor, not a proof — the prompt rule is what carries the
common case.

---

### 1.8 Ask again inside the attempt when a reply does not parse — **done**

**Found in run 4**, the first clean run with every earlier fix in place. Four of
six tickets passed, including TT-006, which converged file-by-file exactly as
partial application intended:

```
apply ok :: build.sh, build.ps1
apply ok :: build.sh, build.ps1
apply ok :: build.sh, build.ps1
apply ok :: build.sh, build.ps1
apply ok :: build.sh, build.ps1, README.md   <- passed
```

TT-003 did not, and the reason is not the work. Of its nine attempts:

| outcome | count |
|---|---|
| parsed, applied, reviewed | 3 |
| **no parseable output** | **6** |

Every one of the six was Mode B — a fenced block with no path line — and the
pattern is that the executor drops the path line whenever it narrates before
answering. The whole third cycle produced nothing parseable, so respec's second
revision was never tested against anything.

The three attempts that did land drew accurate, specific review objections: the
RNG reset on every lock instead of persisting, and scoring applied after the
level update rather than before. Answerable defects. It never had the budget
left to answer them.

**Landed as.** `_malformed_reply` on the orchestrator, and a `for remaining in
(1, 0)` loop around the build call — the same shape the tester already uses to
reprompt a rejected test file. The complaint goes back through a new `malformed`
section of `build_prompt`, which tells the executor the code was never the
problem and not to rewrite it.

Refused and asked again: unreadable content (modes B, C, D), a short fence with
nothing clean beside it, and duplicate paths. **Not** reprompted: a `BLOCKED:`
reply (a decision), a reply carrying no file content at all (1.2 — may be a
finished ticket, and asking again would talk it into inventing an edit), a
partial parse (already written, and a second ask could trade it for worse), and
a response cut off at the output limit (the retry gets the same budget and runs
out of it the same way).

Once only. A model that cannot follow the format twice will not follow it on the
third ask, and the attempt should end while the evidence is fresh.

Replayed over every build response recorded in run 4: **8 replies now get a
second ask, 11 proceed unchanged** — six of the eight are TT-003's.

**How it actually performed.** On its first outing it fired four times and
recovered nothing: TT-003's failure was a *stable* shape, `#### src/game.rs`,
and a model told its path line is missing has nothing to act on when it does not
experience that line as missing. That is 1.9, and it is the fix that mattered
for TT-003.

The reprompt still earns its place. Once 1.9 landed, TT-003's winning attempt
opened with an unreadable reply, was asked again, and passed review on the
second — a transient slip, which is what this is for. The lesson is the split: a
reprompt answers a slip, a parser change answers a shape. Check whether the
failure repeats before reaching for the prompt.

**Tests.** `TestAnUnreadableReplyIsAskedForAgain` — recovery on the second ask
with the complaint and the do-not-rewrite instruction present, a readable reply
never asked twice, two failures spending the attempt, and one case each for
blocked, empty, and partial.

**Risk.** Low-moderate. Worst case doubles the build calls in an attempt, which
is the trade for not spending the attempt itself.

---

### 1.9 A path line that arrived as a markdown heading — **done** (`4cd209c`)

**Found in run 5.** TT-003 spent thirteen replies across four cycles emitting

```
#### src/game.rs
```rust
use crate::board::Board;
```

above a correct implementation, and was told each time that its response
contained a fenced code block with no file path before it. The 1.8 reprompt did
not help: the model does not experience `#### src/game.rs` as a missing path, so
being told the path is missing gives it nothing to change.

**Landed as.** The path line in `_BLOCK` now tolerates a leading markdown
heading and bold markers, beside the backticks and `File:` labels it already
took. Decorations around the path are the harness's to absorb; the executor
getting them exactly right was never the part that mattered.

Replayed over every build response recorded to that point: **13 previously
unreadable replies parse, 16 parse identically, 0 gain a phantom edit.** All 13
are TT-003's.

**Risk.** This widens the rule enough that a heading naming a file — `##
README.md` in a document — could be read as a path. That only arises in prose
being rescanned after a fence closed early, and 1.0 withholds such text rather
than applying it. A test asserts `### Using build.ps1 (Windows)` is still not a
path.

**Outcome.** TT-003 passed on its next attempt, after nineteen. TT-004 followed
in one. The backlog went green: lint and typecheck clean, 36 tests passing
across four files, every file the plan asked for on disk.

---

### 1.1 The provenance marker leaks into the criterion key — **done** (`88ed838`)

**Problem.** `_criteria_provenance_block` (`forge/prompts.py:142-162`) renders
each criterion as two lines:

```
- <criterion>
  _(from the plan — you may not change this)_
```

A planner that copies the criterion back verbatim — which is what the prompt
asks for — copies the marker with it. `_key` (`forge/respec.py:95-101`) strips
only non-alphanumeric characters, so `fromtheplanyoumaynotchangethis` survives
into the key. The copy no longer matches the original, and `_merge_criteria`
scores the same criterion as both dropped and minted.

**Evidence.** Run 1, event 76 `data.minted[2]`:

```
'piece::cells(kind, rotation)` returns 4 distinct offsets ... `0..4\n  _(from the plan — you may not change this)_'
```

All nine of TT-001's plan criteria appear in `minted` carrying the marker, and
the same nine appear in `restored` without it. Reported as "9 dropped, 11
minted". Actual: nine kept verbatim, two added. The inline variant appears in
event 37, so the newline is not the only carrier.

Across the run: 30 reported drops, 0 actual drops, 2 actual rewords — both
tightening a criterion toward the spec, not loosening it.

**Reproduced in run 2, on a different reviewer.** Event 37 reports 13 criteria
"put back"; event 38 reports 13 "the plan does not state" — and all 13 of the
latter are the former carrying the marker. A clean 13/13. The ticket was TT-003,
which had **zero attempts**, so the entire episode is the harness arguing with
itself about a ticket that never ran. Confirms the mechanism is
model-independent.

**Fix, two parts.**

1. Stop inviting the echo. Replace the per-line marker with two headed groups —
   *"Criteria from the plan (you may not change these)"* and *"Criteria added by
   an earlier revision (you may revise or retire these)"* — so a verbatim copy
   carries no marker to begin with.
2. Defend anyway. Strip a trailing provenance marker in `_key`, both the
   `_(...)_` and bare `(...)` forms, before normalising. `forge/respec.py:313`
   already states the principle: *"Instruction-following is not an access
   control."* Part 2 is the load-bearing half; part 1 removes the temptation.

**Files.** `forge/prompts.py:142-162`, `forge/respec.py:95-101`.

**Tests.** `tests/test_forge.py`, beside
`test_thirteen_criteria_reworded_stay_thirteen` (line 3132):

- `test_a_criterion_returned_with_its_provenance_marker_is_not_counted_as_new`
- `test_the_inline_marker_form_is_stripped_too`

**Risk.** Low. The stripping is narrow and anchored to text the harness itself
writes.

---

### 1.2 A genuinely empty response should be reviewed, not failed — **done**

**Revised after run 2.** The original version of this item assumed
`"executor returned no file edits"` meant the executor had nothing to do. In run
2 that message was almost always a **parse failure** instead — see 1.0, modes B
and C. Fix 1.0 first; it removes most of the occurrences. What remains is the
narrower case below.

**Problem.** `forge/loop.py:1405-1409` treats an empty parse as a failed
attempt:

```python
if parsed.is_empty:
    return StepResult(ok=False, detail="executor returned no file edits")
```

Disk is never reverted between attempts, and the executor is shown current file
contents. When the previous attempt's work already satisfies the spec, writing
nothing is the correct answer, and the loop spends an attempt punishing it.

**Evidence.** Run 1, steps 61-62, 94, 100-101:

> "Looking at the files provided, I can see they already implement the spec
> correctly."

Run 2 shows no clean instance of this — every "no edits" there was 1.0. So the
case is real but rarer than first estimated, and the item drops in priority
accordingly.

**Fix.** Once 1.0 can tell a genuinely empty reply from an unparseable one,
route the genuinely empty case into VERIFY and REVIEW against disk state. The
machinery already exists and is already tested: when the diff is empty,
`forge/loop.py:1707-1708` collects disk contents into `state`, and
`forge/prompts.py:473-486` instructs the reviewer to judge the criteria against
those contents and to *"say so and ACCEPT: a ticket whose work was already done
is finished, not failed."* The empty-executor path short-circuits before
reaching any of it. Skip TESTS when no file changed; attempt capping still
bounds the no-op case.

**Landed as.** `describe_unparsed` now returns `""` when a reply carries no
file content at all, which is the signal that separates "nothing to write" from
"content the parser could not read". In `_attempt`, a complaint still fails the
attempt; an empty one sets `wrote_nothing`, which skips APPLY and forces
`test_path` empty so the existing `no_tests_because` path handles the message.
Everything downstream is untouched — VERIFY and REVIEW already knew what to do
with a ticket that changed nothing.

Authoring a test on an attempt that wrote no files is exactly the orphan the
fixed-path rule exists to prevent, so that is skipped rather than merely
unused. Tests the ticket wrote on an earlier attempt stay on disk and still run.

**Tests.** `TestAnExecutorThatWritesNothing` — reviewed rather than failed, the
existing file left byte-identical, no tester call, and an unreadable reply still
failing with its specific complaint.

**Verification gap.** The recorded instances of this were run 1 steps 61-62, 94
and 100-101, and that `run.db` was replaced by the re-run. Classifying every
executor response in both surviving databases under the new logic gives 6
applied, 6 refused for a short fence, 8 refused as unreadable, and **0** taking
the new path. So this changes the outcome of nothing recorded — it is purely
additive protection, exercised by tests rather than by surviving data.

**Risk.** Low-moderate. It is the one change that can turn a previously failing
ticket green, so it depends on an accurate reviewer — satisfied as of Phase 0.

---

### 1.3 Never respec a ticket that never ran — **done** (`88ed838`)

**Problem.** `forge/loop.py:864-868` builds the respec set from `RETRYABLE`,
which is `(failed, blocked, skipped)` (`forge/state.py:497`). Requeueing a
skipped ticket is correct — it must run once its dependency lands. Respec'ing
one is not, and the two share a list.

`respec.revise` cannot decline either: `ticket_failures()` returns empty for a
ticket with zero attempts, but `gave_up_note` holds `"dependency not met:
TT-001"`, so `failures` is non-empty (`forge/respec.py:254-259`) and the
planner is called. It receives a section headed *"What happened, oldest attempt
first"* containing one line about an unmet dependency, and an instruction to
*"Revise the ticket so the next attempt can succeed."* It complies, because
that is the only output the schema allows.

**Evidence.** TT-002, TT-003 and TT-004 each have `attempts = 0` and each had
its human-authored spec rewritten twice.

- **TT-003 — fabricated generator.** `plan.md` deliberately specifies only "a
  xorshift32" and fixes no constants. Respec wrote into the spec:
  `state ^= (state << 13) ^ (state >> 7) ^ (state << 17);`. That is not
  xorshift32 — Marsaglia's form is three sequential assignments (13/17/5), not
  one combined XOR with 13/7/17. `forge/respec.py:288-290` names this exact
  failure mode as already observed.
- **TT-002 — broke the module contract.** Original: declare this ticket's module
  *"alongside the ones already there"*. Revised: *"`src/lib.rs` must contain
  exactly: `pub mod piece; pub mod board;`"*. TT-003 and TT-004 both add modules
  to that file, so the three tickets are now mutually unsatisfiable.
- **TT-004** — event 41: the ratchet fired on a ticket with zero attempts.

Criteria survived (`criteria == original_criteria` for all three), so the lock
held there. `spec` has no equivalent protection.

**Fix.** Split the requeue set from the respec set at `forge/loop.py:864`. A
ticket is eligible for respec only if it has recorded attempts. As a second
guard, have `revise` return `Revision(note="never ran")` when `ticket_failures`
is empty and the only evidence is a dependency-miss note.

**Files.** `forge/loop.py:864-868`, `forge/respec.py:254-259`.

**Tests.**

- `test_a_skipped_ticket_is_requeued_but_not_respecced`
- `test_a_dependency_miss_is_not_evidence_a_spec_is_wrong`

**Risk.** Low. Strictly removes work.

**Confirmed in run 2.** TT-003 and TT-004 both sat at `attempts = 0` and were
respec'd in both cycles (events 39, 41, 65, 66). TT-003 additionally triggered
the full 1.1 ratchet episode — 13 restored, 13 refused — on a ticket that had
never executed.

**Follow-up.** The run-1 fabrications were cleared by the fresh `run.db`. Run 2
has produced its own: TT-006's spec and context now carry the 1.0 misdiagnosis.
Restore both from `original_spec` / `original_context` before the next baseline
run, or reingest `plan.md`.

---

### 1.4 Phantom revisions must not satisfy the retry brake — **done** (`88ed838`)

**Problem.** `forge/loop.py:919` ends the retries when `not revised` — the
guard against handing the executor an unchanged ticket and hoping for a
different sample. Revisions to never-run tickets count toward `revised`.

**Evidence.** In cycles 1 and 2, TT-001/005/006 came back materially unchanged
every time, but TT-002/003/004 were "revised", so the brake never engaged. The
loop bought two extra cycles on the strength of rewriting tickets that had not
run. `_evidence_fingerprint` eventually stopped it — two cycles late, at event
109.

**Fix.** Count only tickets that actually failed when deciding whether the cycle
produced variation. Falls out of 1.3 if the eligible set is split before
`_respec` is called; assert it directly regardless.

**Files.** `forge/loop.py:912-940`.

**Tests.** `test_revising_a_never_run_ticket_does_not_buy_another_cycle`,
beside `test_a_respec_that_changed_nothing_ends_the_run` (line 729).

**Risk.** Low.

**Confirmed in run 2.** Cycles 1 and 2 again ended on `_evidence_fingerprint`
(event 89), never on `not revised`, while TT-003 and TT-004 — both at zero
attempts — supplied the revisions that kept the brake off.

---

### 1.5 A failed ticket's work is grandfathered into the baseline — **done**

**New in run 2.** Nothing reverts a failed ticket's edits, and `baselineVerify`
then reclassifies its breakage as pre-existing for every ticket that follows.

**Evidence.** TT-002 failed all three attempts, but `src/board.rs` and the
`pub mod board;` line it added to `src/lib.rs` stayed on disk. From step 27
onward every `baseline-lint` carries its clippy errors, and each subsequent
ticket is told:

> "TT-005: lint was already failing before this ticket started (7 error(s)); it
> will not be blamed for them." (events 19, 26, 47, 54)

TT-005 and TT-006 were therefore verified against a poisoned baseline. TT-005 is
`done`. As of now `cargo clippy -- -D warnings` fails on the repo with four
errors, all in `src/board.rs`, all owned by a ticket that failed.

Two things are wrong at once: the run reports tickets complete over a crate that
does not lint, and any *real* lint regression a later ticket introduces in those
same files would also be excused.

**Fix.** Keep the edits — respec and the "already on disk" logic both depend on
them — but stop granting them baseline amnesty. Track which files a failed
ticket touched, and exclude errors in those files from the pre-existing set for
later tickets in the same run. At minimum, name the owner in the log: *"N of
these errors were introduced by TT-002, which failed."*

---

**What was actually implemented, and why it differs.**

Two claims above did not survive checking, and the fix changed shape as a
result.

*"Any real lint regression a later ticket introduces in those same files would
also be excused"* — **false**. `introduced` is a set difference over
*signatures*, not over files, and a signature carries its own `--> path:line`.
A new error in an already-broken file produces a new signature and is still
attributed. Verified directly against `signatures()`.

*"Exclude errors in files a failed ticket touched from the pre-existing set for
later tickets"* — **would have been harmful**. TT-005 cannot fix
`src/board.rs`; it is not in its scope. Blaming it would spend three attempts on
work it has no authority to do, which is the exact chain `_baseline_failures`
was written to break — its own docstring says so.

The real defect is narrower and sharper: **a ticket was being excused breakage
it caused itself.** Nothing reverts a failed ticket, so a retry starts with its
own damage on disk, and `_baseline_failures` runs per ticket per cycle — so the
damage became that ticket's own baseline. Run 2, event 73: *"TT-002: lint still
failing, but only on errors that pre-date this ticket; not counted against it."*
The four errors were in `src/board.rs`. TT-002 wrote them.

So the rule implemented is neither of the above: **amnesty covers only what the
ticket cannot fix.** A diagnostic pointing at a file in the ticket's
`allowed_files` is never excused — it is in scope, the ticket is the only party
that can fix it, and on a retry it is usually the ticket's own. Everything
outside that scope is excused exactly as before.

This needs no attempt counters and no tracking of who broke what, because
ownership is a property of scope rather than of history. It also closes the
cross-run case for free: breakage left on disk by a failed run is owned by
whichever ticket's scope covers it, whenever that ticket next runs.

Replayed against the baseline TT-002 was actually handed on its final cycle:

| ticket | scope | not excused | excused |
|---|---|---|---|
| TT-002 | `src/board.rs`, `src/lib.rs` | 4 | 0 |
| TT-005 | `web/*` | 0 | 4 |
| TT-006 | `build.sh`, `build.ps1`, `README.md` | 0 | 4 |

**Landed as.** `Orchestrator._signature_scope`, and the owned/inherited split in
`_baseline_failures` (`forge/loop.py`). A signature with no parseable location
stays excusable — the safe direction.

**Tests.** `TestTheBaselineExcuseStopsAtTheTicketsScope` — six cases covering
in-scope, out-of-scope, whole-scope, glob scope, case folding, and the
no-location fallback.

**Risk.** Moderate, as predicted, but narrower than feared. A ticket that
*inherits* breakage in a file it owns now has to fix it. That is correct — it
has the authority and nobody else does — but it will look like a regression on
the first run after it lands.

**Still open.** `_finish` runs its final verify only when nothing is blocked
(`forge/loop.py:753-763`), so a blocked run never reports the state of the tree.
Lower value now that ownership is attributed per ticket, but it is the last
place a dirty tree can leave without comment.

---

### 1.6 Configuration note — **done**

`alllocal`'s lint command is `cargo clippy -- -D warnings`, which makes style
lints fatal. TT-002 lost its three attempts to three *different* clippy lints in
sequence: `E0308` (step 18), `manual_range_contains` (step 26), then
`unnecessary_cast` (step 71). Each fix surfaced the next lint.

That is a treadmill a local executor will rarely walk off, and it is a plan
configuration choice rather than a harness defect.

Narrowed in `alllocal/.hybridforge/config.json` to:

```
cargo clippy --all-targets -- -D clippy::correctness -D clippy::suspicious
```

Measured on the tree as it stands: 6 errors and exit 101 under `-D warnings`, 0
errors and exit 0 under the narrowed set. The style lints that were failing
TT-002 — `manual_range_contains`, `unnecessary_cast` — are warnings again, while
the bug-shaped groups stay fatal. Real compile errors fail regardless of lint
level, and `typecheck` runs `cargo check --all-targets` besides.

---

## Phase 2 — Reviewer discipline

Model choice fixed the acute problem. These make the harness resilient to the
next weak reviewer rather than dependent on a strong one.

### 2.1 Require cited evidence for every objection — **done**

**Problem.** `REVIEWER_SYSTEM` (`forge/prompts.py:84-99`) lets the reviewer
assert an absence without showing what it looked at.

**Evidence.** Every objection in the last two cycles was factually false:

| Reviewer's claim | Reality |
|---|---|
| canvas "does not specify a width of 240 and a height of 480" (3×) | `web/index.html:31` — `<canvas id="board" width="240" height="480">` |
| `score`/`lines`/`level` elements absent | `web/index.html:32-34` — all three present |
| Space "mapped to `' '`, should be `4`" | `web/main.js:86` — `game_input(4)` |
| loop "continues to advance after `game_over()` returns 1" | `web/main.js:32` — `if (game_over())` guard |
| score/lines/level "only updated once at the beginning" | `web/main.js:60-62`, inside the frame callback |
| `build.sh` "does not include the shebang line and `set -eu`" | `build.sh:1-2` — both, first two lines |

**Fix.** Amend `REVIEWER_SYSTEM`: each objection must quote the line it is about,
or name the identifier it searched for and did not find. An objection without
either is not a finding.

**Landed as.** An added clause in `REVIEWER_SYSTEM` requiring a quoted line
per objection, or the exact text searched for when the objection is that
something is missing, plus the canvas example as the reason it is not a
formality. A closing instruction not to restate the sections it was given,
which is 2.2's problem addressed at the source.

**Risk.** Low. Prompt-only, and it cannot make a correct reviewer worse.

**Not enforced.** The citation is asked for, not checked. Mechanical
verification is 2.4, still conditional on whether the new reviewer needs it.

---

### 2.2 Strip harness scaffolding from stored verdicts — **done**

**Problem.** The reviewer echoed the prompt's own headings back as part of its
verdict. That text is stored verbatim (`forge/loop.py:1775-1776`) and fed back
as `prior_verdicts` next attempt (`forge/loop.py:1741`), where it is echoed
again. The block nests on itself each round.

**Evidence.** Steps 17 and 24 contain `## You have already rejected this ticket
/ ### Attempt 1 / … Read these before deciding …` inside the stored verdict.

**Landed as.** `strip_prompt_echo` in `forge/prompts.py`, applied where the
verdict joins `rejections` in `forge/loop.py`. The headings live beside the
prompt that writes them so the two cannot drift.

Matching is split by how much of a line has to match. The long headings are
sentences no reviewer writes by accident, so a prefix is enough. `## Spec`,
`## Diff` and `## Acceptance criteria` are ordinary markdown a reviewer might
quote out of a README it is reviewing — especially now that 2.1 asks it to quote
things — so those count only when the line is nothing else.

Only the copy used as a prompt is trimmed. The raw completion stays whole in
`steps.detail` and in the detail handed to the executor.

**Tests.** `test_a_verdict_that_echoes_the_prompt_is_not_fed_back` end to end,
plus `TestStrippingThePromptEcho` for the clean verdict, idempotence, a quoted
heading inside a citation, a wholesale prompt copy, and the empty string.

**Verification gap.** The evidence for this was run 1 steps 17 and 24, and that
`run.db` was replaced by the run 2 re-run. Scanning every verdict in both
surviving databases finds 4 recorded and 0 carrying echo — the new reviewer does
not do it. So the fix is tested against a reconstruction of the observed shape,
not against surviving data.

**Risk.** Low.

---

### 2.3 Cap the prior-verdict block and make it droppable — **done**

Both history blocks now travel as their own user message, because the gate
drops whole messages and these were inside the ticket body. `review_prompt`
emits `PRIOR_VERDICTS_HEADING` and `build_prompt` emits
`PRIOR_FAILURES_HEADING`, both after the context message and before the ticket,
so the drop order is memory, then history, and never the spec. `_PRIOR_VERDICTS
= 3` caps what the reviewer is shown, which is what keeps trimming a last
resort rather than a routine.

**Problem.** `rejections` is uncapped (`forge/loop.py:1130`, appended at
`1775-1776`) while `prior_failures` is capped at two (`_PRIOR_FAILURES`,
`forge/loop.py:1221`). Neither is droppable: `forge/loop.py:356` marks only the
retrieved-memory block. On overflow the gate raises `ContextOverflow`, which
`forge/loop.py:1377-1379` converts to `blocked=True`.

So a ticket that accumulates enough rejection text gets hard-blocked rather than
trimmed. Not reached at `maxAttempts: 3`; reachable the moment that is raised,
and more reachable on a smaller single-model context window.

**Fix.** Add `_PRIOR_VERDICTS` mirroring `_PRIOR_FAILURES`. Mark the prior-verdict
and prior-failure sections droppable so the budget gate trims before it blocks.

**Files.** `forge/loop.py:1130`, `:1221`, `:356`; `forge/prompts.py:506-521`.

**Tests.** `test_a_long_rejection_history_is_trimmed_rather_than_blocking`.

**Risk.** Low-moderate. Touches the budget gate's droppable predicate; verify the
spec and criteria remain non-droppable.

---

### 2.4 Optional — verify citations mechanically

Once 2.1 is in, a quoted citation can be checked against the diff and the
substituted file contents the reviewer was actually shown. A quote that appears
nowhere in its input is a provable fabrication.

Do **not** flip such a verdict to ACCEPT — `parse_verdict`
(`forge/prompts.py:527-541`) is deliberately fail-closed and should stay that
way. Instead mark the review inconclusive, retry once with the fabrication
named, and record it in `blocked_note` so respec and the human see it.

Gate behind a config flag. Land only if Phase 0 shows the new reviewer still
fabricates.

---

## Phase 3 — Close the gap between the reviewer's bar and the planner's

### 3.1 Allow criteria the spec already states — **done**

`_spec_entailed` compares a proposed criterion's content words against each
sentence of `original_spec` — the ingested one, so the loop cannot rewrite the
spec and then mint criteria out of what it just wrote. A criterion is admitted
when one sentence covers 80% of its content words and it has at least five of
them; below that floor, overlap is coincidence. Admissions are logged at `info`
with the criteria named, so the heuristic is auditable rather than merely
plausible, and `forge retry --respec` prints them.

The refusal message no longer claims the plan is silent. It now says the
criterion appears neither in the criteria nor in the spec — the two places that
are enforced — and points at re-ingesting the plan.

**The promotion path.** `forge criteria` lists what respec proposed and the
loop refused, read back out of the run log — the refusal is already an event,
and a second store of the same fact is a second thing to keep true.
`forge criteria TT-006 --accept 1` adopts one: it lands on the ticket *and* on
`original_criteria`, so it is plan-authored from that moment and the ratchet
protects it from the next revision exactly as if a human had written it in the
plan. The ticket file is rewritten so the artifact a human reads does not lie,
and the adoption is logged.

The anchor stays unwritable everywhere else. `Store.promote_criteria` is its
only writer after ingest, it is reachable from the CLI alone, and
`update_ticket` still cannot touch the anchor at all — which is the property
that made the refusal worth enforcing in the first place. A ticket that has
already passed is not requeued behind anyone's back; the command prints the
`forge retry --ticket` line and leaves the call to a human.

**Problem.** `REVIEWER_SYSTEM:98` instructs the reviewer to reject when *"a
criterion is unmet **or the diff does something the spec did not ask for**"*, and
`review_prompt` hands it the full spec. So the enforced bar is spec ∪ criteria.
Meanwhile `_merge_criteria` (`forge/respec.py:162-229`) tests novelty against
`ticket.criteria` alone and never reads `ticket.spec`, so the planner may not
write down any part of the bar the reviewer is actually applying.

That gap is not closeable from inside the loop, which is what the six respec
cycles were trying and failing to do.

**Evidence.** Every genuinely-new criterion in the run is a verbatim spec
sentence:

- TT-001: `Cargo.toml … crate-type = ["cdylib", "rlib"] … opt-level = "z", lto = true` — stated in the spec, refused
- TT-001: `src/lib.rs contains pub mod piece;` — stated in the spec, refused
- TT-005: canvas `width 240 height 480`; `ArrowLeft..Space → game_input(0..4)`; `game_over()==1 → stop + draw GAME OVER`; `update score/lines/level each frame` — all four stated in the spec, refused
- TT-006: `build.sh` begins `#!/usr/bin/env sh` then `set -eu` — stated in the spec, refused

Two of roughly fifteen were real inventions: an element id `hint`, and naming
CORS as the reason `file://` fails — and the reviewer had already rejected for
exactly that, so the planner was transcribing the reviewer's bar, not raising
its own.

**Run 2 demonstrates the gap end to end on a single criterion.** The planner
proposed *"build.sh must start with #!/usr/bin/env sh and set -eu"* and was
refused, twice (events 42, 67). The reviewer rejected TT-006 for that exact
requirement, twice (steps 47, 64). One party is required to enforce it; the
other is forbidden from writing it down; the plan states it in the spec. Three
cycles could not close a one-line gap.

**Fix.** A criterion that restates a spec sentence is not a ratchet: the spec is
already enforced, because the reviewer is given it. Admit minted criteria that
are entailed by `ticket.spec`, using a conservative token-overlap match against
spec sentences, and log when the allowance fires so the heuristic stays
auditable.

Correct the refusal message either way. It currently reads *"if these are things
it genuinely must do, the plan is what needs changing"* — false when the plan
does state them, in the spec.

For criteria that are *not* spec-entailed, keep the refusal and add a promotion
path: surface them so a human can accept one with a single command rather than
editing `plan.md` by hand.

**Files.** `forge/respec.py:162-229`, `:353-367`.

**Tests.**

- `test_a_criterion_restating_the_spec_is_not_treated_as_a_new_demand`
- `test_a_criterion_absent_from_the_spec_is_still_refused`

**Risk.** Moderate. This deliberately loosens a guard that exists for a reason.
The entailment test must be conservative — a false positive lets the loop raise
its own bar, which is the regression `_merge_criteria` was written to stop. Land
after Phase 1 and only with the audit log.

---

### 3.2 Protect plan-authored context — **done**

`original_context` is recorded at ingest beside `original_spec` and
`original_criteria`, and is absent from `update_ticket` for the same reason they
are: an anchor any caller can move is not one. A revised `context` is appended
to the plan's paragraph rather than written over it, skipped when the revision
already contains it, and the restoration is logged and printed by `forge retry
--respec`. `_refuse_protocol_edits` now anchors `context` on the original too,
so a formatting phrase the plan never used is still judged as introduced after
a revision has rewritten what it is compared against. A context-only change now
counts as drift, which is what puts the ingested text in front of the planner.

Ticket text that predates the column has no anchor, and is left to the planner
exactly as before — the guard protects a paragraph a human wrote, and reports no
paragraph rather than inventing one.

**Problem.** `context` is a full replacement with no equivalent of
`original_criteria`. Respec used it as a rationale scratchpad and deleted the
executor's output protocol.

**Evidence.** TT-001's context after cycle 1, in full:

> "The executor consistently omits scaffold files when they are not explicitly
> verified by acceptance criteria. Verifying them in the criteria list will force
> their inclusion in subsequent attempts."

The plan's original context — the bare-path-line rule and the do-not-write-tests
rule — is gone from TT-001 through TT-005. Only TT-006 kept it, because its
failure was about fencing. The system prompt still carries the rule
(`forge/prompts.py:41-47`), so this is degradation rather than deletion, but the
redundancy that was holding a weak local model to format is gone.

**Fix.** Record `original_context` at ingest alongside `original_spec` and
`original_criteria`. Respec appends to context; the plan's paragraph is
preserved. Rationale belongs in the `rationale` field, which already exists and
is already logged.

**Files.** `forge/state.py` (schema + migration), `forge/ingest.py`,
`forge/respec.py`, `forge/prompts.py`.

**Tests.** `test_respec_cannot_delete_the_plans_context`.

**Risk.** Moderate — schema migration. Follow whatever pattern `original_spec`
used.

**Confirmed in run 2.** TT-006's context was replaced again, this time with the
1.0 misdiagnosis: *"The executor must output raw file contents for exactly three
files, not markdown prose describing them."* The plan's path-protocol paragraph
is gone, and what replaced it is false.

---

### 3.3 Carry rejection history across cycles — **done**

`_work_ticket` seeds both lists from the step log when `attempt_base > 0`:
failures through `ticket_failures`, verdicts through a new
`Store.ticket_rejections`, which reads failed `review` steps whole rather than
distilled — a verdict is prose the reviewer wrote for its own successor, and
`distill` is built for compiler output. Seeded verdicts go through
`strip_prompt_echo` on the way in, for the same reason the live list does. The
2.3 caps apply to both, so a fourth cycle seeds three verdicts rather than
twelve.

Seeding is conditional on `attempt_base` because the step log is per ticket,
not per cycle: unconditional, the first cycle would show a ticket its own
current attempts back to itself.

**Problem.** `history` and `rejections` are locals in `_run_ticket`
(`forge/loop.py:1129-1130`). A retry cycle calls `_run_ticket` fresh, so both
start empty. Only `blocked_note` survives — the last failure, distilled to 1500
characters (`forge/loop.py:1189-1192`) — and it goes to respec, not to the
executor or the reviewer.

**Evidence.** Cycle 2's TT-005 reviewer had no knowledge of the three rejections
cycle 1 had already issued. It re-raised the same objections from scratch, and
the *"a rejection that repeats is evidence the spec is wrong"* nudge in
`review_prompt` never fired across cycles because `prior_verdicts` was empty. The
one channel designed to notice a third identical objection resets exactly when
it matters most.

**Fix.** Seed `history` and `rejections` from the step log at the start of
`_run_ticket` when `attempt_base > 0`. Both are already durable —
`store.ticket_failures` reads failed steps, and review verdicts are stored at
`forge/loop.py:1403`. Apply the 2.3 caps to the seeded lists.

**Files.** `forge/loop.py:1129-1130`, `forge/state.py:460-490`.

**Tests.** `test_a_second_cycle_reviewer_sees_the_first_cycles_rejections`.

**Risk.** Moderate. Interacts with 2.3 — seeding without caps makes overflow
more likely, so land 2.3 first.

---

### 3.4 Minor — ingest mangles criteria backticks — **done** (`88ed838`)

`forge/ingest.py:111` applies `.strip("`")` to the whole criterion line. On a
criterion that both opens and closes with inline code, that removes the opening
backtick of the first span and the closing backtick of the last, leaving
unbalanced markdown in every prompt that renders it:

```
piece::cells(kind, rotation)` returns 4 distinct offsets for every `kind` in `0..7` and every `rotation` in `0..4
```

Harmless to `_key`, which strips all non-alphanumerics. Not harmless in
practice: it is part of why the planner "rewords" criteria — it is repairing the
backticks — which feeds the 1.1 false positives.

**Fix.** Strip a wrapping pair only when the whole line is one code span.

**Test.** `test_a_criterion_wrapped_in_two_code_spans_keeps_both`.

---

## Phase 4 — Conversational executor, behind a flag

An experiment, not a fix. Run it only against a green Phase 1–3 baseline.

**Built — `loop.executorTurns`, default 0.** `Store.ticket_turns` rebuilds the
exchange from `steps` on every call: each `build` step's reply paired with the
step that failed next. A reply with no failure after it is dropped rather than
paired with the next one along — an attempt can end without a failed step, and
attaching that reply to a later failure would tell the executor its code caused
something it never reached. `build_prompt` writes the ticket once, then each
answer as an `assistant` turn with its failure as the reply to it, and the
newest failure as the last word. Old exchanges are droppable, the newest failure
is not, and the flat `prior_failures` block is suppressed when turns are present
— it is the same failures, each now attached to the answer that caused it.

Executor only. Planner, tester and reviewer keep single-turn prompts; a reviewer
inheriting the executor's turns stops being an independent check.

**Not measured.** The comparison the section asks for — same backlog, flag on
and off, watching for anchoring — has not been run.

**Premise.** The daemon-owned state machine is right and does not change. What
is separable is *prompt shape*: the daemon can reconstruct a multi-turn message
list from SQLite on every call. Stateless transport, conversational shape, no
loss of durability.

**The gap it addresses.** The executor never sees its own previous output.
`build_prompt` supplies the spec, the files as they exist on disk,
`failure_context`, and `prior_failures` — all of which are *failure details*
(`history.append(f"Attempt {n}: {outcome.detail}")`, `forge/loop.py:1174`). The
model's own completion text is never fed back.

So on attempt 2 the executor reads files it wrote with no representation that it
wrote them, which is exactly the state that produced *"Looking at the files
provided, I can see they already implement the spec correctly."* As
`user`(task) / `assistant`(files it emitted) / `user`(rejected, here is why),
that confusion is structurally impossible, and it matches what instruct models
are post-trained on.

Secondary benefit, now dominant with one model loaded: append-only turns give a
stable KV prefix. The current design mutates the single user message every
attempt, forcing a full re-prefill.

**Constraints.**

- **Per-role, per-ticket.** Four separate threads. A reviewer that inherits the
  executor's turns stops being an independent check — the same principle that
  stops the executor writing its own tests, and that `_merge_criteria` enforces
  one level up.
- **Executor only, to start.** Fresh judgment is worth more than continuity for
  the reviewer, and the prior-verdict channel is where 2.2 lives.
- **Capped and droppable**, per 2.3.

**Known risk.** Anchoring — a model shown its own wrong answer as an assistant
turn defends it more readily. The current design already suffers this through
disk state, so the trade is not clean in either direction. Measure it; do not
assume it.

**Scope.** Config flag, default off. Reconstruct up to N prior attempt turns for
the current ticket from `steps`, drop oldest-first under budget pressure, leave
planner/tester/reviewer single-turn. Compare against the same backlog.

**Files.** `forge/prompts.py` (`build_prompt`), `forge/loop.py:1328-1376`
(`_attempt`), `forge/config.py`.

---

---

### 3.5 A design decision in spec prose has no protection — **done**

Landed with 3.2. `ingest.plan_decisions` reads the sentences a plan marked as
settled out of `original_spec` — everything under a heading about decisions
("Design decisions, already made", "do not revisit", "non-negotiable"), plus any
single line that marks itself (`**Decision:** ...`) for a spec with no room for
a section. A revised spec that no longer states one of them is refused whole and
the dropped sentence named, in the run log and in `forge retry --respec`.

Three deliberate limits. It protects what the plan *labelled*, not prose in
general: an unmarked sentence stays freely revisable, because a guard over all
prose would refuse every genuine clarification. Matching is on a normalised form
— punctuation and backticks may change, the words may not — which is the bar the
prompt now asks for and the one a human can check by reading. And a decision
whose normalised text is under 24 characters is skipped, because containment
proves nothing at that length.

The whole spec revision is refused rather than the sentence stitched back in: a
spec revised around a dropped decision has already reasoned from its absence,
and restoring the sentence into that reasoning produces a spec contradicting
itself.

The original write-up follows.

**Found in run 5, in a backlog that passed.** `plan.md` opens with a section
headed *"Design decisions, already made — implement them, do not revisit them"*,
and one of them is:

> **Randomness** is a xorshift32 seeded from JavaScript

TT-003's spec now says "an internal deterministic PRNG". Respec's rationale:

> "The spec incorrectly pinned the PRNG algorithm to xorshift32 while tests only
> require determinism"

What shipped is a Numerical Recipes LCG:

```rust
self.state = self.state.wrapping_mul(1664525).wrapping_add(1013904223);
```

It is deterministic, it satisfies every criterion, and the reviewer accepted it
correctly, because no criterion names xorshift. The ticket is green and the
decision a human wrote down is gone.

This is the criteria ratchet's blind spot, exactly inverted from 3.1. The
ratchet protects criteria from being weakened; nothing protects a design
decision stated in prose. Respec observed that the criteria are the real
contract and revised the spec to match them — locally reasonable, globally
wrong, and invisible because everything downstream agreed.

Related to 1.7 (respec editing what was not its to edit) and to 3.2 (protecting
plan-authored context). 3.2 should be widened from context to spec prose: keep
`original_spec` as the anchor it already is, and refuse a revision that drops a
sentence the plan marked as a decision rather than a requirement.

Worth noting the ordering problem this exposes. Every guard so far assumed the
criteria are the contract and the spec explains it. A plan can also put load-
bearing decisions in the spec, and the harness has no way to tell those from
commentary.

---

### 3.6 A ticket can be verified by reading rather than by running — **done**

`_tests_authored` and `_tests_skipped` hold ticket ids rather than counts, and
`_report_unexecuted` names at run end every ticket that reached `done` while
skipping test authoring and never authoring any. A ticket that skipped on the
attempt that wrote nothing and authored on the one that did is not named — what
matters is whether it ended covered. Neither is a ticket that failed: the claim
is about what a green ticket proved.

No new dependency, no browser driving, and nothing new is tested. The run says
what its green did not cover, which is the thing that would have pointed at the
two files worth opening by hand.

The original write-up follows.

**Found by opening the finished game in a browser.** The backlog was green — all
six tickets `done`, lint and typecheck clean, 36 tests passing — and the page
loaded to an empty board that never started.

`web/main.js:13`:

```js
const instance = await WebAssembly.instantiateStreaming(fetch('./tetris.wasm'), {});
const { game_new, ... } = instance.exports;   // TypeError
```

`instantiateStreaming` resolves to `{ module, instance }`, not the instance, so
`instance.exports` is `undefined` and the next line throws. `run()` was called
with no `.catch()`, so it became an unhandled rejection: no error on the page, no
output, an empty canvas. The Rust was correct throughout — driven directly, the
module spawns, applies gravity, hard-drops and locks exactly as specified.

**Nothing in the pipeline could have caught it.** TT-005's acceptance criteria,
in full:

- `web/index.html` contains a canvas element with id `board`
- `web/index.html` contains elements with ids `score`, `lines` and `level`
- `web/main.js` calls `WebAssembly.instantiateStreaming`
- `web/main.js` reads the render buffer through a `Uint8Array` over `memory.buffer`
- `web/main.js` maps all five arrow and space keys to `game_input`

Every one is a token-presence check, and every one is satisfied by code that
throws on the second line of its own entry point. It *calls*
`instantiateStreaming`. It *reads* a `Uint8Array` over `memory.buffer`. Both
statements are true of code that never runs.

The reviewer passed it, correctly, against the contract it was given. And no
other check applied: the ticket authored no tests — *"TT-005: no tests authored
— the ticket wrote no .rs file, and this project's test command collects .rs
tests. Review will check the criteria instead."* — and verification is `cargo
clippy`, `cargo check`, `cargo test`, none of which executes a line of
JavaScript. The 36 passing tests cover pieces, board, rules and the wasm exports
*from the host*. The JS-to-wasm boundary is the one seam no ticket owns, and it
is the only place the bug could hide.

**Two layers.** The plan wrote acceptance criteria for a browser shell in terms
a text search can satisfy; `piece::cells(1, 0)` returning four offsets is
checkable, "main.js calls instantiateStreaming" is grep. And the harness lets
that pass unremarked: when a ticket authors no tests the loop falls back to
"review will check the criteria", and when the criteria are textual that
fallback is a text check too.

`_report_test_coverage` already warns when *no* ticket in a run authored tests.
Here four did, so it stayed quiet about the two that did not.

**Not a proposal to test JavaScript.** The harness verifies with the project's
own commands, and that is right — teaching it to drive a browser would be a
different tool. What is missing is honesty about what a green ticket means. At
run end, name the tickets that were never executed:

> TT-005 and TT-006 passed on review alone. Neither authored tests and neither
> is covered by the test command, so their criteria were checked by reading the
> diff, not by running anything.

That costs nothing, needs no new dependency, and would have pointed straight at
the two tickets worth opening by hand.

**Related to 3.5**, and the same shape: the run is green, and the green means
less than it looks. 3.5 is a decision quietly dropped; this is a whole ticket
quietly unexecuted. Both are cases of the harness reporting more confidence than
it earned.

## Suggested landing order

Revised after run 2. 1.0 moves to the front — it destroys files, and it is
currently manufacturing the evidence that everything downstream reasons from.

| Order | Items | Why here |
|---|---|---|
All completed work lives on `fix/loop-repair`, which is where the rest should
land too — one branch to push when the backlog is green.

| Order | Items | Why here |
|---|---|---|
| — | 0.1 | Done. Reviewer confirmed good; two new failure modes exposed |
| — | 1.0 | Done — `308fb6a` |
| — | 1.1, 1.3, 1.4, 3.4 | Done — `88ed838` |
| — | 1.5 | Done — amnesty now stops at the ticket's own scope |
| — | 2.1, 2.2 | Done — citation required, prompt echo stripped |
| — | 1.2 | Done — empty reply reviewed against disk |
| — | 1.7 | Done — respec cannot describe the reply format |
| — | 1.8 | Done — unreadable reply reprompted once |
| — | 1.9 | Done — heading-decorated path line read |
| — | 3.2, 3.5 | Done — context anchored, marked decisions protected |
| — | 2.3 | Done — history is its own droppable message, capped at three |
| — | 3.3 | Done — both lists seeded from the step log on a retry cycle |
| — | 3.6 | Done — a review-only ticket is named at run end |
| — | 3.1 | Done — spec-entailed criteria admitted, `forge criteria` adopts the rest |
| — | 4.1 | Built behind `loop.executorTurns`; unmeasured |
| 1 | 2.4 | Conditional on the reviewer still fabricating |

1.6 is a decision, not a code change, and should be settled before the next
baseline run.

Groups 1 and 2 are independent and can go in one branch each. Everything from 6
onward wants the `alllocal` backlog green first, so a regression is
attributable.

**Before re-running `alllocal`:** restore TT-006's spec and context (1.3
follow-up), and delete `src/board.rs` plus its `pub mod board;` line, or the
1.5 baseline is already poisoned on the next start.

---

## Recovering a whole file that forgot its name

The reprompt (1.8) assumes the model misunderstood the format. Replayed against
a later run, that assumption is often wrong. `alllocal2` BUG-002 lost three of
five attempts to unparsed replies, and the reprompt was answered **in the same
shape both times** — because formatting was not what the model got wrong.

The shape, from the artifacts:

```
Looking at the problem, I need to fix the `piece::color` function so that:
...900 words of reasoning about a genuine contradiction...
```rust
pub fn color(kind: usize) -> u8 { ... }      <- the current code, QUOTED
```
...more reasoning...
```rust
pub const WIDTH: usize = 10;                  <- the whole rewritten file
...
```
```

A correct file, complete, with one line missing above it. Discarded, attempt
spent, and the ticket eventually reported "gave up after 5 attempts".

The original sketch — *one file authorised, one fenced block, infer the path* —
would not have worked here, because these replies hold **two** blocks and the
first is a fragment. Applying the wrong one writes a fragment over a whole file:
a successful apply, no rejected paths, nothing in the log connecting the two.
That is worse than discarding the reply, and it is the outcome the guards exist
to rule out.

**`infer_single_file` (`forge/patch.py`).** Called only when the ticket
authorises exactly one writable path, so the destination is never guessed. Then:

- Take the **largest** block, so a quoted fragment loses to the file containing
  it.
- Require it to still hold **80% of the top-level lines already on disk**
  (`_REWRITE_COVERAGE`). Measured against the real replies: the quoted function
  scores 17%, the whole file scores 100%. Nothing lands near the threshold.
- **Never** recover into a file that does not exist. With nothing to compare
  against, ```` ```python\nx = 1\n``` ```` would become the entire contents of a
  new module. A reply meant to be a file that arrived unreadable stays a failure.

Recovery runs *after* the reprompt, so a model that can be corrected still is,
and is logged at `warn` when it fires — the harness has just written a file the
model never addressed by name, and that should be visible rather than inferred
from a diff.

**Replayed over every build reply in `alllocal2` run 3:**

| reply | before | now |
|---|---|---|
| attempt-2/01 | discarded | recovered (1422 chars, whole file) |
| attempt-3/02 | discarded | recovered (1422 chars, whole file) |
| attempt-3/01 | discarded | **still refused** — fragments only |

The third is the one that matters. Its largest block is the `color` function
alone; writing it would have deleted `CELLS`, `cells` and `WIDTH`.

**Tests.** `TestAWholeFileWithNoPathLineIsStillTheFile` — eight, including the
fragment-beside-the-file case and the two-file ticket that is left alone.
