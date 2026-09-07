# Adaptive Ticket Loop — Specification

**Status:** built 2026-09-06 with its trigger off, and exercised live on
2026-09-07 by arming it against a fixture — §6.6 is what those runs found.
Steps 1 through 6 of §12 have landed: the live run the order was waiting on,
the objection-splitter replay, the volume counters, completion against the
frozen criteria, the ratification counters, and SPLIT itself behind
`loop.volumeThreshold`, which ships `0`. Steps 7 (RESTART) and 8 (plan-time
prediction) are deliberately not built and the section on each says why. What
every section below describes is either shipped code or a decision not to
write it; the "owed" notes are kept where the thing is still owed.

Revised 2026-08-28 against `main` at `91cb82e`. The original draft was written
before the convergence work landed. Roughly half of what it specified already
existed under other names, one of its central mechanisms had been built
elsewhere and reverted on measured evidence, and two of its rules pointed the
wrong way against what the loop enforces. That revision kept the draft's
structure so the two could be read side by side.

**Scope:** the code→review iteration loop in `forge go`, plus a small amount
of plan-time work in `forge ingest`.

---

## 0. What this draft asked for, and what already exists

Nine of the ten features in [CONVERGENCE.md](CONVERGENCE.md) shipped between
this draft and now, derived from the `Puzzle-Path` run of 2026-08-22/23. They
overlap this document heavily.

| Draft section | Current state |
| --- | --- |
| §3.1 Objection ID | **Built for tool output, not for review.** `failures.classify(step, output)` reduces a diagnostic to `(step, code, file)` with line numbers, offsets and numeric literals masked. A reviewer `REJECT:` is classed from its first meaningful line, so one review is one class however many complaints it carries. |
| §3.2 Attempt ledger | **Built in two halves, minus `approach`.** `Ticket.learned` (`loop.learnedLimit`, default 12) is an append-only, deduplicated, counted list of what earlier attempts established, rendered into the executor and tester prompts. `loop.priorFailures` (default 8) carries prior failure classes with counts. `loop.executorTurns` (default 4) replays prior attempts as real conversation turns, which is the ledger's `approach` field obtained for free. |
| §3.3 Category vocabulary | **Not built, and superseded in its main use.** Classes carry the tool's own error identifier rather than a closed taxonomy. |
| §4 Failure classification | **Built, with a different taxonomy.** `Orchestrator._convergence` compares this cycle's class set against the last and records `descending`, `churning`, `flat` or `cleared`. There is no volume axis. |
| §5 Remediation state machine | **Partly built.** AMBIGUOUS→RESPEC exists as respec with its `impossible` verdict. ESCALATE exists as the two-rung ladder (`loop.reviewWhenStuck`, default 2). RESTART and SPLIT do not exist. |
| §5.2 RESPEC permissions | **Inverted against what shipped.** The draft permits adding criteria and forbids removing them. `respec._merge_criteria` protects plan-authored criteria from removal *and* refuses new ones. See §5.2. |
| §6 Ticket splitting | **Not built.** No parent/child relation exists on `tickets`. |
| §7 Rule promotion | **The recurrence-gated form was built, measured against this run's own data, and reverted** — Feature 5. Its motivating case is now served by Feature 1 (toolchain context) and Feature 6 (conventions to memory from parked tickets). See §7. |
| §8.1 Criteria drift audit | **Built, and stricter than specified.** `original_spec`, `original_criteria` and `original_context` are frozen at ticket creation; `Ticket.contract_criteria` is what a revision may not walk back; `loop.respecCriteria` defaults `false`. Completion is not verified against the frozen criteria — that half is still owed. |
| §8.2 Signer participation | **The data exists, the counter does not.** `ratify_notes` records `{role, signed, blocking, suggestions}` per pass per ticket. Nothing aggregates it. |
| §8.3 Sign-off efficacy | **Not built.** Ratification runs (`loop.ratifyPasses`, default 2) and nothing measures whether it caught anything. One run has since been read by hand — 3 of 5 tickets changed by the pass before any code existed, and one role that had signed off on nothing across 16 earlier passes — but reading a run by hand is what this section exists to replace. See [RATIFY.md](RATIFY.md). |
| §9 Plan-time prediction | **Not built. Its substrate now exists.** |
| §10 Telemetry | **Mostly built.** `steps.classes`, `tickets.cycle_classes`, `cycle_mark`, `flat_cycles`, `tests_fingerprint`, `abandoned_values`, `impossible_fingerprint`, plus the `usage` table. |
| §11 Thresholds | **Renamed.** See §11 for the mapping to real config keys. |

### The standing caveat this document must not ignore

**No backlog has run with any of it on.** Every number in CONVERGENCE.md is a
replay over the recorded `Puzzle-Path` steps. Invariant 9 of
[LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) — *validate against recorded artifacts,
not against reasoning* — cuts both ways here: a replay is recorded-artifact
evidence, and it is still only evidence about failures that already happened,
not about the failures the new code causes itself.

