# Convergence: making a long run learn instead of resample

Derived from the `Puzzle-Path` run of 2026-08-22/23
(`C:\Users\nschr\Documents\GodotProjects\Puzzle-Path\.hybridforge\run.db`, run
1). Every file anchor below is against `hybrid-forge` at `08a7584`.

The run lasted 18.2 hours, spent 24.5M tokens across 2,270 logged model calls,
and ended `blocked` with 3 of 10 tickets `done`. That outcome is not the
problem. A long run is fine when it is converging, and the finding here is that
this one was not: over 330 attempts a ticket's failures neither shrank nor
changed shape.

Distinct error kinds per failed step, PF-003, its 177 failures split into
deciles:

```
2.8  2.4  2.4  2.9  1.6  2.4  3.9  2.5  2.2  2.0
```

PF-009, over 225 failures:

```
1.5  1.4  1.5  1.3  1.5  1.3  1.2  1.4  1.3  1.0
```

Flat. PF-003 and PF-005 eventually went green; they got there by resampling,
not by descending. PF-007 ran 430 attempts and 86 retry cycles against an
unsatisfiable ticket without ever concluding anything about it.

What the run was actually failing on, counted across every failed step's
recorded detail:

| ticket | failed steps | dominant error | occurrences |
| --- | ---: | --- | ---: |
| PF-009 | 225 | `trailing-whitespace` | 1,125 |
| PF-003 | 177 | `TS2532 object is possibly 'undefined'` | 512 |
| PF-003 | | `TS18048` + `TS2538` | 313 |
| PF-005 | 142 | `TS5097` import extension | 33 |

117 of PF-009's 160 lint failures had trailing whitespace as their *only*
problem, in a file the tester itself had just written. A one-line sed over the
file clears every one of them. Each instead cost an executor call, a tester
call, an 8-second gdUnit run, and a share of a respec cycle.

The rest of this document is the feature set that would change that curve,
ordered so that each item unblocks the ones below it, followed by the defaults.
Three of those defaults shipped with this document because they name features
that already existed and were switched off; the rest ship with their features.

---

## The three reasons nothing accumulated

Before the features, the mechanisms they are fixing. All three are structural,
not model quality.

### The models are graded against rules they are never shown

`tools/path_forge/tsconfig.json` sets `"noUncheckedIndexedAccess": true`.
`gdlintrc` sets `max-line-length: 125` and disables three specific checks with
reasons. Neither file, nor `package.json`, nor the vitest config, reaches any
prompt: `_sources_for` (`forge/loop.py:3600`) reads only `allowed_files`,
`reference_files`, and the caller's `extra`, and no ticket lists a toolchain
config among them.

So the executor writes idiomatic TypeScript, is handed `TS2532` on every array
index, guesses a fix, breaks something else, and repeats. 512 times. It was
being asked to infer a compiler flag from error messages, one attempt at a
time, with a two-failure memory.

### The ticket's only durable learning slot is rebuilt from the plan every cycle

`_preserve_plan_context` (`forge/respec.py:469`):

```python
anchor = (ticket.original_context or "").strip()
if not anchor or "context" not in revision:
    return False
proposed = (revision["context"] or "").strip()
if _normalise(anchor) in _normalise(proposed):
    return False
revision["context"] = f"{anchor}\n\n{proposed}".strip()
return True
```

The anchor is `original_context` — the plan's paragraph. The ticket's *current*
context is never an input. Cycle N+1's context is therefore `plan + cycle N+1's
paragraph`, and everything cycle N concluded is gone. When respec omits
`context` entirely, `_disarmed_context` resets it to the plan's outright.

The receipt: after 86 respec cycles PF-007's `context` column holds the plan's
paragraph, verbatim, twice. After 67 cycles PF-003's holds the same. Not one
operational conclusion survived 18 hours.

### The executor's failure window is 2, across 430 attempts

`_PRIOR_FAILURES = 2` (`forge/loop.py:3134`). `Store.ticket_failures`
(`forge/state.py:794`) dedupes on exact `name::detail`, so 512 instances of
`TS2532` at different line numbers are 512 distinct facts and the executor is
shown the newest two.

`build_prompt` already carries the right instruction for this
(`forge/prompts.py:456`) — *"if the newest failure is one you have already seen
here, the two changes are undoing each other"* — and it cannot fire, because the
window is too small and the dedupe key is too specific to ever produce a
repeat.

`loop.executorTurns` was `0`, so the conversational prompt shape at
`forge/prompts.py:539`, the one whose whole purpose is to tell the executor
*you wrote these files*, was off. It met its own work as a stranger's on every
attempt.

A fourth, smaller one worth naming: respec repeatedly produced correct
knowledge and the loop threw it away. It proposed "imports must use `.js`
extensions" on PF-003 four times and on PF-005 three times, independently, and
"every declaration needs an explicit type" on PF-009 five times. The criteria
ratchet refused all of them, correctly — these are project conventions, not
acceptance criteria — and there was nowhere else to put them.

---

## Feature 1 — Toolchain context — built

**Status:** shipped. `loop.toolchainContext`, default `true`.

**Problem.** Roles are measured by linters, compilers, and test runners whose
configuration they cannot read. Around 1,900 of this run's recurring errors are
direct consequences of two settings in two files nobody was shown.

**Change.** Before each build and tests call, resolve the toolchain
configuration for every language the ticket's writable files touch, and attach
it to the prompt as read-only reference alongside the existing source blocks.

Resolution is by suffix, walking up from each writable file to the workspace
root, first match wins:

| suffix | files |
| --- | --- |
| `.ts` `.tsx` `.mts` `.cts` | `tsconfig.json`, `package.json`, `.eslintrc*`, `eslint.config.*`, `vitest.config.*`, `jest.config.*` |
| `.js` `.mjs` `.cjs` `.jsx` | `package.json`, `.eslintrc*`, `eslint.config.*`, `jsconfig.json` |
| `.gd` | `gdlintrc`, `.gdlintrc`, `project.godot` |
| `.py` | `pyproject.toml`, `setup.cfg`, `ruff.toml`, `.flake8`, `mypy.ini` |
| `.rs` | `Cargo.toml`, `clippy.toml`, `rustfmt.toml` |
| `.go` | `go.mod`, `.golangci.yml` |

Under its own heading, distinct from `## Reference — read only`, because the
instruction differs. Reference files say *take signatures from here*; toolchain
files say *this is the standard you will be measured against, and code that
violates it fails before anyone reads it*.

