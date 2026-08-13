# Loop repair plan

Derived from the `alllocal` run of 2026-08-12 (`.hybridforge/run.db`, run 1):
six tickets, three cycles, 27 executor attempts, 12 planner calls, zero tickets
completed. Every file anchor below is against `hybrid-forge` at `961b06b`.

The run failed for four independent reasons that compounded. Ordered here by
what unblocks what, not by severity.

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
4. **Not done, still open:** when a ticket authorises exactly one file and the
   reply is a single fenced block with no path line, infer the path. Safe for a
   one-file ticket; do not attempt it for TT-002's two. Left out because mode D
   turned out to be the commoner shape and a clear message may be enough.

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

**Open question for review.** When a response mixes good and truncated blocks
(steps 57, 79), the whole attempt is refused rather than applying the files that
parsed cleanly. That matches the `duplicate_paths` convention and avoids leaving
a ticket half-done, but partial application is defensible and was not tried.

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

### 1.2 A genuinely empty response should be reviewed, not failed

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

**Files.** `forge/loop.py:1405-1409`.

**Tests.**

- `test_an_executor_that_writes_nothing_is_reviewed_against_disk`
- `test_a_ticket_already_satisfied_on_disk_passes_without_an_edit`

**Risk.** Low-moderate. This is the one change that can turn a previously
failing ticket green, so it depends on an accurate reviewer — satisfied as of
Phase 0 — and on 1.0 landing first.

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

### 1.6 Configuration note — not a code change

`alllocal`'s lint command is `cargo clippy -- -D warnings`, which makes style
lints fatal. TT-002 lost its three attempts to three *different* clippy lints in
sequence: `E0308` (step 18), `manual_range_contains` (step 26), then
`unnecessary_cast` (step 71). Each fix surfaced the next lint.

That is a treadmill a local executor will rarely walk off, and it is a plan
configuration choice rather than a harness defect. Consider narrowing to
correctness lints (`-D clippy::correctness`) and leaving style lints as
warnings, at least while the loop is being debugged. Worth deciding before the
next baseline run, because it changes what TT-002 is actually being asked to do.

---

## Phase 2 — Reviewer discipline

Model choice fixed the acute problem. These make the harness resilient to the
next weak reviewer rather than dependent on a strong one.

### 2.1 Require cited evidence for every objection

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

**Files.** `forge/prompts.py:84-99`.

**Risk.** Low. Prompt-only.

---

### 2.2 Strip harness scaffolding from stored verdicts

**Problem.** The reviewer echoed the prompt's own headings back as part of its
verdict. That text is stored verbatim (`forge/loop.py:1775-1776`) and fed back
as `prior_verdicts` next attempt (`forge/loop.py:1741`), where it is echoed
again. The block nests on itself each round.

**Evidence.** Steps 17 and 24 contain `## You have already rejected this ticket
/ ### Attempt 1 / … Read these before deciding …` inside the stored verdict.

**Fix.** Truncate a verdict at the first known harness heading before storing it
as a prior verdict. Keep the raw completion in `steps.detail` — that is the
durable record.

**Files.** `forge/loop.py:1775-1776`.

**Tests.** `test_a_verdict_that_echoes_the_prompt_is_not_fed_back`.

**Risk.** Low.

---

### 2.3 Cap the prior-verdict block and make it droppable

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

### 3.1 Allow criteria the spec already states

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

### 3.2 Protect plan-authored context

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

### 3.3 Carry rejection history across cycles

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
| 2 | 2.1, 2.2 | Prompt-only; cheap insurance against the next weak reviewer |
| 3 | 1.2 | Only meaningful once 1.0 can tell empty from unparseable |
| 4 | 2.3 | Prerequisite for 3.3 |
| 5 | 3.3, 3.2 | Information architecture; 3.2 carries a migration |
| 6 | 3.1 | Deliberately loosens a guard; wants a stable baseline |
| 7 | 2.4, 4.1 | Conditional and experimental |

1.6 is a decision, not a code change, and should be settled before the next
baseline run.

Groups 1 and 2 are independent and can go in one branch each. Everything from 6
onward wants the `alllocal` backlog green first, so a regression is
attributable.

**Before re-running `alllocal`:** restore TT-006's spec and context (1.3
follow-up), and delete `src/board.rs` plus its `pub mod board;` line, or the
1.5 baseline is already poisoned on the next start.