Everything in this document sits on top of that. A split mechanism built before
the ladder has met a live run is a second unvalidated layer over a first, and the
failure mode is that neither can be attributed when the run goes wrong. **§12
orders the work so a live run comes first.**

---

## 1. Purpose

The draft's framing survives and is still the useful one. The loop conflates two
failure modes:

- **Volume churn** — many distinct objections, each appearing once or twice. The
  executor is dropping constraints it cannot hold simultaneously. The ticket is
  too big.
- **Coupling churn** — a small set of objections repeating. Constraints conflict,
  or an early structural decision in the accumulated diff blocks the fix.

The loop as it stands measures the second axis well and the first not at all.
`descending` / `churning` / `flat` compares consecutive class sets; nothing reads
the cardinality of the set. So a ticket failing on 38 distinct classes and one
failing on 7 look identical to every brake in the system, and the only remedy
either is offered is another attempt, a respec, or a park.

What is left for this document:

1. A volume axis on top of the existing convergence measurement (§4)
2. Ticket splitting with a scope-conservation invariant (§6) — the only genuinely
   new remedy
3. Completion verified against the frozen criteria (§8.1)
4. Ratification instrumentation (§8.2, §8.3)
5. Plan-time prediction from telemetry that now exists (§9)

Dropped from the draft: rule promotion as specified (§7), the closed category
vocabulary (§3.3), and RESTART as an unconditional rung (§5.1).

Explicit non-goal, unchanged: no model weights are trained or modified.

---

## 2. Definitions

Aligned to the terms the code already uses.

| Term | Meaning | In the code |
| --- | --- | --- |
| **Attempt** | One executor generation for a ticket. | `tickets.attempts` |
| **Cycle** | A retry cycle: everything after the last `cycle_mark`. | `Ticket.cycle_mark`, monotonic step ids |
| **Class** | A failure reduced to `(step, code, file)`. Replaces the draft's "objection ID" everywhere below. | `failures.classify` |
| **Cycle state** | `descending` / `churning` / `flat` / `cleared`. | `Orchestrator._convergence` |
| **Learned** | Append-only counted facts this ticket's attempts established. Replaces the draft's "ledger". | `Ticket.learned`, `Store.learn` |
| **Contract criteria** | The criteria a revision may not walk back. | `Ticket.contract_criteria` |
| **Rung** | One step of the escalation ladder. | `_stuck_review`, `respec_prompt(stuck=…)` |

The draft's "Run" is dropped: it collided with `runs` in the schema, and
`cycle_mark` already delimits what it was for.

---

## 3. Data structures

### 3.1 Objection identity — what is left to do

`classify()` already gives stable identity to anything a tool printed. The gap is
review: an `ACCEPT`/`REJECT` verdict followed by prose collapses to one class
regardless of how many distinct complaints it carries, so a reviewer raising six
objections and one raising a single objection are indistinguishable to the volume
axis §4 wants to add.

**Do not rewrite the reviewer's contract to fix this yet.** The draft proposed
structured JSON objections. That change is expensive in the exact place the loop
is most fragile — `forge/prompts.py:5` says the `REJECT` verdict is what the loop
branches on, and the verdict parser exists because models decorate the one word
it needs. Wrapping the reasoning in JSON risks the verdict line for a signal that
has no consumer yet.

**Instead, in this order:**

1. **Count objections without changing the contract.** The reviewer is already
   required to cite: *"an objection with neither is not a finding"*
   (`forge/prompts.py:295`). Citations are countable. Split the rejection body on
   its citation anchors and class each point by `(review, cited-file,
   masked-head)`, reusing `failures._blocks` head parsing so review points and
   tool diagnostics never disagree about what one complaint is.
2. **Replay it before trusting it.** ✅ Done — `scripts/review_points.py`, over
   every recorded `*-review.md` in this repository and both `forge-evidence`
   trees. 62 reviews, 24 of them rejections, and **61 objections inside them**:
   2.54 per rejection, 23 of 24 carrying more than one, and the largest
   carrying twelve — one per acceptance criterion, each quoting the string it
   had searched for and not found. The volume axis reads review.
3. **Only if step 2 shows real spread**, tighten the reviewer prompt to number
   its points — a numbering instruction, not a format change. Step 2 showed
   the spread and this is still not done: the splitter reads bullets, numbers
   and blank-line paragraphs already, and the recorded rejections are bulleted
   without being asked. A prompt change here would be spending the loop's most
   fragile contract on a format it is already producing.

`criterion_ref` from the draft is worth keeping as a *request* in the reviewer
prompt rather than a schema field: "name the criterion each point violates." It
is what §8.3 needs and it costs one sentence.

### 3.2 The ledger — built, with one hole

`learned` plus `priorFailures` plus `executorTurns` is the ledger the draft asked
for, and the conversational replay is a better `approach` field than a
self-reported summary: the executor's actual prior reply is in the transcript as
an `assistant` message, not a one-line paraphrase of it.