**As built.** `toolchain.toolchain_context(config, paths)` resolves the files;
`Orchestrator._toolchain_for` calls it from the ticket's own scope and swallows
anything it raises, because context is never worth the work it is context for.
`prompts._toolchain_message` renders it as its own `user` message ahead of the
ticket, under `TOOLCHAIN_HEADING`, which is in `_DROPPABLE_HEADINGS` — a
repository with a large linter config must not be able to stop a ticket
fitting, and losing a rule the role can still infer from a failure is the
cheaper loss.

Two decisions worth keeping visible:

- **Linter and compiler configuration only.** A build manifest is included only
  where it genuinely grades the code: `package.json` because `type` and
  `scripts` decide what module resolution rejects, `Cargo.toml` for `[lints]`,
  `go.mod` for the language version, `pyproject.toml` because that is where
  `ruff` and `mypy` live. `project.godot` was in the first draft and was cut
  after the smoke test — 4,013 characters of input maps and rendering settings,
  no grading content, riding on every GDScript prompt of a run.
- **The cap takes whole files, not characters.** Half a `tsconfig.json` states
  compiler flags the other half turns off. Per file 4,000 characters, per
  prompt 12,000, and the per-ecosystem order is most-authoritative first so
  what goes is the least load-bearing.

**Measured on the run this came from.** PF-007's real ticket, resolved through
the real config:

```
tools/path_forge/tsconfig.json      420 chars
tools/path_forge/vitest.config.ts   158
tools/path_forge/package.json       222   (scripts and type; 4 keys omitted, named)
```

1,395 characters of prompt including the heading. It carries
`noUncheckedIndexedAccess: true` — the source of 512 `TS2532` and 313
`TS18048`/`TS2538` failures — and `verbatimModuleSyntax` beside `"type":
"module"`, which is where the 33 `TS5097` import-extension failures came from.
A GDScript ticket resolves to `gdlintrc` alone, 1,135 characters, holding the
`max-line-length: 125` that PF-009 hit 143 times.

**Still owed.** Nothing has run a backlog with this on. The claim above is that
the rules now reach the prompt, which is checked; the claim that a role reads
them and writes the guard on attempt 1 is not.

---

## Feature 2 — Format step — built

**Status:** shipped, with one deliberate change of mechanism from the spec
below. `commands.format`, empty by default.

**Problem.** 117 of PF-009's 160 lint failures were trailing whitespace and
nothing else. Each consumed a full attempt out of a budget of five.

**Change.** A fourth command kind, `format`, alongside `lint`, `typecheck`
and `test` — but it runs *before* verification rather than as part of it, and
it rewrites the tree rather than judging it. An attempt becomes:

1. `apply`
2. `tests`
3. **`format`** — over the files this attempt landed
4. `verify` — lint, typecheck, test as before

**Where this departs from the spec.** The original said: run verify, and if it
is red, format and *uncharge* the attempt when a second verify comes back
green. That was the wrong shape and the cost is what shows it. Finding out
whether formatting helped would need a second full verification of every red
attempt — on the run this comes from, roughly 400 further gdUnit sweeps at 8.2
seconds each. Running the formatter first costs milliseconds, needs no second
verify, and makes every failure that survives a real one. There is then nothing
to uncharge: a ticket whose only defect was whitespace simply passes. So there
is no `loop.autoFormat` — the behaviour it was going to gate is the only
behaviour there is.

**What it runs over.** The files the loop itself landed this attempt — the
executor's `written` and the tester's `test_path` — not the ticket's whole
glob. A ticket scoped `src/*.py` has not touched most of `src/`, and
reformatting a file it never wrote is an out-of-scope edit dressed as a
tidy-up.

**Never the reproduction.** On a bug ticket `test_path` is the test that proved
the fault, and it is the standard the fix is measured against — the one file in
the pipeline nothing may rewrite, for the same reason the executor cannot edit
it and `_discard_tests` does not reclaim it. "It only changes whitespace" is a
claim about a third-party binary, and it is not one worth making about the
contract.

**Its failure is never the ticket's.** A missing binary is a configuration
fault. It is logged at `warn`, the pass is skipped, and the attempt is judged
exactly as it would have been with no formatter configured. Parking a correct
implementation over a missing `gdformat` would be a worse bug than the one this
fixes.

**This lowers no bar.** The linter is the project's and its thresholds are
untouched; what changes is that the code is made to meet them before it is
judged, rather than the judgement being softened. A run that quietly relaxed
its own verification is the failure `docs/LOOP-INVARIANTS.md` exists to
prevent, and this is the opposite move.

**Rewrites are reported.** `_format_pass` diffs each file across the call and
logs what changed, with the paths, on the ticket. This is model output being
rewritten by something other than the model: small and mechanical every time it
has been looked at, and the moment it stops being that is the moment somebody
needs to be able to see it.

**Files.** `Orchestrator._format_pass` and `_read_or_none` in `forge/loop.py`,
called between the tests and verify phases of `_attempt`. `format` needed no
new validation — `_validate_commands` is generic over command kinds — and
`toolchain.detect` and the wizard now ask for it, with the
arguments-are-appended contract stated in both prompts.

**Measured.** Against a real `gdformat` and a real `gdlintrc`, on the exact
PF-009 failure shape:

```
before:  scripts/a.gd:5: Error: Trailing whitespace(s) (trailing-whitespace)
         Failure: 1 problem found
format:  T-1: the formatter rewrote 1 file(s) before verification: scripts/a.gd
after:   Success: no problems found
```

Red to green, no model call. On the run this comes from that is 117 of one
ticket's 160 lint failures, each of which had cost an executor call, a tester
call, an 8-second suite run and a share of a respec cycle.

**Still owed.** Same as Feature 1: no backlog has run with it on. What is
checked is that a whitespace-only failure clears without a model call, and that
the failure of the formatter itself cannot park a ticket.

---

## Feature 3 — `learned`: a per-ticket field that only grows — built

**Status:** shipped. `loop.learnedLimit`, default `12`.

