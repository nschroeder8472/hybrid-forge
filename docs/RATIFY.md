# Ratification — design spec

**Status:** built. `loop.ratifyPasses`, default `2`. `0` turns it off.

A ticket enters the loop as a contract nobody has agreed to. The plan was
written by one party — a human, or the planner at `forge ingest` — and the four
roles that have to live with it first see it at the moment they are being
measured against it. The executor discovers the scope is too narrow on attempt
one. The tester discovers a criterion it cannot express as an assertion while
writing the test that has to encode it. The reviewer discovers it disagrees
with the bar on the diff that was built to the old one, and rejects work that
did exactly what it was asked.

Every one of those is the same failure: **disagreement about the contract,
discovered after the work.** Ratification moves it before.

---

## What it is

Before a ticket's first attempt, each configured role is shown the ticket and
asked one question: *can you do your part of this, as written?* A role answers
by signing off, or by objecting with specific points and suggestions. The
planner turns the objections into a revision, and the pass repeats up to
`loop.ratifyPasses` times.

Nothing is built until the ticket is ratified, and what happens when the roles
cannot agree is decided by rule rather than by whoever spoke last.

```
ingest ─▶ RATIFY ─▶ BUILD ─▶ APPLY ─▶ TESTS ─▶ VERIFY ─▶ REVIEW ─▶ …
           │
           ├─ pass 1: four sign-off calls, then one planner revision
           ├─ pass 2: four sign-off calls, then one planner revision
           └─ pass N: four sign-off calls — no revision after the last vote
```

The revision never comes after the final vote. A ticket that shipped text
nobody had voted on would have the same defect the whole mechanism exists to
remove.

---

## Who decides

Two rules, and they do not conflict because they answer different questions.

**The planner has final say over the text.** It is the only role that writes a
revision. The other three propose; the planner decides what the ticket says.
That keeps authorship in one place — a contract edited by four parties in turn
is how a spec ends up asserting the opposite of what its author wrote, which
`_merge_criteria` already exists to prevent one level down.

**A majority decides whether it ships.** After the last pass:

| signed off | outcome |
|---|---|
| all four | `unanimous` — proceeds |
| a majority (3 of 4) | `majority` — proceeds, dissent recorded |
| planner and at least one other | `split` — proceeds, dissent recorded |
| anything less | `blocked` — parked for a human, nothing built |

So a reviewer that will not sign off does not stall a ticket the other three
agree on, and a planner outvoted three to one does not get to overrule them.
Below the floor the run does not guess: the ticket parks with every objection
recorded, and the rest of the backlog carries on.

---

## What a ratify pass may change

`spec`, `context`, `allowed_files`, `reference_files` — and, unlike respec,
**`criteria`**.

This is the one place in the loop where the bar may move, and it is deliberate.
Respec refuses new criteria because of *when* it runs: on a ticket that has
just exhausted its attempts, where raising the bar cannot serve the purpose of
the revision, and where the party being judged is the one asking. Ratify runs
before any attempt exists. There is no failure to rationalise, no attempt to
rescue, and no evidence yet for anyone to argue from — only the question of
whether the contract is the right one.

Once ratified, the settled version becomes the anchor:

- `ratified_spec` and `ratified_criteria` are written on the ticket.
- Respec's ratchet reads those in preference to `original_*`, so from that
  moment the ratified criteria are protected exactly as a human's are.
- `original_spec` and `original_criteria` still hold what was ingested, so
  `Ticket.drifted` keeps reporting drift against what a person actually wrote.

Guardrails that survive ratification unchanged: a scope that names a
`neverDelegate` path, a language with no runner, files no workspace owns, and a
ticket spanning two workspaces are all re-checked *after* the revision, because
a ratify pass can widen scope into any of them. A ticket that widens itself
into an uncovered language parks, the same as one that arrived that way.

---

## Suggestions and responses stay on the ticket

Each vote and each planner response is appended to `ratify_notes`, and the
ticket file gets a `## Ratification` section listing them: who objected, to
what, and what the planner did about it.

That record is not bookkeeping. It is carried into `build`, `tests` and
`review` as a droppable context block, so the role that asked for something
sees whether it got it, and the role that objected and was overruled sees the
reason rather than re-raising the point on the diff. It is the closest thing
the loop has to the memory of an argument.

---

## Learnings carry to the next ticket

When the next ticket ratifies, the prompt carries a digest of what earlier
tickets in the same run settled — the objections that were raised, and how the
planner answered them. Read out of `ratify_notes` in the database rather than
held in the daemon, so a run resumed after a reboot keeps them.

The point is that the second ticket should not re-litigate the first. A tester
that asked for a criterion to be made measurable on TT-001 should see, on
TT-002, that it asked and what happened — and a planner writing TT-002's
revision should see the same.

---

## Cost

Ratification is `roles × passes` model calls per ticket, plus up to
`passes - 1` planner revisions. At the default four roles and two passes that
is ten calls before a line is written, one of them on the reviewer — which is
roughly 100% of the money on a hybrid run.

That is why the default is `0`. The knob is the number of passes, and `0` means
the feature is off entirely: no calls, no steps, no change to any existing run.

---

## The honest weakness

**A reviewer that helped write the ticket is not independent of it.** The loop's
strongest structural rule is that the party being judged does not write the
standard — the executor does not write its own tests, respec may not add
criteria, and a reviewer does not inherit the executor's turns. Ratification
knowingly weakens a version of that rule: the reviewer signs off on the contract
it will later judge work against.

The trade was made deliberately. The failure it addresses — review rejecting a
diff over a bar the reviewer never agreed to, on a ticket that did what it was
told — is one the loop has actually produced, repeatedly. The failure it risks
is a reviewer anchored on a contract it helped write. Both are real; only the
first has been observed.

**Still no evidence about ratification itself, and it is now on by default.**
That tension is deliberate and worth stating plainly. Every claim above about
what ratification improves is still derived from reading the loop rather than
from watching it run; Section 9 of
[LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) exists to distrust exactly that.

What changed is not the evidence for this pass, it is the evidence for the
problem. The Puzzle-Path run of 2026-08-22/23 handed the loop two tickets no
implementation could satisfy — one whose spec described an algorithm its own
criteria contradicted, one demanding a count of 13 from a fixture holding 15 —
and with `ratifyPasses: 0` the loop had no moment at which anyone was asked
whether the ticket was buildable. They cost 650 attempts, 16.6M tokens, and
about 16 hours. See [CONVERGENCE.md](CONVERGENCE.md).

Eight calls per ticket against that is a defensible default even without
knowing how well the pass performs, because the alternative is not "cheaper",
it is "no check at all". The evidence still owed is the one this section always
asked for: the same backlog run twice, `ratifyPasses: 0` and `ratifyPasses: 2`,
compared. Until that exists, treat the default as a judgement about the cost of
the failure rather than a measurement of the fix.
