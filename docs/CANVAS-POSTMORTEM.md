# Post-mortem: a spec that reached the loop with three fifths of its criteria

Derived from the `Puzzle-Path` canvas backlog, runs 2 through 5 of 2026-08-30
(`.hybridforge/run.db`). Run 2: five tickets, **one blocked, four skipped, zero
attempts, no file written**, 18m48s. Run 3, after the fixes in §1–§4: five
tickets, **5/5 `done`**, every one ratified unanimous, 120 minutes. Run 4, the
defects found reviewing run 3: three of four landed in one attempt each and one
parked, for the cause in §8. Run 5, that ticket respecified: **one attempt, 13
model calls, 15 minutes**.

The spec was correct. Run 2 failed because the parser that read it dropped
three fifths of what it said, and then the loop repaired the damage it had
caused — into a ticket no implementation could satisfy.

Run 3 landed green and was then reviewed by hand. It had shipped a defect that
makes every acceptance criterion in the backlog false on any machine but the one
that verified it. That half is §5 onward, and it is the more uncomfortable half.

---

## 1. The parser read one physical line per bullet

`ingest._bullets` iterated `text.splitlines()` and kept the lines starting `- `.
A markdown list item may run over as many lines as the author's column limit
demands, and every continuation line was silently discarded.

The spec wrapped at 95 columns. **31 of its 51 acceptance criteria arrived
truncated**, along with both halves of several file lists:

```
PF-011  9 of 15 truncated      PF-012  9 of 13      PF-013  6 of 7
PF-014  2 of 9                 PF-015  5 of 7
```

What the author wrote:

```
- `screenToCell` with `origin {x:0,y:0}`, `scale 16` returns `{x:-1,y:-1}` for the point
  `{x:-1,y:-1}`.
```

What the ticket held: `…returns {x:-1,y:-1} for the point`. A grammatical
fragment that still reads like a criterion, missing the only thing it names.

**Why the previous backlog was green.** The bug was always there. The core spec
that ran clean two days earlier had **0** wrapped bullets out of 110; the canvas
spec had **43** out of 101. The only thing that changed was the author's line
width.

## 2. The loop repaired the damage it had caused

The tester diagnosed it exactly, on the first sign-off pass:

> the round-trip criterion is missing the viewport/scale/origin, the two
> `screenToCell` point criteria are missing the point, the second `visibleCells`
> case is missing `size`, the first `fit` case is missing the expected origin,
> and the final command criterion is missing the expected exit code.

Six holes named, and all six are truncation points. Nothing acted on that
reading. The planner filled the holes instead — inventing `point {x:0,y:0}` for
the criterion whose point had been dropped, which contradicts both `Math.floor`
and the round-trip criterion. The executor had signed off on pass 1; on pass 2
it refused, correctly, and the ticket parked at 1 of 4 with four dependents
skipped.

Respec also rewrote the spec body and **flipped the coordinate convention**,
from "with `origin` subtracted" to `cell.x * scale + origin.x` — contradicting a
criterion that had survived intact and that nobody had objected to.

**Decisions protected nothing.** `plan_decisions` was line-oriented too, so a
wrapped `Decision:` sentence shattered into `Decision:`, `else.` and `The base`.
Every fragment fell under `respec._DECISION_FLOOR`, so the ratchet enforced
none of them while reporting 85 protected decisions. Rejoined, the same document
has 40, of which 37 are long enough to enforce.

## 3. The sign-off parser read the model's thinking as its answer

Two of the blocking objections that parked PF-011 were `...` and
`(one line each, or NONE)` — the prompt's own format placeholders, quoted back
inside the executor's reasoning while it was still deciding what to say. The
stored suggestions list contained a literal `</think>`.

Depending on the chat template, llama.cpp returns the reasoning block inline in
`content` rather than in `reasoning_content`, and nothing stripped it.
`parse_ratify` scanned the whole reply, found the rehearsed `BLOCKING:` heading,
and collected what followed.

## 4. One role had signed off on nothing, for sixteen passes

Across runs 1 and 2 the reviewer blocked **11 of 16** sign-off passes and signed
zero. Always the same objection:

> Cannot verify the numeric acceptance criteria without seeing the actual diff.

`RATIFY_SIGNOFF_SYSTEM` bans that objection in a dedicated paragraph. The model
raised it anyway, because the question invited it: `RATIFY_QUESTIONS["reviewer"]`
asked it to name any criterion it *"could not settle by reading a change — one
needing the code run"*, and every ticket carried "lint, typecheck and test all
exit 0", which genuinely does need the code run.

`resolve` counts votes, so a role that can never sign turns a four-way pass into
a three-way one and puts `unanimous` permanently out of reach. After the
question was rewritten to say the harness settles those, the reviewer signed
**8 of 8**.

---

## 5. Run 3 was green and had deleted the project's dependencies

The executor rewrote `package.json` to add two scripts and returned it without
its `devDependencies` — all six — plus `version`, `private` and `description`.

Every check passed, because **verification runs where the packages are already
installed**. Reproduced against the produced manifest and the real lockfile:

```
$ npm ci
up to date, audited 1 package in 572ms
--- installed? ---   (no node_modules)   (no .bin)
```

So on a clean checkout there is no vitest, no tsc, no eslint, and the criterion
every ticket carried — "all four exit 0" — is false. It was true only on the
machine that verified it. This is now [LOOP-INVARIANTS §18](LOOP-INVARIANTS.md).

## 6. A test suite that ran the build system, and itself

Handed "npm run test exits 0" as a criterion on all five tickets, the tester did
its job: it wrote tests that shell out and run those commands, including
`npm run test` invoking itself, guarded by an environment variable so the nested
run skips the block that spawned it.