**Problem.** The single durable learning slot on a ticket is rebuilt from the
plan every cycle. 86 cycles of conclusions, none retained.

**Change.** A new ticket column, `learned`, holding a deduplicated list of short
statements. Rules:

- **Append-only within a run.** Nothing in the respec path may shorten it. It is
  not a field respec returns wholesale; respec returns `learned_add: [...]` and
  the harness merges.
- **Deduplicated on normalized text** (`_normalise`, `forge/respec.py:440`), so
  the same conclusion proposed on cycles 12 and 40 is stored once with a count.
- **Rendered into the executor and tester prompts** under its own heading, above
  the ticket, framed as established fact about *this repository*: things earlier
  attempts on this ticket established.
- **Never a bar.** It carries no authority over the reviewer and adds no
  acceptance criteria. That distinction is what keeps it out of the ratchet's
  jurisdiction: the ratchet exists to stop the loop raising its own bar, and
  `learned` raises nothing — it only stops the loop forgetting.
- **Waiver-screened.** `_waiver_language` (`forge/respec.py:375`) applies here
  too. "The failing check does not count" is not a learning.

Distinct from `context` on purpose. `context` is the plan's, and the reason
`_preserve_plan_context` and `_disarmed_context` guard it so tightly is that it
is a human's text. `learned` is the loop's own, and it should be free to grow
without the harness having to decide every cycle whether the planner is
smuggling a waiver into a human's paragraph.

**Files.** A `learned` column on `tickets`, a `Ticket.learned` field holding
`[{"text", "count"}]`, and `Store.learn` — the only writer. `update_ticket`
does not name the column, for the same reason it does not name `original_spec`:
a field any caller can shorten is not append-only. `prompts.learned_message`
renders it under `LEARNED_HEADING`, which is droppable; respec parses
`learned_add` and screens it through `_waiver_language` before merging.

**Ordered by how often the loop had to rediscover each one**, because that
ordering is itself the signal: a conclusion reached on four separate cycles is
one the plan should have stated, and it should be the first thing the next
attempt reads.

**Shown to the executor and the tester, not the reviewer.** That is what keeps
it out of the criteria ratchet's jurisdiction and it is the load-bearing part
of the design. The ratchet exists to stop the loop raising its own bar; this
stops the loop forgetting, and the two must not be confused. The prompt says so
in as many words — *established facts about how this project works, not
requirements you are judged against*.

**Done when.** A ticket's `learned` after 20 cycles names the toolchain
conventions its failures demonstrated, and cycle 21 does not rediscover any of
them.

**Still owed.** No backlog has run with it on. What is checked is that the
field accumulates, deduplicates, counts, survives an ordinary `update_ticket`,
reaches both prompts and reaches neither the reviewer nor the criteria.

## Feature 4 — Failure classes, not failure blobs — built

**Status:** shipped. `loop.priorFailures`, default `8`.

**Problem.** `_PRIOR_FAILURES = 2` and a dedupe key of exact detail text. The
executor cannot see that it is in a loop, and neither can the retry brake:
`_evidence_fingerprint` (`forge/loop.py:2334`) hashes raw detail, so a failure
carrying a random hash value produces a new digest every cycle and the
"reproduced the previous cycle's failures exactly" brake never fires. It did not
fire once in 86 cycles.

**Change.** Normalize every failure detail into a set of **classes** before it is
deduped, fingerprinted, or shown to anyone. A class is `(step, code, file)`
where `code` is the tool's own error identifier (`TS2532`,
`trailing-whitespace`, `E501`, `clippy::needless_range_loop`) and line numbers,
column numbers, byte offsets, timings, temporary paths, and bare numeric
literals are masked.

**This is an extension, not a new mechanism.** `signatures()`
(`forge/failures.py:158`) already reduces a tool's output to stable per-error
identifiers, and `_block_key` (`forge/failures.py:182`) already survives the
rebuild noise — renamed cargo hashes, stamped pids — that would otherwise make
every failure look new. What it deliberately keeps is the `-->` location span,
because its existing caller is baseline attribution and *which line* is part of
telling "you broke this" from "this was already broken."

Convergence wants the opposite: `TS2532` at line 40 and `TS2532` at line 51 are
the same fact about the same misunderstanding. So `classify()` is `signatures()`
with the span reduced to its file, plus numeric-literal masking so a hash
mismatch carrying a random value does not read as a new error every attempt.
The two must not be collapsed into one function — attribution needs the line
and convergence needs it gone — but they should share `_block_key`'s parsing so
they never disagree about what counts as one error.

Three things consume it:

- **The executor's prompt** shows the class set with counts, not the last two
  blobs: `TS2532 x40 in src/level/serialize.ts — first seen cycle 3, last cycle
  61`. Plus the newest raw failure in full, as today, because the exact text is
  what you fix against.
- **`Store.ticket_failures`** (`forge/state.py:794`) dedupes on class, so 512
  `TS2532` are one fact with a count instead of 512 facts.
- **`_evidence_fingerprint`** hashes the class set. A cycle that fails the same
  way in different words is then correctly recognised as a repeat.

**Files.** `failures.classify(step, output)` beside `signatures()` and
`distill()`, sharing `_blocks` and the head/location parsing so the two never
disagree about what counts as one diagnostic. `Store.end_step` computes the
classes and stores them on the step; `Store.ticket_classes` counts them;
`Store.ticket_failures` deduplicates on them; `Orchestrator._evidence_fingerprint`
hashes them; `prompts._classes_message` renders the tally under
`FAILURE_CLASSES_HEADING`, which is droppable like every other history block.

**Classified on write, not on read.** A ticket accumulates hundreds of failed
steps and one gdUnit run leaves 780 KB of output behind. Classifying on read
would mean re-parsing megabytes on every prompt; classifying in `end_step`
costs one pass over output that has just been produced. A `classes` column on
`steps` holds the result.

**Measured on the run this came from.** Failed steps to distinct classes:

| ticket | failed steps | raw signatures | classes |
| --- | ---: | ---: | ---: |
| PF-007 | 339 | 250 | **7** |
| PF-005 | 142 | 18 | **10** |
| PF-003 | 177 | 627 | **32** |
| PF-009 | 225 | 351 | **38** |