The hole is **cross-ticket**. `learned` is per ticket, and Feature 6 carries it
outward only through MemPalace on a ticket's way out. Two sibling tickets in the
same backlog still cannot tell each other anything mid-run — CONVERGENCE names
this directly: *"PF-005 and PF-003 could not tell each other anything."* It is
also a prerequisite for §6: split children must read what their siblings
established.

**Owed:** run-scoped learnings. `Store.learn` gains a run-level tier that split
children share, ordered by count like the per-ticket tier and rendered under the
same droppable heading. Not a new prompt block.

### 3.3 Category vocabulary — dropped

The draft's closed vocabulary was in service of counting, and counting now keys
on the tool's own error identifier, which is stabler than any taxonomy a model
picks from a list. Two entries were load-bearing and both have homes:

- `spec-ambiguity` — the executor's `IMPOSSIBLE:` refusal and respec's
  `impossible` verdict, both of which park for a stated reason.
- `scope-violation` — the scope gate, which rejects out-of-scope writes before
  they touch disk.

---

## 4. Failure classification — adding the volume axis

Current state, per cycle, in `_convergence`:

```
descending  classes went away and none arrived
churning    some went, others came
flat        the same set, again
cleared     nothing failed this cycle
```

That is the coupling axis. It answers *is the set moving* and never *how big is
the set*. The volume axis is one number the loop already computes and discards:

```
distinct_classes = |classes for this ticket|
```

`Store.ticket_classes` returns exactly this. Nothing reads its cardinality.

### 4.1 The measured objection to the draft's threshold

The draft proposed `VOLUME_THRESHOLD = 8` distinct objection IDs, above which a
ticket splits. Against the reference run's own class counts, from Feature 4's
table:

| ticket | distinct classes | how it ended | the draft would |
| --- | ---: | --- | --- |
| PF-007 | **7** | failed — genuinely unsatisfiable | not split |
| PF-005 | 10 | **done** | split |
| PF-003 | 32 | **done** | split |
| PF-009 | 38 | blocked — unsatisfiable | split |

**The threshold splits two tickets that went on to pass and leaves the one
unsatisfiable ticket alone.** This is the same shape as the `flatCycles` finding
that turned that brake off: no value separates the cases, and the direction of
the error is the expensive one — it decomposes work that was about to land.

That is not a reason to discard the signal. It is a reason not to act on it
alone, which is the conclusion Feature 7 already reached about its own.

Two caveats on the table, both of which the replay in §12 must settle:

- These are lifetime counts per ticket, not per cycle. A per-cycle count may
  separate the cases where a lifetime count does not.
- They are tool-output classes. If §3.1 step 2 finds real spread in review
  points, the volume axis should be recomputed over those, which is what the
  draft meant by "objections" in the first place.

**A third caveat, from arming it.** A class is `(step, code, file)`, so the
count tracks how many *files* a project has as much as how big a ticket is.
`Puzzle-Path`'s 32 and 38 came from a large two-language tree; the largest any
ticket in `examples/sample-project` has produced across fourteen runs is 2, and
that ticket was the deliberately unsatisfiable one. A threshold fit on either
number describes the other badly, which means `volumeThreshold` is not only
unvalidated — it is not *portable*. Whoever arms it should read their own
repository's counters first, which is what §4.2 records them for.

### 4.2 What to record now

Record the volume axis alongside the cycle state and act on neither:

```
cycle_state       descending | churning | flat | cleared     (built)
distinct_classes  cardinality of the ticket's class set       (new)
new_classes       classes first seen this cycle               (new)
```

Said in the log beside the existing convergence line. `new_classes` is the
discriminator worth watching: a ticket that keeps minting classes it has never
seen is a different animal from one cycling through the same forty, and a
lifetime count cannot tell them apart.

**Done when** the dashboard can answer "is this ticket's failure set growing, and
is the growth new material or the same material rediscovered" — and the loop
still does nothing about the answer until §12's replay says which threshold, if
any, separates the cases.

---

## 5. Remediation

### 5.1 RESTART — deferred, not adopted

The draft's argument stands: local search on an accumulated diff cannot leave the
basin its structure defines, and discarding the diff is the only move that
changes the basin.

It is deferred for two reasons.

- **The trigger has no safe threshold.** RESTART fires on the same flat-cycle
  count `flatCycles` was measured against and turned off for. A restart is
  cheaper than a park — it discards a diff, not a ticket — but it discards it on
  exactly the evidence that would have killed a ticket which went on to pass.
- **The ladder may make it unnecessary.** Rung two asks the planner to name what
  the next attempt will do differently. A planner that can name it has produced
  the restart's benefit without discarding anything; one that cannot has produced
  an `impossible` verdict, which is a better outcome than a restart.

**Revisit when** a live run shows the ladder answering `winnable` and the ticket
staying flat afterwards. That is the case restart is for, and it has not been
observed.

