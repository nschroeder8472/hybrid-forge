# Handback: the work the loop returns, and how a human answers it

**Status:** specified, not built. Against `main` at `743f87c`.

The loop has six ways to stop working on a ticket and hand it back. It has no
way to be handed anything in return. Every one of those exits writes prose into
`blocked_note` and ends there — the only channels back in are the command line
and a text editor, and neither of them reaches the planner on the next respec.

This document specifies the return channel, and renames the one route whose
name says who decided rather than what was decided.

---

## 1. Where the loop hands work back

| Exit | Where | What it leaves behind |
| --- | --- | --- |
| Routed away from the executor | `_work_ticket` (`forge/loop.py:3331`) | `skipped`, a fixed note |
| Dependency not met | `_park_unmet` (`forge/loop.py:1797`) | `skipped`, the missing ids |
| Executor `BLOCKED:` | the build step | `blocked`, the executor's own sentence |
| Executor `IMPOSSIBLE:` | held, put to the planner | `blocked`, the claim and the planner's ruling |
| Ladder park | respec answers `impossible` | `blocked`, the named contradiction |
| Ratification, no majority | `ratify()` | `blocked`, every role's objection |
| Bug never reproduced | the reproduce step | `blocked`, the tester's explanation |

Seven, counting the two that share a status. Every one of them is a sentence
written to a human who has no way to write back.

### `skipped` means two unrelated things

`_work_ticket` and `_park_unmet` both set `TICKET_SKIPPED`. One means *a person
must write this code*; the other means *this is fine, it is just waiting on
PF-002*. They are distinguishable only by reading the prose in `blocked_note`,
which is why the dashboard shows them identically and why `forge retry --all`
treats them as one class.

That is the same defect as the route name, one level down: a status that
carries the fact and not the reason.

### The route has no exit

`forge retry --all` resets a `skipped` ticket to `pending`
(`forge/cli.py:1124`). `_work_ticket` then re-reads `route`, finds it is still
not `delegate`, and skips it again. Nothing in the codebase ever writes
`ticket.route` after ingest — every other reference is a read.

So a ticket a human has *already implemented by hand* has no transition back
into the run. Its dependents stay parked behind a ticket that is, in fact,
done.

---

## 2. What this document does not propose

**The gate stays.** A withheld ticket is not withheld for want of detail. It is
withheld because `neverDelegate` matched, or because the plan judged the work
unsafe to hand to an executor — authentication, migrations, cryptography, an
unresolved design question. A richer respec prompt does not change any of
that: a better-specified auth ticket still ends with a local model writing auth
code.

`_work_ticket` states the rule and the reason it is a gate rather than a
preference:

> Triage is a hard gate, not a preference. A claude-only ticket is one the plan
> judged unsafe to hand to the executor, and the loop is not entitled to
> overrule that because the backlog would otherwise stall.

That holds. What follows is about the *return* path, which is missing for all
seven exits and not only for this one.

---

## 3. `claude-only` becomes `withheld:<reason>`

The current value names the party that decided. It should name the objection,
because the objection is what a reader six weeks later needs and what the
dashboard has to render.

```
**Route:** delegate
**Route:** withheld:security
**Route:** withheld:concurrency
```

### Why the colon form rather than a second field

Every gate in the codebase is already written as `route != "delegate"` —
`_work_ticket` (`forge/loop.py:3334`), the status marker
(`forge/cli.py:559`), the `forge bug` notice (`forge/cli.py:1330`). A route of
`withheld:security` passes all three unchanged, and so does a `claude-only` row
recorded by an older run. The reason travels in the value, so nothing that
prints a route has to learn a second field to stay informative:

```
PF-004  blocked   (withheld: concurrency)
```

A separate `route_reason` column would have to be joined at every one of those
sites, and a row where the column was empty would read as delegable.

### The vocabulary

Closed, and drawn from the categories the delegation-protocol skill already
lists — this is not a new taxonomy, it is the existing prose made countable.

| Reason | The work touches |
| --- | --- |
| `security` | authentication, authorization, session handling, secrets |
| `concurrency` | locking, async ordering, shared mutable state |
| `interface` | public API surface, published interfaces, database migrations |
| `compliance` | cryptography, payment flows, anything with a compliance dimension |
| `performance` | a path where the fix depends on profiling judgment |
| `unresolved` | "what should this do?" is still genuinely open |
| `never-delegate` | a `neverDelegate` glob matched — the one case set mechanically |
| `unspecified` | the reason was not stated. Accepted, reported, never minted |

`never-delegate` is the one the harness can prove: `forge bug`
(`forge/cli.py:1283`) already computes it from a glob match, and that match is
a mechanical fact in the way an error code is. The rest are the planner's
judgement, and the delegation-protocol skill already requires the reason to be
stated — *"do not leave it implicit. A reader six weeks later cannot
reconstruct which category it fell under."* Today that sentence has nowhere to
land but prose.

`unspecified` exists so the parser never has to reject a ticket over the
reason. A route that fails to parse its reason is still a route that withholds
the ticket, and failing closed on the safe side is the whole point of the gate.
It is reported by `forge doctor` and by ingest, and it is the value a migrated
`claude-only` row takes.