And the brake, replayed over PF-007's 86 real retry cycles: **11 distinct
class-digests, with cycle 3 identical to cycle 2.** The brake that never fired
in 86 cycles fires on the third. That ticket would have parked for a human
after roughly 15 attempts rather than 430.

---

### What this uncovered: nothing could read a vitest failure

Building the measurement found a defect the measurement was going to be built
on top of. Every pattern in `forge/failures.py` is anchored at the start of a
line, and `vitest` colours its output — so an escape sequence sat in front of
every first character and **not one vitest failure parsed in the entire
18-hour run**:

- `signatures()` returned the empty set, so the baseline amnesty compared
  nothing to nothing on every `.ts` verify step;
- `files_blamed()` named no file, so no `.ts` test failure could be attributed
  to anything;
- `distill()` fell through to the head of the output, which is the vitest run
  banner — and that is what the executor was shown as the failure it was being
  asked to fix, on the ticket that went on to spend 430 attempts.

`failures.strip_ansi` now runs where output is captured in `Orchestrator._shell`
and again at the entry to each parser, for details recorded before it existed.

Two smaller parsing gaps went with it, both about the same runner. `_ERROR` did
not match an indented verdict line, and vitest's ` FAIL  tests/a.test.ts >
suite > case` header is the only place a vitest failure names its file — the
`AssertionError:` block below it carries the message and no path at all. And
`_VERDICT` used `\b` after `×`, which cannot match beside a space, so every
`× suite > case` line fell through to the message path and minted a class per
test case.

---

### Design notes worth keeping

- **A verdict line is classed by its file, not its message.** A runner's
  message *is* the test's own name, so treating it as one mints a class per
  case — the opposite of the point.
- **A step that parses to no diagnostic still gets a class**, from its first
  meaningful line with numbers and quoted symbols masked. A reviewer's
  `REJECT:` is the loop's own protocol, not a toolchain's, and the brake exists
  precisely to notice the same rejection twice. Returning nothing there is what
  a caller cannot survive: a ticket with no classes is a ticket whose evidence
  is always new.
- **`signatures` was not changed.** It keeps the line because attribution needs
  to know which line broke; `classify` drops it because the same rule broken
  twice in one file is one thing to learn. A test asserts they still disagree.

**Still owed.** No backlog has run with it on. The class counts and the replay
above are computed from the recorded run, not produced by a live one.

## Feature 5 — Refused criteria become learnings — rejected

**Status:** built, tested against this run's own data, and reverted. The
premise did not survive contact with the evidence.

**What it was going to do.** When `_merge_criteria` refuses a minted criterion,
route the *content* into `learned` with its imperative stripped, rather than
deleting the sentence along with the refusal. The refusal is right — a ticket
that keeps failing does not need a higher bar — and the observation behind the
feature was that twelve refused criteria across three tickets were all true and
all thrown away.

**Why it was wrong.** Two measurements, both from the run this document is
derived from.

Replaying PF-003's eleven refused criteria through the feature produced a
`learned` block containing these two entries, side by side:

```
- This project requires: all array and string index accesses that are
  guaranteed in-bounds use the non-null assertion operator (`!`) to satisfy
  `strictNullChecks`.
- This project requires: both `.ts` files compile under `strictNullChecks`
  *without* non-null assertions (`!`); all potentially undefined array or map
  lookups are guarded with explicit `!== undefined` checks.