If it is built: discard the working diff, re-delegate from the ticket spec plus
`learned` at elevated temperature, reset `flat_cycles` and `cycle_classes` but
**not** `learned` or `abandoned_values`, bounded by `loop.maxRestarts`.

### 5.2 RESPEC — the draft's rule is backwards

The draft says: *"Permitted: adding criteria. Not permitted: deleting or
weakening acceptance criteria."*

`respec._merge_criteria` refuses **both** directions, and the reason for refusing
additions is measured:

> Respec runs on a ticket that has just exhausted its attempts, and its whole
> purpose is to produce one the next attempt can satisfy; raising the bar at that
> moment cannot serve that purpose. Left open, the bar only ever rose — one
> ticket went from the plan's nine criteria to sixteen across six cycles, and the
> criterion blocking it at the end was one respec had invented two cycles
> earlier.

Plan-authored criteria the planner tries to remove are put back; criteria it
tries to add are refused and logged with the `forge criteria --accept` command
that lets a **human** adopt one. Criteria a previous revision invented are not
protected, so an already-inflated ticket unwinds on its next revision.

**No change to the ratchet is specified here.** The draft's §5.2 should be read
as describing the human path — `forge criteria --accept` — not the planner's.

What respec may still do, unchanged: rewrite `spec`, sharpen wording, add
examples, pin down ambiguity, and propose `learned_add`, which is waiver-screened
and merged rather than replaced.

### 5.3 SPLIT — see §6

The one remedy in this document that does not exist in any form.

### 5.4 ESCALATE — built

Two rungs, one per flat cycle after `loop.reviewWhenStuck`, each firing once:

1. **`_stuck_review`** — the reviewer is asked against the red tree whether the
   ticket is winnable at all, answering `unwinnable`, `winnable` or `unclear`.
   Advisory throughout; an unreachable reviewer and an unparseable reply both
   resolve to `unclear`, because a step whose purpose is advice must not be able
   to end a ticket.
2. **`respec_prompt(stuck=…)`** — the planner is asked the inverted question and
   may answer `impossible`, which parks the ticket with the contradiction as its
   blocked note and offers what it learned to memory on the way out.

An executor `IMPOSSIBLE:` claim skips to rung two immediately and does not wait
for the flat count.

**The draft's CONFLICT class maps onto this and needs no new machinery.** An
alternating objection pair *is* a contradiction the reviewer can name at rung
one, which is what `unwinnable` asks for — in words rather than in a counter.

---

## 6. Ticket splitting

The remaining new mechanism, and now built — `forge/split.py`, with the trigger
in `Orchestrator._consider_split` and the gate in `_close_split_parents`.
`loop.volumeThreshold` ships `0`, so nothing below happens on a default
configuration. What follows is the specification it was built to; where the
code chose one of two readings, the choice is named.

### 6.1 The invariant

> The union of the children's acceptance criteria must cover the parent's.

This is what makes splitting safe where respec is not — scope is conserved by
construction rather than by judgement. It is also the criteria ratchet's rule
applied across a decomposition instead of within one ticket, which is why it
belongs beside `respec._merge_criteria` and not in a new module.

Enforce it mechanically:

- Every parent criterion carries a stable ID. `Ticket.contract_criteria` already
  distinguishes plan-authored criteria from the loop's own; the split covers
  **contract criteria**, not the current list, so a parent that invented criteria
  cannot pass them down as obligations.
- Every child declares which parent criterion IDs it covers.
- After split, assert `union(covered) == parent.contract_criteria`.
- Uncovered criteria fail the split and escalate.

A parent criterion may be covered by several children or by exactly one. Never by
zero.

### 6.2 Schema

Minimum, all three through `_ADDED_COLUMNS` like every column added since the
first release:

```
tickets.parent_ticket_id   TEXT NOT NULL DEFAULT ''
tickets.covers             TEXT NOT NULL DEFAULT '[]'   -- parent criterion ids
tickets.split_depth        INTEGER NOT NULL DEFAULT 0
```

The parent becomes a gate rather than a work item: it holds the green-tree
requirement (§6.4) and completes when every child does.

### 6.3 Who proposes the split, and what checks it

The planner proposes the decomposition, given the parent ticket, its `learned`
entries with counts, and the class histogram from `Store.ticket_classes`. Classes
carry a file, so the histogram offers a file-shaped seam and no other; that is
the split axis, and a planner proposing a feature-shaped one against a
file-shaped histogram is proposing something the evidence does not support.

**Ratification already covers the review half.** `ratify()` puts a ticket to every
role before it is built and parks it when no majority signs. A proposed child is
a ticket that has never been built, so it enters that path with no new machinery
— and because the split came out of the ladder, the roles have something concrete
to object to. A child no role will sign returns to the planner once, then
escalates the parent.

### 6.4 Constraints on children