### What changes

- `forge/ingest.py:66` — `_ROUTE` accepts `withheld(:reason)?` beside
  `delegate`, and continues to accept `claude-only`, mapping it to
  `withheld:unspecified` with a warning naming the ticket.
- `forge/ingest.py:654` — the ticket-file writer round-trips the new value; it
  already writes `ticket.route` verbatim.
- `forge/cli.py:559` — the status marker prints the reason.
- `forge/cli.py:1283` — `forge bug` sets `withheld:never-delegate`.
- `plugins/forge-spec/skills/spec-contract/SKILL.md:44` — the grammar line.
- `plugins/forge-spec/templates/spec.md:65` — the worked example.
- `plugins/forge-spec/skills/delegation-protocol/SKILL.md` — the category list
  becomes the vocabulary, with each bullet naming the value it maps to.

**No migration of stored rows.** `claude-only` gates correctly as it stands,
and rewriting historical rows to a reason nobody recorded would be inventing
evidence. Old runs read as `withheld:unspecified` at the display layer only.

---

## 4. The return channel

One mechanism, serving all seven exits. A note a human writes against a ticket,
which the planner reads on the next respec and the executor reads on the next
attempt.

### It is asynchronous, and this is the load-bearing property

The loop is unattended by design: a daemon, a `waiting_budget` state that wakes
itself, a run that survives the process being killed. A gate the loop *waits*
on puts a human back in the critical path of a system built not to have one —
and the failure mode is silent, because a run blocked on a person looks exactly
like a run that is thinking.

So: the note is picked up if it is there and nothing waits for it. A ticket
that never receives one behaves exactly as it does today. Everything below
follows from that.

### `human_note`, and why it is append-only

A `human_note` column on `tickets`, holding `[{"text", "at"}]`, written by one
method — `Store.advise` — and never by `update_ticket`.

This is `learned`'s rule for `learned`'s reason. `update_ticket` does not name
`original_spec` either: *a field any caller can shorten is not append-only*. A
note a respec cycle can quietly drop is a note the human will find missing
three cycles later with nothing recording that it was ever there.

Unlike `learned`, it is **not deduplicated on normalized text**. A person
repeating themselves is saying it is still true, and collapsing two identical
notes written a day apart discards the fact that the first one was not acted
on.

### Provenance, and what a human note may do that respec may not

Invariant 2: *provenance decides what is immutable, not position.* The criteria
ratchet refuses respec's additions because the party being judged does not
raise the bar it is judged against — and a human is not that party. A note
written by a person is plan-authored by the same rule that makes
`original_criteria` plan-authored.

So a human note **may add acceptance criteria**, and respec may not. That is
not an exception to the ratchet; it is `forge criteria --accept`
(`forge/cli.py:1992`) generalised from adopting a proposal the loop minted to
stating one the loop never thought of.

Two consequences worth naming before this is built:

- **Added criteria join `contract_criteria`.** They are protected against
  removal by a later revision, exactly like the plan's own. A person who adds a
  criterion at cycle 12 does not want cycle 13 negotiating it away.
- **A human note is not waiver-screened.** `_waiver_language`
  (`forge/respec.py:375`) exists to stop the loop excusing its own failures —
  *"the failing check does not count"* is not a learning. A person is entitled
  to say exactly that, because a person can be right about it and is
  accountable for it. The note is recorded as authored by a human and the
  screen does not apply. What the screen protects — `learned`, and everything
  that reaches memory through it — stays screened, so a waiver a human grants
  does not become a fact a future ticket reads as the project's own.

### Where it renders

Under its own droppable heading, like every other history block, in two
prompts:

- **`respec_prompt`** — which already takes `report`, `ruled_out`,
  `contradiction` and `stuck`. This is a fifth block into a shape built to
  carry evidence, not a new call.
- **the executor prompt** — above the ticket, framed the way `learned` is
  framed, with one difference stated in as many words: this was written by a
  person about this ticket, and it outranks what earlier attempts concluded.

Not the reviewer. The reason is `learned`'s: what the reviewer is shown is the
bar, and the bar changes through criteria or it does not change. A human who
wants the bar moved adds a criterion, which is a thing they can now do.

---

## 5. Discharging a withheld ticket

The transition that does not exist today: a person has written the code, and
the run should carry on.

Three actions, all of them a human's:

| Action | Effect |
| --- | --- |
| **Release** | `route` becomes `delegate` — the objection was answered, or the scope was narrowed until it no longer applies. The ticket requeues and runs normally. |
| **Discharge** | The work was done by hand. The ticket goes `done` against the tree as it now stands, and its dependents unblock. |
| **Advise** | A note, no state change. The only one that applies to all seven exits. |

**Discharge verifies before it believes.** A ticket marked done by hand is
still a ticket whose criteria have to hold, and the harness already knows how
to check that: run the verify commands, and record the result on the ticket the
way any other pass is recorded. A discharge against a red tree is refused, with
the failure shown — the point of a hand-implemented ticket is that a person did
the work the loop could not, not that the standard was suspended for it.