```

They contradict. That is not a bug in the restatement — it is an accurate
record of the oscillation PF-003 was actually in, and putting both in front of
every later attempt as established fact would feed it rather than break it. The
original claim that "every one of them was true" was too generous: a few were
project conventions and several were the planner's shifting theories about how
to satisfy one compiler flag.

The obvious fix is a recurrence gate — promote a criterion only once it has
been proposed on several separate cycles, which is what the entry itself
suggested. Measured across the whole run:

| ticket | minted criteria refused | distinct | most repeats |
| --- | ---: | ---: | ---: |
| PF-003 | 11 | 11 | 1 |
| PF-005 | 7 | 7 | 1 |
| PF-009 | 8 | 8 | 1 |
| PF-007 | 1 | 1 | 1 |
| PF-002 | 1 | 1 | 1 |

**Not one minted criterion was ever proposed twice in the same words.** The
planner rephrases every time, so a gate keyed on text promotes nothing and the
feature is inert on the exact data it was designed from. Matching them
semantically would take a model call to decide whether two sentences are the
same convention, which is a lot of machinery for a fact the next feature gets
for free.

**Where its motivating case actually went.** The observation that started it
was real — the `.js` import-extension rule was rediscovered seven times across
two tickets that never exchanged a word. That rule is a direct consequence of
`"type": "module"` beside `verbatimModuleSyntax`, both of which sit in
`package.json` and `tsconfig.json`, and **Feature 1 now puts both files in the
prompt.** The convention does not need to be learned from failures because it
no longer has to be inferred from them.

What remains genuinely unaddressed is the cross-*ticket* half: PF-005 and
PF-003 could not tell each other anything. That is Feature 6's job, not this
one's.

The refusal log line is unchanged and still names every refused criterion with
the `forge criteria --accept` command that adopts one.

## Feature 6 — What a failed ticket learned outlives it — built

**Status:** shipped. Gated by `memory.write`, which `forge init` now offers with
`dryRun: true`.

**Problem.** 262 memory retrievals, 0 writes. `memory.write` was unset, so
`_record_outcome` returned before it did anything.

That was half of it. The other half is which tickets ever reach that step.
`_record_outcome` runs after review passes, and its reason is right — *a
conclusion drawn from unverified work is a rumour that future tickets will read
as fact*. The consequence is that the tickets which learn the most record
nothing: on this run the two that spent 650 attempts between them both ended
parked, and everything their failures had demonstrated about the project went
into the artifact directory and nowhere else.

**Change.** Two additions, one on each side of that rule.

- **The recorder is shown the ticket's `learned` entries.** `record_prompt`
  never had them — it judged a diff, a verdict and a failure summary, and had
  to infer a convention from all three. The entries arrive with their counts,
  and the prompt says what a count above one means: the loop had to work this
  out more than once, which is the strongest signal available that it belongs
  in memory rather than in a ticket.
- **A ticket that never passed gets its own, narrower pass.** New
  `convention_prompt` and `Orchestrator._record_conventions`, wired to the
  give-up path, the executor's `BLOCKED:` path, and respec's `impossible`.

**Where this bends the rule, and why it does not break it.** The recorder's
rule is about conclusions drawn from unverified *code*. A toolchain fact is not
one: whether `noUncheckedIndexedAccess` is set is something the compiler said,
and it stays true whether or not the ticket that ran into it went on to pass.

The separation is structural rather than a matter of prompt wording. The
convention recorder is shown **only** `learned` — no diff, no verdict, no
failure text, not even the spec. Nothing can reach memory through it that did
not first come through a field that is respec-authored, waiver-screened, and
derived from what the project's own tools printed. Its system prompt refuses
approaches, corrections, and anything scoped to the files the ticket owned; the
worked examples are a compiler flag and an import convention, and `NOTHING` is
named as the common answer.

**Files.** `prompts.CONVENTION_RECORDER_SYSTEM`, `prompts.convention_prompt`,
`prompts._learned_block`; `Orchestrator._record_conventions` and
`Orchestrator._remember` — the write half of `_record_outcome`, split out so a
memory write goes through one set of guards however it was proposed. The
refusal, the outage and what the step log says are identical in both
directions.

**Cost.** One model call per ticket that parks *and* has something in
`learned`; a ticket that learned nothing costs nothing, and neither does a run
with `memory.write` off.

**Still owed.** No backlog has run with it on, and this one is the most
important of the three to watch in dry-run before trusting: it writes to a
store every future session reads, with no undo. `memory.dryRun` logs what it
would have written and writes nothing, which is why `forge init` now turns
write-back on with dry-run set rather than asking twice.

## Feature 7 — Measure convergence and let the loop read it — built

**Status:** shipped, measuring. `loop.flatCycles`, default `0` — the brake is
off, and the section below is why.

**Problem.** Nothing in the loop knew whether it was making progress. A run
that spends 18 hours descending has earned them; one that spends 18 hours
resampling reads identically from the outside — same log lines, same attempt
counts, same "re-delegating" every five minutes.

The backlog-wide brake could not answer it either, and correctly so. It asks
whether *every* unfinished ticket reproduced the last cycle, so a ticket going
nowhere stays invisible for as long as any other ticket is still moving. On
this run PF-007 ran the full 18 hours in exactly that position while PF-003
(13.3 h) and PF-005 (11.6 h) were still landing work, taking a fresh attempt
budget on every one of its 86 cycles.

**Change.** Per ticket, at each cycle boundary, compare the failure classes
this cycle produced against the last cycle's and record the answer:

| state | meaning |
| --- | --- |
| `descending` | classes went away and none arrived — the next cycle is worth running |
| `churning` | some went, others came — a fix is breaking what the last one satisfied |
| `flat` | the same set, again |
| `cleared` | nothing failed this cycle |

Said out loud in the log, with the classes named, because a person deciding
whether to let a long run continue is the reader. `churning` is kept separate
from `flat` rather than folded into "not descending": they ask for different
things. Churning is the executor trading one failure for another, which the
anti-oscillation block in its own prompt is written for and which more attempts
can genuinely resolve. Flat is nothing varying at all.

Cycle boundaries come from `Ticket.cycle_mark`, the highest step id at the last
boundary — step ids are monotonic, so "everything after this" is the current
cycle's evidence without a cycle number on every row.

**The brake was built, measured, and turned off.** `flatCycles` parks a ticket
that has been flat that many cycles running, removes it from the requeue, lets
the rest of the backlog carry on, and offers what it learned to memory on the
way out. All of that works. What does not work is choosing the number.

Replayed against this run's real cycle boundaries, the longest run of identical
cycles per ticket:

| ticket | how it ended | longest identical run |
| --- | --- | ---: |
| PF-007 | failed — unsatisfiable | 3 |
| PF-005 | **done** | **4** |
| PF-009 | blocked — unsatisfiable | 2 |
| PF-003 | done | 1 |

**A ticket that went on to pass sat still for longer than the one that never
could.** At `flatCycles: 3` this parks PF-005 on cycle 16 of 40 and still lets
PF-007 run to cycle 40 of 86. No value separates them, and the direction of the
error is the expensive one: it kills work that was going to land while barely
denting the work that was not.

That is not a reason to discard the signal, and it is a reason not to act on it
alone. Consecutive identical cycles are real evidence that something has to
change; they are not evidence that nothing can.

What changes something is the escalation ladder this measurement drives, and it
is now built — see Features 8 and 9. The ladder parks a ticket when the planner
names the contradiction, which is a reason rather than a count, and
`flatCycles` stays off because parking on the count alone still trades a
stalled ticket for a killed one.

**Files.** `Ticket.cycle_classes`, `cycle_mark`, `flat_cycles`;
`Store.record_convergence`, `Store.last_step_id`, and an `after` argument on
`Store.ticket_classes`; `Orchestrator._convergence` and `_measure_cycle`,
called from `_retry_cycle` before the backlog-wide comparison.

**Done when.** The dashboard can answer "is this ticket getting closer" — it
can now — and the loop acts on the answer, which waits on Features 8 and 9.

## Features 8 and 9 — The escalation ladder — built

**Status:** shipped. `loop.reviewWhenStuck`, default `2`.

Feature 7 measures whether a ticket is going anywhere and, on its own, has
nothing to do about it: the only action available was to park on a count, and
the measurement itself showed no count is safe. These are what it drives.

### The ladder

Two rungs, cheapest first, one per flat cycle after `reviewWhenStuck`. Each
fires once rather than every cycle after it.

**Rung one — ask the reviewer whether the ticket is winnable** (`_stuck_review`).
Review normally sits behind verification, so a ticket failing the same way for
cycles never reaches the one role positioned to say the contract is wrong. On
the run this comes from, 1,350 executor calls produced 17 reviews, and the
ticket that spent 6.7M tokens against an unsatisfiable contract gave the
reviewer 43k of them.

It runs against the red tree, is shown the criteria and the repeated failure
classes together, and answers one of three things:

| verdict | meaning |
| --- | --- |
| `unwinnable` | name the clause and the contradiction, checkably |
| `winnable` | say what the attempts keep doing and what they must do instead |
| `unclear` | cannot tell from what was shown |

`unclear` is a listed answer on purpose. A wrong `unwinnable` parks work that
would have landed; a wrong `winnable` spends another dozen cycles. An
unparseable reply and an unreachable reviewer both resolve to `unclear`,
because a step whose whole purpose is advice must not be able to end a ticket.
The verdict is advisory throughout — it changes no status and passes no tree.

**Rung two — ask the planner the inverted question** (`respec_prompt(stuck=…)`).
`impossible` has been available on every respec call since the field existed,
and in 86 consecutive cycles on one ticket the planner never once reached for
it. Not because it could not see the contradiction, but because it was asked,
every time, to *revise the ticket so the next attempt can succeed* — and that
question has an answer whether or not one exists.

So the question is inverted. The planner is shown the flat count, the classes
that keep repeating, the reviewer's verdict from the rung before, and the
executor's own `IMPOSSIBLE:` claim if it made one, and told to answer one of
two things without splitting the difference: name the criterion that cannot be
satisfied, or name what the next attempt will do differently. Returning the
ticket unchanged is called out as the one reply that guarantees another
identical cycle.

It travels with the ordinary respec rather than as a call of its own — same
planner, same ticket, same evidence, only the question differs.

### `IMPOSSIBLE:`, the executor's second refusal

`BLOCKED:` says *I need something*. `IMPOSSIBLE:` says *no implementation
satisfies this as written*, which is a claim about the ticket rather than a
request.

It exists because the claim was made and nothing read it. On attempt 58 of 430,
an executor wrote "there's a contradiction in the acceptance criteria" into the
middle of an otherwise ordinary reply. It was right — two criteria demanded
different values from the same call — the edits parsed, the sentence did not,
and 372 further attempts followed.

Three properties, each of them load-bearing:

- **It is read even when files came back with it.** That is the shape it
  arrives in: an executor implements its best guess *and* says so. The edits
  still land; the claim travels with them.
- **It is never acted on where it is made.** An executor that cannot pass a
  ticket has every reason to conclude nobody can, so the claim is held and put
  to the planner beside the criteria — marked in the prompt as a claim to
  check, not a finding.
- **It does not wait for the flat count.** A ticket carrying a claim goes
  straight to rung two. Waiting would have read attempt 58's observation about
  300 attempts late: that ticket went on to produce 23 descending and 31
  churning cycles, every one of which resets a flat counter.

### What this changes about parking

`loop.flatCycles` — park on a count — stays off, and the reason is Feature 7's
measurement: a ticket that went on to pass sat still for four consecutive
cycles while the genuinely unsatisfiable one managed three. No threshold
separates them.

The ladder parks on a **reason** instead. When the planner replies `impossible`
it names the contradiction, the existing `result.impossible` path parks the
ticket with that text as its blocked note, and what it learned goes to memory
on the way out. That is a different kind of evidence from a counter, and it is
the one that ended the only ticket on the reference run that ever ended
correctly — PF-009's respec said `impossible` and was right, at cycle 44 of 44.

Walked end to end against PF-007's ticket shape, with a second ticket still
moving so the backlog-wide brake stays quiet:

```
cycle 1: flat=0
cycle 2: flat=1
cycle 3: flat=2  -> stuck review: "unwinnable — criteria 1 and 2 both name hash(0,0,0)"
cycle 4: flat=3  -> inverted respec: impossible
         PARKED: respec: Criteria 1 and 2 demand 1691721052 and 284728508 from
                 the same call.