- **Green-tree rule.** Each child leaves build, typecheck and tests passing. Work
  that cannot splits along mechanical lines — by file, by call site — with the
  parent holding the gate. A child inherits the parent's `baseline_tree` rather
  than taking a fresh one, or invariant 14's *red the backlog did not start with*
  is computed against the wrong tree and the child is blamed for the parent's
  own red.
- **Floor.** At least one acceptance criterion and at least one test. Below that
  the fixed cost of a context load plus a review round exceeds the benefit.
- **Ceiling.** More than `loop.maxChildren` proposed children means the parent was
  mis-scoped at plan time; escalate rather than split.
- **Depth.** `loop.maxSplitDepth`, default 2. A child that fails at max depth
  fails its parent — it does not split again and does not partially complete.
- **Learnings are inherited as a snapshot**, not shared. §3.2's run-scoped
  tier is not built, so a child reads what its *parent* established and not
  what its siblings are establishing beside it. That is the cheaper half of
  §13's open question, and the half that does not need a new tier in
  `Store.learn`.
- **Tests are not inherited.** `freezeTests` fingerprints criteria, spec, scope
  and test command. A child has different criteria and a narrower scope, so its
  fingerprint differs and its tests are written fresh. The parent's test file
  goes through the existing `_discard_tests` path — except on a bug ticket, whose
  reproduction is never reclaimed and which does not split at all: its contract
  was written before the fix and the party being judged does not get to add to
  it.

### 6.5 Ordering and shared context

Children execute in declared dependency order through the existing `needs` field
and `dep_stamp` — no new dependency mechanism. Each child's delegation carries
the parent spec as reference context and the run-scoped learnings from §3.2, so
later children read what earlier siblings established rather than rediscovering
it.

If child N breaks child N−1's criteria, that is coupling churn at the parent
level. Do not re-split. Escalate, and `reopenStaleDependents` handles the requeue
it already handles today.

---

### 6.6 What the first live splits found

The trigger was armed at `volumeThreshold: 2` against
`examples/sample-project/STALL.md` on 2026-09-07 and the ticket was decomposed
five times. Every run found a defect, and **none of them was in `split.py`** —
the module the unit tests cover hardest. Each lived at a seam between this
mechanism and machinery that predates it, which is the part a test calling
`_consider_split` directly cannot see.

- **A cycle that only split ended the run.** `_consider_split` takes the parent
  out of `eligible`, because it is a gate now, and puts its children on the
  backlog as pending tickets. When the parent was the only eligible ticket the
  list emptied, and the guard reading an empty list as *nothing left to retry*
  stopped the run with two freshly created children never worked. A cycle that
  decomposed now continues the run, and is charged to the retry budget: a cycle
  that produces work without spending one is how an unbounded run gets built
  out of bounded parts.

- **Index coverage let the hard half of a criterion be dropped.** This is the
  one that matters, because it manufactured a *false pass* on the backlog that
  exists to be unsatisfiable — `done: 3`, green suite, reviewer approval,
  parent gate closed. `covers` is a claim, and a claim checked by counting is
  satisfied by any restatement. The planner claimed parent criterion 4 —
  ``count_words("Hello, world!")`` returns ``{"hello": 1, "world": 1}``, *so
  that* ``summarize(...)`` returns ``2 word(s), 2 occurrence(s)`` — and wrote
  its child's criterion as the second clause alone, which passes on the
  existing counter because two punctuated keys still count as two words. The
  half it dropped is the half that cannot hold without writing a file the
  ticket may not write. §6.1 says scope is conserved by construction; the first
  implementation conserved the numbering. **A covered criterion now crosses to
  the child verbatim** and is frozen as its `original_criteria`, with the
  planner's own criteria kept after it, where they narrow a child's work
  without loosening the parent's contract.

- **A child was ordered behind its own parent, and the run reported `done` over
  the deadlock.** The proposal was clean — no child asked to wait on the
  parent. `respec._order_shared_files` reruns `derive_needs` on every revision,
  and that rule orders any two tickets writing the same file by position: a
  child writes its parent's files *by construction*, so the rule ordered it
  behind a gate that was waiting for it, every time. `derive_needs` now skips a
  ticket in status `split`, which writes nothing further, while siblings
  sharing a file are still ordered against each other. `check` also refuses a
  child that waits on anything but a sibling, and refuses a sibling cycle.

- **The finish tally counted `blocked` and `failed` only.** A gate that never
  closes has no failure of its own — what failed is a child, and a child parked
  as unreachable is `skipped` — so the arithmetic reported success over work
  nobody did. An open gate now counts as needing a human.

**The fifth run is what the fixture was written to produce**, and it is better
than the pre-split behaviour: `ST-001-a` done over criteria 1-3, `ST-001-b`
blocked at ratification with the planner naming the inherited criterion it
cannot satisfy, and the parent blocked because a parent does not partially
complete. Three quarters of the work landed and the note names the single
obligation that could not be met — which is decomposition earning its place
rather than losing a whole ticket to its impossible quarter.

