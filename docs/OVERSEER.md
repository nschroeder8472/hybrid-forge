# The overseer

A seat that watches the loop rather than working in it, runs on a frontier
model, and is allowed to do the things the loop is structurally forbidden from
doing to itself.

This is a specification, not a description. Nothing here is built.

## What made the case for it

The platformer run of 2026-09-10/11 — fifteen tickets, four local seats, about
8M tokens — stopped or went wrong eleven times. A person intervened at each
one. **Every single intervention was an action no role in the loop is permitted
to take**, and in every case the loop had already diagnosed the problem
correctly and stopped rather than guess.

That is the finding. The loop is not short of judgement. It is short of
*authority*, and deliberately so: each restriction below is correct on its own
and none of them can be relaxed without losing something.

| What happened | Who diagnosed it | Why the loop could not fix it |
|---|---|---|
| PT-011 could not add a `Spiky` enemy: PT-009's test asserted every placed enemy is a `Walker`, and that file was not in PT-011's scope. Five attempts failed identically. | The executor, naming the file and line; then respec, reporting the ticket impossible; then the convergence brake, stopping the run | A planner that may rewrite another ticket's tests is a scope discipline with a hole in it |
| PT-006 criterion 8 pinned neither `dt_ms` nor the held input. PT-007 criterion 7 named "a level 60 tiles wide" when every level is 48 | Executor and tester, independently, four times across three passes | No role owns `SPEC.md`. They may object to a criterion; they may not edit the document a person wrote |
| `draw_count` returned floats where `docs/ABI.md` says commands, so the shell read six times the buffer and painted heap as draw commands | Nothing. Two model reviews passed it; 91 tests pass; the export only runs in a browser | The contract and the module that violates it belong to different tickets, and no criterion spans them |
| A reviewer rejected correct code because it read "a dead world" as state 3 when the ABI says 1; a planner claimed a fall took longer than 1000 ms when the arithmetic says 457; a reviewer claimed no API could set `player.y` when the field is public | Nobody. The executor refused one of them and was right | A role's claim about the tree is not checked against the tree by anything |
| 14 of one model's 40 sign-off votes were 8,217 completion tokens and 49 bytes of output — reasoning exhausted, verdict never written | The husk detector, after it was built mid-run | No role can reassign a seat to a different model |
| `enemy::place` returns nothing on every shipped level, because a ratify revision replaced "two tiles above" with `row + 1` / `row + 2` | The capture reviewer, from a screenshot showing no enemies | The executor implemented the spec it was given; the tests encode the same spec; no criterion asks for enemies on shipped levels |

Six kinds of failure, one run. What unites them is not difficulty — most were
diagnosed within one step of occurring — but that the fix lives *above* the
ticket: in the authored spec, in the configuration, in a document no ticket
owns, in the choice of model, or in a second ticket's file.

## What it is

The overseer is a Claude seat, started once per run, that:

- **reads everything the loop writes** — `run.db`, artifacts, captures, the
  tree, git history — and nothing it produces is a model's summary of those;
- **verifies claims against the tree** rather than weighing them as opinions;
- **acts on the project around the loop**, within an authority the operator
  sets;
- **never writes application code, never votes, and never reviews a diff.**

### It is not a fifth role

`loop.ratifyPasses` counts sign-off over `ROLES`, and a majority is defined
against that count. Adding a voting seat changes what a majority is, which is
why `viewRole` names one of the four rather than adding one. The overseer sits
outside that count entirely: it never casts a vote, and a ticket's fate is
settled by the same four seats as today.

It also never reviews. A reviewer judges a diff against criteria; the overseer
judges whether the *criteria, the scope, the configuration and the contract*
are fit for what the run is trying to do. Those are different questions and
collapsing them would make the overseer a second reviewer with more authority
than the first.

### Why Claude only

Three reasons, in descending order of how well tonight's run supports them.

**It must be right about the tree.** Its whole function is checking a claim
against a file, an arithmetic result, or a document. Across this run the local
seats produced four confident claims about the tree that were false on
inspection — the fall time, the missing setter, and the dead-versus-game-over
reading twice — and each cost attempts. The Claude seat produced none, and
found three defects nothing else did. One run, two models, so this is evidence
rather than proof; it is also the only evidence there is.

**It must read pictures.** Two of the six findings above came out of a
screenshot. A seat that cannot see them cannot do half the job.

**It holds the highest-consequence write surface in the project** — the spec, the
config, the run's control channel. That is a seat to spend money on.

`overseer.model` must name a model whose provider reports `supports_images`,
and startup refuses otherwise rather than degrading quietly, in the same way
`roleNeeds` refuses a seat whose model cannot do its job.

## Authority

Three tiers. The operator sets the ceiling; the overseer never raises it.

**`read`** — the default, and always available. It looks, records findings, and
changes nothing. A run started this way behaves exactly as it does today, plus
a report.

**`propose`** — it writes findings as proposed actions, each with the evidence
and the exact edit, and applies none of them. A person runs `forge oversee
--apply <id>`. This is the tier a first adopter should use, and the tier a run
should fall back to whenever the overseer's last finding was wrong.

**`act`** — it may take the actions on an allowlist, and only those:

| Action | Why it needs an overseer | Reversible by |
|---|---|---|
| widen one ticket's `allowed_files` | respec correctly refuses to reach into another ticket | `git revert` on the spec; the ticket re-ratifies |
| edit an acceptance criterion in `SPEC.md` and the ticket | no role owns the authored document | `git revert` |
| stop a run | the brake stops cycles it can see; not ones it cannot | `forge go` |
| file a bug ticket | the loop cannot notice what no criterion asks for | delete the ticket |
| reassign a role's model | nothing in the loop measures a seat's own quality | edit config |

Never, at any tier: delete a run or its artifacts, force-push, rewrite history,
change provider credentials, edit another ticket's *source*, or modify a
criterion in the direction of what the models managed to produce.

That last one is the rule most worth stating twice. **A criterion may be made
more precise and may never be made easier.** Respec already carries this rule —
*"a criterion that stops asserting a value the plan pinned has dropped it
however well the new wording reads"* — and the overseer is far more dangerous
under it, because it can edit the source document rather than a copy. Every
criterion edit records the old text, the new text, and which role's objection
motivated it. An edit motivated by no objection is refused.

## When it wakes

Not continuously. It is woken by events, each of which is a point where the
loop has stopped being able to help itself:

- a run reaches a terminal state other than `done`;
- the convergence brake fires — flat cycles, identical evidence, or an
  exhausted retry budget;
- a respec returns `impossible`;
- a ticket exhausts its attempts;
- a vote is withdrawn as a husk, or a role refuses without naming a reason
  twice;
- a capture is produced *and* the ticket's review rejected (a passing review on
  a rendering has already been read by a model that can see);
- the operator asks, with `forge oversee --now`.

Each wake is one Claude call with the run's state and the artifacts for the
event that caused it. The events above fired 23 times in the platformer run.
At the per-call costs measured there — `$0.145` a sign-off vote, `$0.46` a
capture review, both around 90% cached — that is a few dollars a run, against
the roughly eight hours of human attention the same run consumed.

A wake produces a finding or explicit silence. Silence is recorded too, because
a seat that never says "nothing here" is one whose findings cannot be counted.

## A finding

The unit of output, stored in `run.db` and rendered in the dashboard:

```
finding    what is wrong, in one sentence
evidence   the artifact, file:line, command output or image it rests on
verified   the check that was run, and its result
proposal   the exact edit, as a diff or a config change
acted      applied | proposed | refused-by-authority
outcome    set later: whether the run got past the thing it was stuck on
```

`verified` is the field that separates this from a fifth opinion. A finding
whose evidence is another model's assertion, rather than a file read or a
command run, is refused before it is written. Tonight's false claims would each
have failed that gate: *"the player falls beyond the level's pixel height
before 1000 ms"* dies against a nine-line simulation of the fall.

`outcome` is what makes the seat measurable. A run where the overseer acted
four times and the backlog still parked is a run where it was expensive and
wrong, and the record should say so without anyone having to reconstruct it.

## Where it lives

A third plugin beside `forge-spec` and `forge-setup`:

```
plugins/forge-oversee/
  .claude-plugin/plugin.json
  commands/forge-oversee.md      # start, --now, --apply <id>, --findings
  skills/overseer-protocol/      # the authority model and the refusal rules
  skills/claim-verification/     # how to check a model's claim against a tree
  skills/run-forensics/          # reading run.db, artifacts and captures
```

`claim-verification` is the skill that carries the most weight, and it is
mostly a list of things that turned out to be checkable: an arithmetic claim
against a ten-line simulation, an "there is no API for X" against `grep`, a
state-machine claim against the ABI table, a rendering claim against a pixel
count and a buffer dump.

### What forge must add

- `overseer.enabled`, `overseer.model`, `overseer.authority` in config, all
  off and empty by default, so every existing project is unchanged;
- a `findings` table and its dashboard panel;
- wake events emitted at the seven points above;
- `forge oversee` in the CLI: `--now`, `--apply`, `--findings`, `--authority`;
- one refusal: starting with `authority: act` and no `overseer.model` that can
  see is an error at startup, not a downgrade.

## How it fails

Four ways, named here so the build can be judged against them.

**It becomes a fifth opinion.** The guard is `verified`: findings rest on the
tree, not on taste. An overseer that rejects a ticket because it would have
written it differently is doing the planner's job with none of the planner's
accountability.

**It edits the spec toward what the models can manage.** The criterion rule
above, plus the requirement that every edit names the objection that motivated
it. If no role objected, there is nothing to fix.

**It hides a bad seat by working around it.** Every finding names the seat
whose output caused it, and the report counts them. Four false claims from one
seat should end as a recommendation to change that seat — which is what
happened here, by hand — not as four quiet repairs.

**It is confidently wrong.** It will be. During this run the human overseer
mis-predicted which run `forge go` would select, and first attributed two
identical screenshots to capture timing when the cause was an edge-triggered
jump that a held input can never fire. Both were caught by checking. Neither
would have been caught by thinking harder. This is why `propose` is the
recommended tier, why `verified` is mandatory, and why `outcome` is recorded
against every finding rather than assumed from the proposal.

## What this does not answer

Whether a seat with this authority is worth its cost on a run that goes well.
The platformer run needed eleven interventions in fifteen tickets; the
sample-project run of 2026-09-10 needed none in nine, across three replications.
The honest position is that the overseer earns its money on hard backlogs
against local models, and that a run which lands first try should barely wake
it — which is a prediction the `outcome` field exists to test.