It worked. It also cost **5.3 of the suite's 5.65 seconds**, wrote `dist/` into
the tree on every run, and made the assertion vacuous — the nested suite is not
the suite that runs. Removing those five tests took the suite to **0.56s**.

The criterion was settled by the harness before anyone read it. The fix was to
say so in `TESTER_SYSTEM`, and to stop writing the criterion.

## 7. Two defects the criteria were shaped not to catch

**The ruler labelled the wrong columns.** `renderRuler` positioned every label
at `cx * scale + scale/2` — a world coordinate, with `view.origin` never
subtracted. At the viewport `fit` produces by default, column 0's label sits 95
pixels from column 0. Six criteria covered it, all about how many labels are
emitted and which indices they carry; **none said where a label goes.**

**The ruler was never drawn.** Nothing imported it. PF-015's spec said "the
shell paints it", the shell's file belonged to PF-014 which had already landed,
and the sentence naming the integration sat in the only ticket that could not
act on it.

Also found: the zoom readout showed `1500%` (`scale * 100`, where the spec said
a percentage of scale 32), and the page's file input was `hidden` with nothing
to open it — the editor could not load a level at all.

---

## Landing order

| | Change | Would it have stopped run 2 | Status |
|---|---|---|---|
| 1 | `_bullets` rejoins indented continuations | Yes, at ingest | **done** — `ingest._logical_lines`; a flush-left continuation is still dropped, deliberately, and `check_spec.py` warns |
| 2 | `plan_decisions` reads logical lines; `_sentences` respects code spans | Not run 2, but the ratchet was reporting protection it did not have | **done** |
| 3 | Strip reasoning at the provider boundary | Yes — two of the four objections | **done** — `providers.base.strip_reasoning` |
| 4 | `parse_ratify` reads only the last `SIGNOFF:`, dedupes points | Yes | **done** |
| 5 | Reviewer's sign-off question names the harness-settled criteria | Not run 2 alone; it removed a permanent no | **done** — reviewer went 0/16 to 8/8 |
| 6 | Refuse an attempt that drops a manifest's dependencies | No — run 3 | **done** — `forge/manifests.py`, LOOP-INVARIANTS §18 |
| 7 | Tell the tester the harness runs the project's commands | No — run 3 | **done** — `TESTER_SYSTEM` |
| 8 | Prompts and plugin docs state what to do, not what to avoid | No | **done** — ~65 rewrites across `forge/` and `plugins/` |
| 9 | `check_spec.py` reports harness criteria, retired routes, entry points, unread products | Yes, before ingest | **done** |
| 10 | Report a delegated non-bug ticket that lists no test file | No — run 4 | **done** — `ingest.untestable_scope`, at `forge ingest` and in `check_spec.py`; the authoring half is in `spec-contract` |

Items 1 and 3–4 are each independently sufficient to have turned run 2 into a
run that built something.

---

## Postscript: the check that existed and was not run

`plugins/forge-spec/scripts/check_spec.py` has had a `wrapped_bullets` warning
since it was written, and the `spec-contract` skill said **"One bullet, one
line"** in bold. The spec was authored without running `/forge-spec-check`.

So the most expensive failure here was not a missing guard. It was a guard
nobody was required to run, documenting a constraint the author did not read.
`/forge-spec` now states that the check is not optional and names this run as
what skipping it costs — which is a weaker fix than making the parser correct,
and is why the parser was fixed first.

The defects in §5–§7 were carried as a bug backlog rather than patched by hand,
so the loop's own reproduce-before-fix path ran against faults whose cause was
known. Three of the four landed in one attempt each. The fourth parked, for a
cause worth recording on its own.

---

## 8. A ticket that named no test file had nowhere to put its tests

PB-004's `Allowed files` were `main.ts` and `index.html`. The tester writes into
a path the ticket designates; with none designated, `_test_target` invented one
beside the workspace — outside the ticket's own scope. What the tester wrote
there failed `typecheck` on DOM properties the project's config does not define,
and the executor is refused any test file, so the one role that could repair it
was turned away on every attempt until the ticket parked.

The sign-off pass caught it. Twice:

```
p1 tester    NO  Add a writable test file … or otherwise allow a test file
p2 executor  NO  Add a writable test file …
p2 tester    NO  Add a writable test file …
```

`resolve` returned `split` — planner and reviewer signed, so the planner plus
one other carried it — and the ticket shipped over the objections of the two
roles that had to do the work. The contrast with its neighbours is exact:

```
PB-001 bug      done     att=1   test file in scope: yes
PB-002 bug      done     att=1   test file in scope: yes
PB-003 bug      done     att=1   test file in scope: yes
PB-004 feature  blocked  att=3   test file in scope: no
```

A `bug` ticket is exempt: its reproduction goes to a derived path granted as
extra scope, which is why the three beside it never met this.

`ingest.untestable_scope` now reports a delegated non-bug ticket that writes
testable source and lists no test file, at `forge ingest` and in
`/forge-spec-check`. Run against the two specs above it flags PB-004 and nothing
else.

**The second half of the cause was not a loop defect.** PB-004's criteria
asserted which imports `main.ts` used and which functions it called — and
`main.ts` is the only file allowed to touch `document`, so nothing in the suite
can import it without a DOM, and grepping it as text is the assertion style that
breaks on the next edit. Those criteria were never settleable by any test. No
guard catches that; it is the authoring rule now written into `spec-contract`
under *Criteria for the parts a suite cannot reach*.

Respecified as PB-005 — the test file in scope, the criteria reduced to markup
assertions the existing suite already proves are testable, and the `main.ts`
wiring left to review with the five things to check named in the ticket's Notes
— the same work landed in **one attempt, 13 model calls, 15 minutes**. Every one
of those five was correct in the diff.