What the five runs say about the *threshold* is nothing at all. They say the
plumbing survives contact with a model: the proposals parsed, the invariant
held against real criteria, and the failures were all in the wiring around it.

---

## 7. Rule promotion — withdrawn, with the evidence

**The draft's §7 was built as Feature 5 and reverted.** Its recurrence gate is
the exact mechanism the draft specified: promote a lesson once it has been
observed across several distinct tickets. Measured against the reference run:

| ticket | refused criteria | distinct | most repeats |
| --- | ---: | ---: | ---: |
| PF-003 | 11 | 11 | 1 |
| PF-005 | 7 | 7 | 1 |
| PF-009 | 8 | 8 | 1 |
| PF-007 | 1 | 1 | 1 |

**Not one was ever proposed twice in the same words.** The planner rephrases
every time, so a gate keyed on normalized text promotes nothing and the feature
is inert on the exact data it was designed from. Matching them semantically needs
a model call per pair.

Worse than inert: replaying PF-003's refusals through it produced two entries,
side by side, demanding opposite things about non-null assertions — an accurate
record of the oscillation that ticket was in, and putting both in front of every
later attempt as established fact would have fed it rather than broken it.

**Where the draft's §7 went instead:**

- Its motivating case — the `.js` import rule rediscovered seven times across two
  tickets — was a direct consequence of `"type": "module"` beside
  `verbatimModuleSyntax`, and Feature 1 now puts both files in the prompt. The
  convention does not need to be learned from failures because it no longer has
  to be inferred from them.
- Its cross-ticket half is Feature 6: a parked ticket's `learned` reaches
  MemPalace through `_record_conventions`, which is shown **only** `learned` — no
  diff, no verdict, no failure text, no spec — so nothing reaches memory that did
  not first pass the waiver screen.
- Its always-on, unretrieved property is Feature 1's, for the one class of rule
  where always-on is defensible: configuration the tools actually enforce, read
  from the repository rather than inferred by a model.

**The one part worth reviving, later:** §7.3 graduation — a rule that keeps
recurring after promotion is not a prompting problem, and should become a
deterministic check. That argument is independent of the promotion pipeline and
applies directly to `commands.format`: the trailing-whitespace class that cost
PF-009 1,125 failures is a formatter's job, not a prompt's, and Feature 2 shipped
that specific case. Generalising it — surfacing *this class recurs after the
prompt was told about it, write a check* — is worth a report before it is worth
machinery.

---

## 8. Integrity checks

### 8.1 Criteria drift — built forward, owed backward

Frozen at ticket creation and never rewritten: `original_spec`,
`original_criteria`, `original_context`. `contract_criteria` is what a revision
may not walk back. `loop.respecCriteria` defaults `false`, so by default respec
does not touch criteria at all.

**Owed: verification at completion.** Nothing checks the merged result against
`original_criteria` rather than against the current list. With
`respecCriteria: false` the two rarely diverge, which is why this has not bitten
— but it is the check that makes the flag's default a *default* rather than a
load-bearing safety, and a ticket that passes its respecced criteria while
failing its original ones should be reported as **scope-reduced**, not as passed.

Whether that verification is a model judgement or a test-suite check: a model
judgement, because criteria are prose and the suite encodes the current ones. It
runs once per ticket at completion, on the reviewer, and its verdict is a report
— never a status change. A check that can retroactively fail a merged ticket is a
brake with no measured threshold, which is the mistake §4.1 names.

### 8.2 Ratification participation

The data is recorded and nothing reads it. `ratify_notes` holds
`{role, signed, blocking, suggestions}` per pass per ticket, and `ratify_status`
holds the outcome.

**Owed:** an aggregate over the run — per role: tickets voted on, times signed,
times blocking, points raised. A role whose block rate is zero over
`loop.participationWindow` tickets is surfaced. This is indistinguishable from
genuine agreement from inside the loop, which is exactly why it needs an explicit
counter rather than a judgement.

Report only. Nothing changes behaviour on a rubber-stamping role — the remedy is
a human reading the number and changing the prompt.

### 8.3 Sign-off efficacy

`ratifyPasses: 2` is, in CONVERGENCE's own words, *"a judgement about the cost of
the failure, not a measurement of the fix."* [RATIFY.md](RATIFY.md) still owes the
comparison it always asked for: the same plan run twice, at `ratifyPasses: 0` and
`ratifyPasses: 2`.

**Owed, and cheap:** per ticket, count the failure classes and review rejections
raised after sign-off whose subject was visible at sign-off time — a criterion
that already existed, a file already in scope. A high rate means sign-off is
answering the wrong question, and the draft's remedy is the right one: ask each
role *"what would you reject in an implementation of this spec"* rather than *"do
you approve this spec"*. That is a prompt change with a number behind it, which
is the only kind worth making here.

---

## 9. Plan-time prediction

Split-on-failure pays the failed cycles before learning anything. The draft
wanted the decision moved earlier, and the telemetry it needs now exists.