```

Cycle 4 of 86, for a stated reason. And with an `IMPOSSIBLE:` claim in hand it
parks on cycle 1, which is the case attempt 58 was.

**Cost.** One reviewer call on the rung-one cycle, and nothing on rung two —
the inverted question replaces a respec call that was happening anyway. A
ticket that never goes flat and never claims impossibility pays neither.

**Still owed.** No backlog has run with it. What is checked is that each rung
fires once at the right cycle, that a ticket still descending never reaches
either, that an unreachable reviewer changes nothing, and that the whole ladder
ends in a park with the planner's own words as the reason.

## Feature 10 — Freeze the tests while the criteria are unchanged — built

**Status:** shipped. `loop.freezeTests`, default `true`.

**Problem.** 916 tester calls, 18,253 seconds — more wall clock than the
executor's 16,726, on the role nobody thinks of as expensive. For PF-007 the
tester regenerated a functionally identical file 430 times, several of them
byte-identical in groups of fifteen.

The seconds are the smaller half. The executor was being judged against
assertions rewritten under it every attempt: it fixes what the last test
demanded, and the next attempt measures it against a different test derived
from the same unchanged criteria.

**Change.** Fingerprint the tester's real inputs — criteria, spec, writable
scope, test command — and keep the file on disk while they hold.

**What is deliberately not in the fingerprint: the implementation.** The tests
encode the *criteria*. That is the whole reason the tester is a separate role
from the executor and the reason its file is outside the executor's scope. A
fingerprint that moved with the code would regenerate on every attempt, which
is the behaviour this replaces.

**Four things rewrite them**, each a case where the file on disk is wrong
rather than merely old:

- the criteria, spec, scope or test command changed;
- no tests have been written for this ticket yet;
- the file is not on disk — reclaimed by `_discard_tests`, taken out by
  `_quarantine`, or it never landed;
- the last failure named the test file itself. That is the tester's own to fix
  and nobody else's, and it is also the escape hatch for the case the
  fingerprint cannot see: an executor that renames an export breaks the test's
  import, the failure names the test file, and the tester rewrites.

A bug ticket is untouched by any of it. It authors no tests at all — its
contract was written before the fix, and the party being judged does not get to
add to it.

**Measured against the run this comes from.** Replayed over every recorded
tester call, keeping the file whenever the criteria had not moved and no
failure named it:

| ticket | tester calls | still needed | avoided |
| --- | ---: | ---: | ---: |
| PF-007 | 335 | 38 | **88%** |
| PF-005 | 136 | 38 | 72% |
| PF-002 | 36 | 12 | 66% |
| PF-004 | 6 | 3 | 50% |
| PF-003 | 170 | 128 | 24% |
| PF-009 | 215 | 210 | **2%** |
| **total** | **898** | **429** | **52%** |

The criteria were never touched on any ticket in the entire run, so the
fingerprint held throughout and every rewrite above came from a failure naming
the test file.

**PF-009 is the interesting row, and Feature 2 fixes it.** Its 2% is not the
freeze failing — it is the freeze working correctly against a tree where the
test file genuinely was failing, on trailing whitespace, over and over. With
`commands.format` set, those failures never reach verification at all:

```
PF-009 as-is:               215 calls -> 210 needed,   5 avoided ( 2%)
PF-009 with the formatter:  215 calls ->  55 needed, 160 avoided (74%)
```

Together the two features avoid roughly 624 of 898 tester calls, about 69%, and
the composition is not a coincidence: a formatter removes the class of failure
that is *nominally* the tester's fault and *actually* mechanical, which is
exactly the class that was unfreezing the tests.

**Files.** `Ticket.tests_fingerprint` and `Store.record_tests_fingerprint`;
`Orchestrator._tests_fingerprint` and `_tests_are_current`, gating the tests
step in `_attempt`. The fingerprint is written only once the file is on disk —
recording one for tests that never landed would stop the next attempt writing
any.

**Still owed.** No backlog has run with it. The table above is a replay over
recorded steps, not a live result.

## Defaults

### Shipped with this document

Three settings changed, because all three name features that already exist and
were off:

| setting | was | is | why |
| --- | --- | --- | --- |
| `loop.ratifyPasses` | `0` | `2` | Two unbuildable tickets reached the executor because nothing asked whether they were buildable. 650 attempts, 16.6M tokens, ~16 hours. |
| `loop.executorTurns` | `0` | `4` | The conversational prompt shape existed and was better. 430 attempts each met their own previous answer as a stranger's. |
| `memory.write` (wizard) | prompts, default no | prompts, default yes, `dryRun: true` | 262 retrievals, 0 writes. Dry-run writes nothing and logs what it would have written, so yes is safe as a default. |
| `loop.toolchainContext` | — | `true` | New with Feature 1, below. The roles were graded by configuration no prompt contained. |
| `commands.format` | — | empty | New with Feature 2, below. No formatter is inferred, and `forge init` now asks for one. |
| `loop.priorFailures` | — | `8` | New with Feature 4, below. Replaces a hardcoded 2 that could only ever hold one mistake repeated. |
| `loop.learnedLimit` | — | `12` | New with Feature 3, below. How many accumulated facts about the repository reach a prompt. |
| `memory.write` behaviour | passing tickets only | passing **and** parked tickets | Extended by Feature 6, below. The tickets that learn most are the ones that fail most, and they recorded nothing. |
| `loop.flatCycles` | — | `0` (off) | New with Feature 7, below. The measurement runs regardless; parking on the count alone has no safe threshold, and the ladder parks on a reason instead. |
| `loop.reviewWhenStuck` | — | `2` | New with Features 8 and 9, below. Flat cycles before the loop asks the reviewer whether the ticket is winnable, then asks the planner to name what cannot be satisfied. |
| `loop.freezeTests` | — | `true` | New with Feature 10, below. Keeps a ticket's tests while the criteria they encode are unchanged. |

`forge doctor` gained three findings for the misconfigurations that cost this
run and never turned anything red:

- **`undeclared builds`** — a nested manifest not declared under `workspaces`,
  so the repository verifies as one build and every `*` command runs on every
  ticket. `discover_workspaces` (`forge/toolchain.py:236`) already found
  `tools/path_forge` at depth 2; nothing said what leaving it out cost. On this
  run: 908 runs of an 8.2 s Godot suite against TypeScript tickets, 2.1 hours,
  and 229 MB of passing output under `.hybridforge/artifacts` for PF-007 alone.
- **`test[...] re-runs the typecheck command`** — `test[".ts"]` was
  `tsc --noEmit -p ... && npm run test` while `typecheck[".ts"]` was
  `tsc --noEmit -p ...`. Cheap in seconds, and a sign the two kinds were filled
  in independently.
- The existing `no type check` and `owned by no workspace` findings are now
  named alongside them in the setup plugin, which previously read only the
  model probes.

`retryCycles` stays at `2`. Under Feature 7 an unbounded `retryCycles: -1`
becomes defensible for the first time — the flat-cycle ladder becomes the real
brake, and the cycle count stops being the thing standing between a productive
run and an infinite one — but `-1` should be an operator's deliberate choice,
not a default.

### Shipping with their features

The rest of the keys in this document do not exist yet, and are deliberately
not written into the sample config ahead of the code. A setting that names a
feature nobody built is worse than an absent one: it reads as configured
behaviour and silently does nothing.

Each lands with its feature, at the value its section states:

| key | feature |
| --- | --- |
| `loop.flatCycles` | 7 |
| `loop.reviewWhenStuck` | 8 |
| `loop.freezeTests` | 10 |

### Still unmeasured

`ratifyPasses: 2` is a judgement about the cost of the failure, not a
measurement of the fix. Nothing here has watched ratification run on a real
backlog, and `docs/RATIFY.md` still owes the comparison it always asked for:
the same plan run twice, `ratifyPasses: 0` and `ratifyPasses: 2`. The default
changed because the alternative is not a cheaper check, it is no check.

---

## Order of work

All ten entries are settled: nine built, one rejected on its own evidence.

**Built and independent of the loop's control flow.** Feature 1 puts the
linter and compiler settings the roles are graded by into their prompts.
Feature 2 runs a formatter over what an attempt wrote, before anything judges
it. Between them they address the majority of this run's raw failure volume.

**Built, and the change of shape.** Feature 4 identifies a failure by its kind
rather than its text, which is the only form in which two attempts at the same
mistake are the same thing. Feature 3 gives a ticket somewhere to keep what its
attempts established. Feature 6 lets that outlive the ticket, including —
especially — when the ticket fails.

**Built, and what makes a long run honest.** Feature 7 says per ticket whether
it is descending, churning or flat. Features 8 and 9 act on it: a ticket that
stops moving is put to the reviewer, then to the planner, and ends when
somebody names the reason rather than when a counter runs out.

**Built, and the largest single saving.** Feature 10 keeps a ticket's tests
while the criteria they encode are unchanged. Most of what it saves depends on
Feature 2 having removed the mechanical failures that were unfreezing them.

**Rejected.** Feature 5 was built, replayed against this run's own data, and
reverted — see its entry.

---

## What is still owed

Nothing here has run a backlog. Every number in this document is a replay over
recorded steps, and replays are the weaker evidence in exactly the direction
that matters: they show what the code would have done to failures that already
happened, not what a live run does to the failures it causes itself.

Three things to watch on the first real run:

- **`memory.write` in dry-run.** Feature 6 is the only one that writes
  somewhere a future session reads, with no undo. Read one run's would-have-
  written log before clearing `memory.dryRun`.
- **Whether the ladder ends tickets for the right reason.** Features 8 and 9
  park a ticket when the planner names a contradiction. A planner that says
  `impossible` about a satisfiable ticket is the failure mode, and it looks
  exactly like the success mode in the log — the note is the thing to read.
- **Whether the curve descends.** The measurement that opened this document is
  now computed per cycle by the loop itself. The claim these features make is
  that those deciles go down. That has not been observed.

---

## The first backlog written to fail

`examples/sample-project/STALL.md` is one ticket that cannot succeed, and it
exists because every brake in this document only ever runs on a ticket that is
going nowhere. Four green runs of `SPEC.md` proved that none of these features
misfires on a run that is going well, and nothing more than that.

The defect in it is a *spec* defect and a subtle one: the ticket's fourth
acceptance criterion demands behaviour from `wordcount/counter.py`, which the
ticket may read and may not write. Every criterion is individually reasonable,
the scope is correct for the work described, and only the pair is wrong.

**The first two runs of it finished `done`.** That is the finding, and it is
the failure this whole project exists to prevent — a green ticket over a
criterion nobody met. The chain that produced it had three links, each of which
looked like a smaller problem than it was:

1. **Ratification reworded the criterion.** The plan pinned
   `count_words("Hello, world!")` returns `{"hello": 1, "world": 1}`; the
   ticket that came out of the sign-off pass said it returns `{"hello,": 1,
   "world!": 1}` — the exact output of the code as it stood, so the criterion
   now asserted the behaviour it had been written to reject. The ratchet
   counted criteria and checked provenance against the spec; nothing checked
   that a *value* survived a reword. The party being asked whether it can do
   the work was able to lower the bar it would be judged against.

2. **The tester softened the same value.** With the criterion restored, the
   tester wrote `assertEqual(count_words("Hello, world!"), {"hello,": 1,
   "world!": 1})` — the criterion's own call, asserted against a different
   answer. `foreign_bindings` and `laundered_assertions` both passed it: no
   foreign declaration, no reshaping helper, just the wrong expectation.

3. **Review approved the ticket with nothing testing the criterion.** When the
   rigged file was discarded, the reviewer was told in as many words that
   nothing ran and that it was the only thing between this ticket and `done`.
   It approved anyway — in an attempt where the executor had already reported
   the criterion impossible.

Three guards, each mechanical, each keyed to the exact evidence:

- `respec._softened_values` refuses a same-length reword that drops a value the
  plan pinned in a code span. Prose may be rewritten freely; what the answer
  has to be may not.
- `patch.weakened_criteria` rejects a test that makes a criterion's own call
  and asserts a value the criterion does not state, and the tester is asked
  again with the pair quoted back.
- A discard for *that* reason blocks the ticket rather than leaving it to
  review. A criterion that cannot be encoded without softening it contradicts
  code the ticket may not change, which is a spec defect and a person's to
  settle.

With all three in, the same backlog ends the way it should:

```
ratify failed -> ratify ok      the reword refused, then signed off
build, build, apply
tests failed                    softened twice, discarded
run: blocked | 1 ticket(s) need a human
2 attempts, 1 retry cycle, 22 calls, 106.7k tokens
```

and the note the human gets names the real problem — *a criterion here
contradicts code this ticket may not write, so no honest test of it can pass*.

**What this still has not measured.** The ticket never reached a state where
the failure classes could descend, so `_convergence`, the escalation ladder and
`flatCycles` remain unexercised.

## The backlog that was supposed to be hard

`examples/sample-project/HARD.md` was written to be that run: satisfiable, but
not on the first try. One function, and every detail pinned exactly — a
percentage always written to one decimal place, rounding half away from zero
where `round()` does not, a right-aligned six-character field, padding to the
longest label, a `limit` that folds the remainder into an `other` row whose
label sets that width, and a summary line with singular and plural forms.

It has not been hard once. Four runs, two versions — four exact details, then
nine — and the local executor landed every criterion on the first attempt each
time, in seven model calls and under 60k tokens. The delivered
`shares(counts, limit=0)` was checked against all nine criteria independently
afterwards; it is correct on every one, including the `16.25 -> 16.3` case that
`round()` gets wrong.

So the middle of this document remains unexercised, and the reason is worth
writing down rather than working around: **difficulty that comes from care is
not difficulty for this executor.** What defeats it is a spec that is wrong —
a criterion contradicting code the ticket may not change, two criteria
demanding different values from one call, a design question left unresolved.
Every failure this fixture has produced has been of that kind, and so was the
`Puzzle-Path` run these ten features were derived from: 430 attempts against
criteria that could not all hold at once.

That reframes what the ten features are for. They are not a way to make a
capable model converge on well-specified work — it already does, first try.
They are the machinery that stops a *defective spec* from consuming a night,
and the honest test of them is a backlog whose specs are subtly wrong in ways
that take several attempts to expose. `STALL.md` is the crude version of that
and stops in one attempt. The version worth writing next is a ticket whose
criteria are individually satisfiable and jointly impossible only for inputs
the executor reaches on its second or third attempt.

**The two false positives that came out of these runs.** `weakened_criteria`
was written from the stall run and immediately parked two healthy tickets:

- A criterion explains itself in code spans as well as pinning a value —
  ``16.25`` and ``round(16.25, 1)`` in the rounding criterion above — and every
  span was being read as a required value. Only the span *following a call* is
  the contract now.
- The tester wrote its expected list one element per line with a trailing
  comma, and the check compared whitespace-collapsed strings, so the honest
  test differed from the criterion by a space after `[`. Comparison now ignores
  whitespace entirely and checks a bracketed value element by element — which
  also gives up on catching a padding-only softening, the right direction to
  miss in for a net whose cost when wrong is a parked ticket.