**Dependents unblock through machinery that exists.** `reopenStaleDependents`
already handles a completed ticket's dependents, and `_park_unmet` already
re-parks anything whose `needs` are still open. A discharged ticket is a
completed ticket as far as both are concerned.

**Release is recorded with its reason.** The withheld reason that was overruled
goes into the event log with the note that overruled it. A run where every
`security` route was released on the first cycle is a run whose triage was
theatre, and the counter should be readable afterwards — the same argument
§8.2 of [ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md) makes about a signer
who never blocks.

---

## 6. The UI surface

What exists: `GET /api/state`, `GET /api/events` (server-sent), and one
`POST /api/control` accepting four enumerated commands — `pause`, `resume`,
`run`, `stop` — written into a single row of the `control` table
(`forge/ui/server.py:39`). There is no per-ticket write path of any kind.

What is added, all per-ticket and all POST:

```
POST /api/ticket/<id>/note      {"text": "..."}          -> advise
POST /api/ticket/<id>/criterion {"text": "..."}          -> add to contract
POST /api/ticket/<id>/route     {"route": "delegate"}    -> release
POST /api/ticket/<id>/discharge {}                       -> verify, then done
```

Plus what the dashboard has to show before any of that is useful: a parked
ticket's `blocked_note`, its withheld reason, its `learned` entries, and its
repeated failure classes — the same evidence the ladder puts in front of the
reviewer, in front of the person instead. A note written without the evidence
is a guess, and the current dashboard renders the backlog, the log, the steps
and token usage, and none of that.

### This is a larger surface than four enum values

`is_exposed` and `exposure_warning` (`forge/ui/server.py:290`) exist because
binding the dashboard off localhost is already a risk worth warning about. Free
text that lands in an executor prompt is a different order of exposure: it is
an instruction channel into a model that writes files, gated today by nothing
but the scope gate downstream of it.

Three requirements, none of them optional:

- **Localhost by default stays the default**, and a note endpoint refuses to
  serve at all on an exposed bind unless the operator has said so explicitly —
  a stronger position than the warning that covers the read endpoints.
- **A note is data, never protocol.** It is rendered under its own heading and
  never concatenated into a system message, for the same reason
  `strip_prompt_echo` (`forge/prompts.py`) exists: text from outside the
  harness must not be able to imitate the harness.
- **Every write is logged as an event** with its author-side origin, so the
  step log shows a human intervened and when. A run whose outcome was changed
  by a note nobody can find afterwards is a run whose record is wrong.

---

## 7. Order of work

Each stage is useful on its own and none of the later ones is required for the
earlier ones to ship.

1. **Split `skipped`.** A withheld ticket and a ticket waiting on `PF-002` stop
   sharing a status. Nothing else in this document reads correctly on a
   dashboard that cannot tell them apart, and it is the smallest change here.
2. **The route rename.** Parser, writer, printers, skill, template. Old values
   keep gating. No stored row is rewritten.
3. **`human_note` and `Store.advise`**, rendered into `respec_prompt` and the
   executor prompt. Written by the CLI first — `forge advise <id> "..."` — so
   the mechanism is exercised before it has a web endpoint.
4. **Human-authored criteria**, joining `contract_criteria`. This is
   `forge criteria --accept` widened from adopting a minted proposal to stating
   a new one.
5. **Release and discharge**, with discharge verifying before it believes.
6. **The dashboard**, read side first: a parked ticket's note, reason,
   learnings and repeated classes, which is what makes the write side worth
   having.
7. **The write endpoints**, behind the localhost rule.

Stage 3 before stage 7 is the point of the ordering. A note that reaches the
planner through a command-line call proves the prompt plumbing works while the
input is still trusted; adding a network surface to a mechanism that has never
been exercised puts two unproven things in the same change.

---

## 8. Open questions

- **Does an unanswered note expire?** A note written at cycle 3 is still in the
  prompt at cycle 30, and the case against `learned`'s dedupe applies in
  reverse: advice that has been carried through twenty cycles without changing
  anything is probably advice the loop cannot act on. Counting is cheap and the
  answer is unknown; count first.
- **Should a withheld reason be shown to the roles at all?** A ticket routed
  `withheld:security` is never delegated, so the reason reaches no prompt. But
  a *neighbouring* ticket in the same scope might benefit from knowing why its
  sibling was withheld — or might be led by it into treating the ticket it does
  have as more dangerous than it is. No evidence either way.
- **Does discharge belong to the run at all?** The alternative is that a
  hand-implemented ticket is committed by the person and picked up as ordinary
  history on the next baseline, with no state transition. Simpler, and it loses
  the record of which ticket the work satisfied.
- **What happens to a note on a ticket that is then split?** §6 of
  [ADAPTIVE-TICKET-LOOP.md](ADAPTIVE-TICKET-LOOP.md) proposes children covering
  a parent's criteria. A human note is not a criterion and has no coverage
  rule, so it either goes to every child or to none. Shared is the reversible
  answer, and it is the same question that document leaves open about
  run-scoped learnings.