Log per ticket at completion: `criteria_count`, `files_touched` (writable scope),
`cycles_to_completion`, `attempts`, `distinct_classes`, `final_state`,
`was_split`, `ratify_status`.

After `loop.predictionMinSamples` completed tickets, fit the simplest thing that
can be read off a scatter plot — a threshold on `criteria_count` and
`files_touched` above which median cycle count spikes. No ML: a two-variable
threshold is inspectable, and inspectable matters more here than accurate,
because §4.1 is what happens when a threshold is trusted without being seen.

**This is last, not first.** One recorded run is one sample of a joint
distribution over model, repository, language and plan quality. `Puzzle-Path` is
TypeScript and GDScript against a strong executor; `plex-namer` is Java/Gradle
against a deliberately weak one. A threshold fit on either predicts the other
badly, and fitting on both without saying so produces a number that describes
neither.

---

## 10. Telemetry

Most of the draft's schema exists. What it asked for, against what is stored:

| Draft field | Stored |
| --- | --- |
| `ticket_id` | `steps.ticket_id` |
| `parent_ticket_id` | **owed** — §6.2 |
| `run_id`, `round` | `steps.run_id`; cycle from `Ticket.cycle_mark` and monotonic step ids |
| `objection_ids` | `steps.classes` |
| `distinct_ids_cumulative` | computable from `Store.ticket_classes`; **not recorded per cycle** — §4.2 |
| `max_repeat_cumulative` | class counts exist; the per-cycle high-water mark does not |
| `classification` | `Ticket.cycle_classes` and `flat_cycles`, via `Store.record_convergence` |
| `action_taken` | `events` rows for the rungs; no single column |
| `active_rules` | withdrawn — §7 |
| `duration_ms` | `steps.started_at` and `ended_at` |
| `tokens` | the `usage` table, with cache and cost columns |

**Owed:** the two per-cycle counters in §4.2, and `parent_ticket_id`. Nothing
else in this section needs building.

Worth naming as a known cost: `forge prune` clears artifact trees and `run.db`
grows without bound, step detail being the bulk of it. The reference run left
229 MB of passing output under one ticket. Retention matters before this document
adds another per-cycle row, not after.

---

## 11. Tunables

Existing keys this document depends on, at their shipped defaults:

| Key | Default | The draft's name |
| --- | --- | --- |
| `loop.maxAttempts` | `5` | — |
| `loop.retryCycles` | `2` | — |
| `loop.priorFailures` | `8` | part of the ledger |
| `loop.learnedLimit` | `12` | part of the ledger |
| `loop.executorTurns` | `4` | the ledger's `approach` |
| `loop.flatCycles` | `0` (off) | — |
| `loop.reviewWhenStuck` | `2` | `MIN_ROUNDS_BEFORE_CLASSIFY`, roughly |
| `loop.freezeTests` | `true` | — |
| `loop.toolchainContext` | `true` | — |
| `loop.ratifyPasses` | `2` | — |
| `loop.respecCriteria` | `false` | §8.1's freeze |
| `commands.format` | empty | — |

New keys, none of which should be written into the sample config ahead of their
code — a setting that names a feature nobody built reads as configured behaviour
and silently does nothing:

| Key | Proposed | Notes |
| --- | --- | --- |
| `loop.volumeThreshold` | `0` (off) | Distinct classes before a split is considered. Ships off, and §4.1 is why: on the only data available, no value separates a ticket that should be split from one that should not. |
| `loop.maxChildren` | `8` | Above this, escalate instead of split. |
| `loop.maxSplitDepth` | `2` | |
| `loop.maxRestarts` | `0` (off) | §5.1 — deferred. |
| `loop.participationWindow` | `20` | Tickets, for §8.2. Report only. |
| `loop.predictionMinSamples` | `30` | §9. |

Dropped from the draft's table: `REPEAT_THRESHOLD` (subsumed by
`reviewWhenStuck` and the flat-cycle count), `PROMOTE_THRESHOLD`,
`GRADUATE_THRESHOLD` and `MAX_ACTIVE_RULES` (all §7, withdrawn), and `MAX_ROUNDS`
(`maxAttempts` times `retryCycles` already bounds it, and the ladder is the real
brake).

**Every new brake ships off.** That is the lesson `flatCycles` paid for.

---

## 12. Order of work

The draft's order started with instrumentation that now exists. What replaced
it started with the thing nothing in CONVERGENCE.md had done. All six of the
steps that were worth doing before a live run of the new machinery are built;
what each one turned into is recorded beside it.

1. **Run a backlog.** ✅ Done, and documented in
   [CONTEXT-TOOLS.md](CONTEXT-TOOLS.md) rather than here: two runs of the
   `HANDBACK-DASHBOARD.md` backlog on 2026-09-04, the first carrying the
   pre-tools/post-tools comparison and the second landing both tickets. The
   ladder was not asked a question on either — nothing stalled — so what it
   establishes is a floor, not evidence about convergence.
2. **Replay the objection splitter** (§3.1 step 2). ✅ Done, and it answered
   the open question. `scripts/review_points.py` over every recorded
   `*-review.md` in this repository and the two `forge-evidence` trees: 62
   reviews, 24 rejections, **61 objections in them — 2.54 per rejection, and
   23 of 24 carrying more than one**. The largest carried twelve, one per
   acceptance criterion. So the volume axis does read review, and §3.1's
   framing was not about diagnostics all along.
3. **Record the volume counters** (§4.2). ✅ Built. `distinct_classes` and
   `new_classes` per cycle, on the ticket, in the log line beside the
   convergence one, and in the dashboard's evidence block. Review points count
   towards both, which is what step 2 earned. Nothing branches on either.
4. **Completion against the frozen criteria** (§8.1). ✅ Built as
   `Orchestrator._audit_criteria`, on the reviewer, once per ticket, and only
   for a ticket whose criteria actually moved — which at `respecCriteria:
   false` is none of them. The verdict is a note on the ticket and a log line;
   the ticket stays `done` either way.
5. **Ratification counters** (§8.2, §8.3). ✅ Built as `forge signoff`, over
   data already recorded. Participation per role, and the share of a ticket's
   later failures that named a file the roles already had in front of them.
   Report only.
6. **SPLIT** (§6). ✅ Built: `forge/split.py`, the invariant, the schema, the
   planner prompt, the parent as a gate. `loop.volumeThreshold` ships `0`, so
   nothing splits until somebody sets it — and §4.1 is the argument for why
   the person who sets it should look at their own numbers first. **Armed live
   at `2` on 2026-09-07** against the fixture written to be unsatisfiable, five
   times; §6.6 is what those runs found, which was four defects in the wiring
   around the mechanism and none in the mechanism.
7. **RESTART** (§5.1), only if a live run shows the ladder answering
   `winnable` on a ticket that then stays flat. **Not built**, and the
   condition has not been met: no run has reached that state.
8. **Plan-time prediction** (§9), once enough completed tickets exist across
   more than one repository. **Not built.** `predictionMinSamples` is not in
   the configuration either, because a key that names a feature nobody built
   reads as configured behaviour and silently does nothing.

Steps 2 through 5 were replays and reports. That was deliberate: this document
was written from reasoning about the loop, and invariant 9 exists to distrust
exactly that.

**What is still owed, and knowingly.** §3.2's run-scoped learnings tier is not
built: a split child inherits a snapshot of its parent's `learned` at creation,
so it does not rediscover what the parent established, but siblings still
cannot tell each other anything mid-run. §13's open question about that —
shared tier or per-child snapshot — is therefore answered by the cheaper half
and left open for the half that costs a new tier.

---

## 13. Open questions

Answered since the draft:

- ~~Can the reviewer produce stable objection IDs?~~ Moot for tool output —
  `classify()` produces them without asking a model. Open for review points, and
  §3.1 makes that a replay rather than a prompt change.
- ~~Does the executor state its approach before the diff?~~ Unnecessary.
  `executorTurns: 4` replays the actual prior reply as an `assistant` message.
- ~~Where do rules live relative to MemPalace?~~ The rule list is withdrawn.
  Toolchain configuration is read from the repository per prompt; conventions
  reach MemPalace through `_record_conventions`, gated by `memory.write` and
  screened by `_waiver_language`.
- ~~Is verification against the original criteria a model judgement or a test
  check?~~ A model judgement, reported and never enforced — §8.1.

- ~~Does a review rejection carry more than one countable objection?~~ Yes, and
  by a wide margin. §12 step 2's replay over 24 recorded rejections found 61
  objections in them — 2.54 each, 23 of 24 carrying more than one, the largest
  carrying twelve. The volume axis counts review points as well as tool
  classes.
- ~~Does a parent gate hold the baseline, or does each child take its own?~~
  Inherited, as §6.4 said, and now exercised: five live splits, and no child
  was ever blamed for red its parent left behind.

Still open:

- **Is the per-cycle distinct-class count a better discriminator than the
  lifetime count?** §4.1's table uses lifetime counts and they point the wrong
  way. The per-cycle series is what step 3 records, and it has never been looked
  at.
- **Do split children share one run-scoped learnings tier, or does each inherit a
  snapshot?** A snapshot, for now, because that is what `children_of` can do
  without a new tier in `Store.learn` — so a child reads what its *parent*
  established and not what a sibling is establishing beside it. The shared
  version §3.2 proposes is still unbuilt, and is still the reversible one.
- **What does `volumeThreshold` mean in a repository that is not this one?**
  Raised by arming it: a class carries a file, so the count tracks file surface
  as much as ticket size — 32 and 38 in a large two-language tree, never more
  than 2 in a four-file fixture. A number tuned in one repository is not
  transferable to another, and nothing yet normalises it.
