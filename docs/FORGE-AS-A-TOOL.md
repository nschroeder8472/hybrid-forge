# Forge as a tool — design (retired)

> **Retired.** Host mode was built, benchmarked and found to cost about 3x
> Opus alone for the same result; see [VERDICT.md](VERDICT.md). The code is kept
> at the tag `archive/host-mode`. What replaced it is
> [THE-FORGE.md](THE-FORGE.md).

A restructuring of hybrid-forge from a daemon that drives models into a tool a
frontier model drives. The standards stay. The driver changes.

Built on branch `feat/forge-as-a-tool`, 2026-11-02, with every decision below
confirmed. Where the build differs from the design, *Status* at the end says
so and why.

---

## Why

The loop does what it set out to do: it makes cheap models produce work that
holds up, by refusing every shortcut a model takes when nothing checks it. What
the recent runs show is *where* it stops being able to help itself, and the
answer is consistent:

- **The platformer run (2026-09-10/11)** stopped or went wrong eleven times in
  fifteen tickets. Every intervention was an action no role in the loop is
  permitted to take — widening another ticket's scope, editing a criterion in
  the authored spec, reassigning a seat, filing a bug nothing asked for. The
  loop had diagnosed each one correctly and stopped. [OVERSEER.md](OVERSEER.md)
- **The lock ticket (2026-11-02)** took four runs. A local planner raised the
  same false geometry objection in every guarded vote, and no spec text moved
  it. One config line seating Claude as planner ratified it unanimously and
  landed it on the first attempt, for $0.29. [CREATIVE-TOOLS.md](CREATIVE-TOOLS.md)
- **Every run that went well had a Claude session watching it**, reading
  `run.db`, replaying artifacts, running `forge advise` and `forge release`, and
  editing specs between cycles. That session is the overseer the spec describes,
  doing the job by hand through a CLI built for a human.

So the loop's judgement roles — planning, respec, arbitration of objections,
oversight — are where local models fail and where a frontier model is already
doing the work, informally. Its mechanical roles — the gates in
[LOOP-INVARIANTS.md](LOOP-INVARIANTS.md) — are where the value is, and none of
them need a model at all. And its bulk roles — executor, tester — are where a
cheap model earns its keep.

The restructure follows that split:

| Kind of work | Today | After |
|---|---|---|
| Judgement: plan, scope, criteria, respec, arbitration, oversight | local planner seat, sign-off majority, a human or Claude watching | **the host model**, through tools |
| Mechanical gates: scope, parse, imports, manifests, attribution, baseline, canary, quarantine, ratchets | the daemon | **the tool**, unchanged, non-negotiable |
| Bulk generation: implementation, tests, renders | executor / tester seats | **delegate seats**, unchanged |
| Independent review | reviewer seat | **a delegate seat**, plus the host, under rules |

The host is whatever large model has the tool attached: Claude Code, the Claude
API with tool use, or anything that speaks MCP.

---

## What does not change

These are the spirit of the project. Each is kept, and each is enforced by the
tool rather than requested of the host.

1. **The executor never decides scope.** Every edit a delegate makes is checked
   against `allowed_files` before it touches disk. Paths outside the project
   root are refused. `neverDelegate` is enforced at contract time.
2. **The executor never authors its own acceptance criteria.** The tester
   encodes the contract's criteria, and a test file is never granted to the
   executor by any route (§6).
3. **The party that benefits does not rule.** Now applied to the host as well:
   the host wants tickets to land, so the host cannot waive a red verify step,
   cannot loosen a criterion after the first attempt, and cannot override a
   review rejection without a recorded argument (see *Review*).
4. **Provenance decides what is immutable.** Criteria and spec numbers carry
   their author — `human`, `host`, or `loop`. A human number is immutable to
   everyone, the host included (`respec._moved_numbers`). A host-authored
   criterion is ratcheted once an attempt exists: tighter, never easier.
5. **Amnesty is measured, never asserted.** Pre-existing failure is whatever
   `_baseline_failures` measured. A host that writes "ignore this error" into a
   contract is refused exactly as respec is (`_refuse_verification_waivers`).
6. **Attribution comes from diagnostic blocks.** Results returned to the host
   distinguish `introduced` from `already` using `failures._blocks`, so the host
   is never told a ticket broke something it did not.
7. **"The command failed" is not "the code is wrong".** An environment failure
   is reported as such and stops delegation, rather than being handed to an
   executor as something to fix.
8. **Validate against recorded artifacts.** Every delegate call writes the same
   artifacts it does today, and `forge replay` keeps working over them.
9. **A bug is never fixed on faith.** A bug contract requires a reproduction
   that fails before any fix is attempted.
10. **Withheld is a human's.** A ticket routed `withheld:<reason>` cannot be
    delegated. The host may implement it itself; the tool records that it did.

What is deliberately dropped: the sign-off *majority*. Objections from delegate
seats are still collected — the float-margin objection on the lock was real —
but they are advice to the host, not a vote that can park a ticket. The false
objection that cost three runs could not have parked anything under this design.

---

## Shape

```
host model (Claude Code / API / any MCP client)
  │   plans, scopes, writes contracts, reads results, decides
  │
  ▼  MCP (stdio)            ◄── also: `forge <cmd> --json`, same API
forge tool
  ├─ contract    validate + store tickets; provenance; ratchets
  ├─ engine      one ticket, one attempt loop: BUILD → APPLY → TESTS → VERIFY → REVIEW
  │               every gate in LOOP-INVARIANTS runs here, unchanged
  ├─ jobs        long calls run in a worker; host polls or waits
  ├─ decisions   every point where the loop parks becomes a question for the host
  ├─ ledger      run.db: tickets, steps, events, usage, + jobs, decisions, contract history
  └─ seats       providers: llamacpp, openai, anthropic, gemini, claude-cli, comfyui
                 roles: executor, tester, reviewer, (planner — autopilot only)
```

Three ways in, one engine:

- **Host mode (new, primary).** The host calls tools ticket by ticket. No
  daemon. Each `delegate` call runs one ticket's attempt loop and returns.
- **Autopilot (kept).** `forge go` and `forge_run_backlog` still drain a backlog
  unattended — the property that a usage limit at 2am does not kill the run is
  worth keeping. What changes is what happens at a judgement point: instead of
  asking the local planner, the run files a *decision* and moves to the next
  ticket that does not depend on it. A host that comes back answers the queue.
  A project with no host configured keeps today's behaviour exactly.
- **CLI (kept).** Every tool is also a CLI subcommand with `--json`, so a human,
  a script, or a host without MCP can use the same surface.

---

## The tool surface

Ten tools. Each returns structured JSON sized for a host's context: distilled
failures, not raw logs, with artifact paths to drill into. A host's context is
the expensive resource this whole project exists to spend carefully.

### `forge_status`
Project config, seats and whether each is reachable (`forge doctor`), verify
commands, the open tickets and their state, pending decisions, running jobs.
The first call a host makes, and cheap.

### `forge_contract`
Open or revise a ticket.

```json
{
  "id": "SHT-002",                     // omit to open a new one
  "kind": "code | bug | image",
  "spec": "...",
  "allowed_files": ["..."],
  "reference_files": ["..."],
  "criteria": ["...", "..."],
  "needs": ["SHT-001"],
  "route": "delegate | withheld:<reason>",
  "reason": "why this revision"        // required on revise
}
```

Runs every check `forge ingest` and `/forge-spec-check` run today and returns
them as refusals or warnings, not prose: unowned files, a criterion the harness
already settles ("tests pass"), a test path the runner will not collect,
manifest gaps, undeclared order, a module-list-only scope, `neverDelegate`
hits. It widens the read scope with `evidence.reading_scope` and
`named_paths` and returns what the delegates will actually see.

On revise, the ratchets apply: human numbers cannot move, waivers are refused,
a criterion with an attempt behind it can be tightened and never loosened, and
every change is stored with the old text, the new text, and the reason. The
host *is* allowed what respec is not — widening scope into another ticket's
file, editing a criterion — because that is the authority the overseer spec
argued for, and it is recorded where a person can read it.

`spec_file` may be passed instead of fields: a ticket-shaped markdown document
is parsed verbatim, as `forge ingest` does now.

### `forge_objections`
Optional pre-flight, replacing sign-off. Asks the configured delegate seats
whether the contract is buildable and returns their objections, each tagged
with the seat that raised it. Nothing is parked. The host reads them, may verify
a claim against the tree, and revises or proceeds. Cheap insurance on a ticket
the host is unsure of; skippable on one it is not.

### `forge_delegate`
Run the attempt loop on one ticket with delegate seats.

```json
{ "id": "SHT-002", "attempts": 3, "seats": {"executor": "qwen"}, "wait": 600 }
```

Inside, unchanged: baseline, canary, BUILD, APPLY with scope and parse gates,
unresolved-import and gutted-manifest checks, TESTS from the tester, VERIFY,
collection check, REVIEW by the delegate reviewer, quarantine on give-up. What
is removed from inside: respec, split, re-ratify, the convergence brake across
cycles — those were the loop judging itself, and the host now does that between
calls.

Returns a job id immediately, or the result if it finishes within `wait`
seconds. The result:

```json
{
  "status": "landed | failed | blocked | halted",
  "attempts": 2,
  "diff_stat": "3 files, +140 -12",
  "diff_path": ".hybridforge/artifacts/run-4/SHT-002/attempt-2/diff.patch",
  "verify": [{"name": "test", "ok": false,
              "introduced": ["tests/test_lock.py::test_posts — AssertionError: 0.2399 < 0.24"],
              "already": []}],
  "review": {"verdict": "reject", "seat": "nemotron", "reasons": ["..."]},
  "blocked": "BLOCKED: needs scripts/turntable.py to read flat_elevation",
  "quarantined": ["assets/lock.py"],
  "decision": "D-17",
  "usage": {"executor": {"in": 41000, "out": 9000}, "...": {}},
  "artifacts": ".hybridforge/artifacts/run-4/SHT-002/"
}
```

`halted` is invariant 12 and 13: the tree cannot verify anything, or a command
never reached the code. The host is told which, and delegation is refused until
it is fixed.

### `forge_job`
Poll or wait on a job: `{ "job": "J-9", "wait": 300 }`. Returns progress (which
step, which attempt) or the final result. Also `cancel`, which applies after the
current step, never mid-patch — the same rule as `forge stop`.

### `forge_verify`
Run the project's verify commands now, with attribution against a baseline the
host names (a ticket's, or `HEAD`). Usable on the host's **own** edits: a host
that implements a withheld ticket itself is held to the same commands and gets
the same `introduced` / `already` split. This is how the standards reach work
that never passed through a delegate.

### `forge_review`
Independent review of a diff against a contract by a delegate seat — for a
ticket the host implemented itself, or a second opinion on a delegated one.
The reviewer has no stake in the ticket landing, which is the point.

### `forge_decide`
Answer a decision. Decisions are the queue of every point where the loop used to
park for a person or an overseer: a respec returning impossible, a `BLOCKED:`
that needs a file outside the repository, a stalled image edit, a contradiction
between two tickets' tests, exhausted attempts. Each carries the evidence the
loop already gathers. The answer is one of the actions the host is allowed —
revise, release, discharge, reassign a seat, split, abandon — and is recorded
against the decision.

### `forge_close`
Land or abandon. Landing commits if the project has `commit` on, and refuses a
ticket whose last verify is red. Abandoning quarantines what the ticket wrote,
as today. `discharge` — "the host wrote this itself" — is a third outcome, and
requires a green `forge_verify` and a `forge_review` on record.

### `forge_offload`
Generic bulk work that is not a ticket: summarise these files, generate fixture
data in this schema, describe this render, draft these docstrings. A prompt, a
seat, and an optional JSON schema the reply is validated against (with the same
recover-before-refusing rule as invariant 7). No files are written. This is the
cheap path for "I need tokens, not judgement", and it keeps the host from doing
bulk generation itself because the ticket machinery was too heavy for it.

### `forge_inspect`
Read the ledger: a ticket's history, one step's full output, an artifact, the
contract history, `forge signoff`-style counts per seat, `forge replay` over a
run. Read-only. This is what the host uses instead of reading `run.db` by hand.

---

## Review

The question the design has to answer honestly: once the host plans the ticket,
writes its criteria, and watches it run, who is independent?

- **Verification is not negotiable.** No tool can land a ticket over a red
  command whose failure is `introduced`. Only the baseline excuses.
- **A delegate seat reviews every delegated diff**, as today.
- **The host may overrule a rejection**, but only by calling `forge_close` with
  an `overrule` argument that names the reviewer's reason and the file and line
  that answers it. It is held to invariant 3's form — an argument that does not
  name its evidence is recorded as a refusal — and every overrule is counted per
  ticket and shown in `forge_status`. A run with many overrules is a run whose
  reviewer seat or whose host is wrong, and the record should say which.
- **The host's own code** goes through `forge_verify` and `forge_review` before
  `discharge`, so it is never the only reader of what it wrote.

---

## Guarding the host's own hands

A tool only enforces what passes through it. A host with its own Edit tool can
write any file, including a test that is failing a delegated ticket. Two layers:

- **The ledger notices.** Every `forge_delegate` and `forge_verify` records the
  tree hash it ran against. A file in a ticket's test scope that changed between
  two calls without passing through the tool is reported in the next result as
  an untracked edit to a judging file.
- **A Claude Code hook refuses it.** The plugin ships a `PreToolUse` hook on
  `Edit|Write` that reads the active tickets from `run.db` and denies host edits
  to a test file owned by a ticket with an attempt in flight, and to
  `allowed_files` of a ticket whose job is running. Off by default for other
  hosts; on by default in the plugin. This is invariant 3 applied to the host,
  enforced rather than requested — "instruction-following is not an access
  control" holds for frontier models too.

---

## Ledger additions

`run.db` keeps every table it has. New:

```
jobs        id, ticket_id, kind, state, step, attempt, started, ended, result_json
decisions   id, ticket_id, kind, question, evidence_json, answer_json, answered_by, at
contract    ticket_id, field, old, new, author (human|host|loop), reason, at
overrules   ticket_id, reviewer_seat, reason, argument, at
host_edits  ticket_id, path, before_hash, after_hash, detected_at
```

`runs` loses its role as the unit of work in host mode: a host session opens one
run per backlog it works, but nothing requires a backlog. A single ticket is a
legal run.

---

## Restructuring the code

`loop.py` is 9,276 lines, and `Orchestrator._attempt` alone is about a thousand.
Almost every gate in it already lives in its own module — `patch`, `failures`,
`imports`, `manifests`, `evidence`, `toolchain`, `respec._moved_numbers` — and
is called from the orchestrator. The restructure is mostly moving the call
sites, not the logic.

```
forge/
  gates/       scope, parse/recovery, imports, manifests, numbers, waivers,
               attribution, baseline, canary, collection, quarantine
               (pure functions where they are already pure; moved, not rewritten)
  engine/
    attempt.py     one ticket's BUILD → APPLY → TESTS → VERIFY → REVIEW,
                   callable without a run loop; returns AttemptResult
    bug.py         reproduce-then-fix
    image.py       render / edit loop
  contract/    ingest parser, validation, provenance, ratchets, revise
  jobs.py      worker thread + jobs table; cancel at step boundaries
  decisions.py the park points, as records
  autopilot.py what remains of Orchestrator.run: drain, order, retry cycles,
               decisions instead of planner calls when a host is configured
  mcp/
    server.py      stdio JSON-RPC, stdlib only; one handler per tool
    schemas.py     tool input/output schemas
  api.py       the ten operations as plain functions; MCP and CLI both call it
  cli.py       thin: argparse over api.py
  providers/, state.py, budget.py, ui/   unchanged
```

`api.py` is the seam. The MCP server and the CLI are both thin over it, so the
tool surface is testable without a model and without a transport.

The MCP server stays stdlib-only. The protocol over stdio is line-delimited
JSON-RPC, a few hundred lines, and the install promise — "`pip install -e .`,
not a deployment" — is worth more than a dependency.

### What moves to the host

| Today | After |
|---|---|
| `planner` seat, `forge ingest` of a freeform plan | host writes contracts; the planner seat remains for autopilot only |
| `ratify.py` majority | `forge_objections`, advisory |
| `respec.py` (planner revises a failing ticket) | host revises through `forge_contract`; respec's *guards* move to `contract/` and bind the host |
| `split.py` | host opens two contracts; `split` remains a decision answer |
| convergence brake across cycles | per-call `attempts`; the host decides whether a second call is worth it |
| overseer (unbuilt) | is the host |
| `forge-spec` plugin skills | one skill teaching the host the protocol: when to delegate, how to write a contract a 27B model can execute, how to read a result |

The guards are the part to be careful with. Respec today is both a judge and a
set of rules the judge is held to. The judge goes; every rule stays, applied to
whoever revises.

### What the plugin becomes

`forge-setup` stays. `forge-spec` becomes `forge`:

```
plugins/forge/
  .claude-plugin/plugin.json      registers the MCP server and the hook
  hooks/guard_judging_files.py    PreToolUse on Edit|Write
  skills/forge-protocol/          when to delegate, contract craft, reading results,
                                  answering decisions; replaces delegation-protocol
                                  and spec-contract
  skills/claim-verification/      from OVERSEER.md — check a seat's claim against
                                  the tree before acting on it
  commands/forge-spec.md          kept: designing a spec with a human is still a
                                  conversation
```

---

## A host session, end to end

The lock ticket, as it would run:

1. `forge_status` — executor and tester on Qwen, reviewer on Nemotron, verify
   commands green.
2. `forge_contract` with the human's spec — accepted; warnings that criterion 4
   compares a float with `>=` and no margin. The host adds margins (host-authored,
   tightening a human criterion's tolerance is a host revision, recorded).
3. `forge_objections` — Nemotron as reviewer objects the lock cannot be 0.155
   deep. The host checks: body 0.14 plus keyhole to -0.085 is 0.155. Proceeds;
   the objection and its refutation are in the ledger.
4. `forge_delegate` — landed on attempt 1, review accepted. ~2k tokens back to
   the host.
5. `forge_close`.

Same evidence as the 2026-11-02 run that seated Claude as planner, without a
config edit, and without a false objection able to park it.

---

## Migration

Incremental, behind the existing tests and `forge replay`. Each phase is usable
on its own.

0. **Characterise.** Freeze the attempt pipeline's behaviour with replay over
   the recorded runs (plex-namer, sample-project, platformer, sprite sheets)
   before moving anything. Invariant 9: the fixtures come from the recordings.
1. **Read-only MCP.** `api.py`, the stdio server, and `forge_status`,
   `forge_inspect`, `forge_verify`. Immediately replaces the Claude session
   reading `run.db` by hand.
2. **Extract the engine.** Move `_attempt` and its gates out of `Orchestrator`
   into `engine/attempt.py` and `gates/`, with the orchestrator calling the new
   module. No behaviour change; replay must read identically.
3. **Contracts and delegation.** `forge_contract`, `forge_delegate`, `forge_job`,
   `forge_close`, the `jobs` and `contract` tables. Respec's guards move into
   `contract/`.
4. **Decisions.** `decisions` table; autopilot files decisions instead of
   calling the planner when `host.enabled` is set; `forge_decide`.
5. **Plugin and hook.** `plugins/forge`, the guard hook, the protocol skill.
6. **Offload, objections, review.** `forge_offload`, `forge_objections`,
   `forge_review`.
7. **Prune.** Once host mode has carried real backlogs: demote `ratify.py`
   and `respec.py`'s judge paths to autopilot-only, and drop what nothing calls.

---

## How to know it worked

Measured from the ledger, per landed ticket, against the recorded runs:

- **Host tokens and cost.** The point is spending frontier tokens on judgement.
  If host tokens per ticket approach executor tokens, the host is doing bulk
  work and `forge_offload` or the protocol skill is failing.
- **First-attempt land rate.** Today 8 of 18 in the measured runs. Contracts
  written by the host should raise it; if they do not, contract craft is the
  problem, not the seats.
- **Decisions per ticket and how they were answered.** The platformer run
  would have produced about 23. A decision answered with "release" or "proceed"
  every time is a park point that should not exist.
- **Overrules and host edits.** Both should be rare. Many of either means the
  independence argument above is not holding.

---

## Decisions — confirmed 2026-11-02

1. **MCP as the primary surface, CLI `--json` as the mirror.** Recommended. The
   alternative — only the CLI, driven through a shell tool — works today and is
   what the watching Claude session already does; it is also why that session
   spent its context on `sqlite3` output.
2. **Delegation is asynchronous, with `wait`.** Recommended. A local executor
   attempt runs for minutes; a synchronous call blocks the host and hits MCP
   client timeouts.
3. **Autopilot is kept, with decisions instead of a planner.** Recommended. The
   unattended overnight run is a real property, and a decision queue lets it
   keep it without a weak planner making the calls.
4. **Sign-off majority is removed in host mode.** Recommended, on the lock
   evidence. Autopilot without a host keeps it.
5. **The host may overrule review, with a recorded argument.** The alternative
   is that it may not, ever — stricter, and a correct ticket then parks behind a
   wrong reviewer, which is the failure sign-off was added to prevent.
6. **The guard hook is on by default in the plugin.** It will occasionally block
   a host edit that was legitimate; the fix is to close or abandon the ticket
   first, which is the order of operations it should have used anyway.

---

## Status — what is built, and where it differs

**Built.** All eleven tools, over MCP (`forge mcp`) and from a shell (`forge
tool <name> '<json>'`), in `forge/api.py`, `forge/contract.py`,
`forge/engine.py`, `forge/jobs.py`, `forge/consult.py` and `forge/mcp/`. The
`forge` plugin registers the server, the `forge-protocol` and
`claim-verification` skills, and the guard hook. Host tickets live in a run with
status `host`, which `forge go` never drains. `jobs` and `contract_log` are new
tables; existing databases gain them on open.

**Measured.** `tests/test_api.py` covers the surface end to end with scripted
seats over a real git repository and real verify commands: a correct
implementation lands, a wrong one fails and is quarantined, an overrule needs a
`path:line` and a green re-verify, and every ratchet refuses what it should.
`scripts/replay_snapshot.py` holds the refactor to the recordings: 803 replay
records across eight evidence projects read identically before and after. One
live delegation, `SP-001` of the sample project with the `claude` seats:
landed on attempt 1 in 25 s for $0.23, lint and test green, the reviewer's
reasons quoting the diff.

**Different from the design, on purpose:**

- **The engine is a facade, not an extraction (phase 2).** `engine.delegate`
  runs `Orchestrator._work_ticket` for one ticket with sign-off and
  stop-on-block off; respec, split and retry cycles live in `run` and are never
  entered. That put every gate behind the tool without moving 9,000 lines, and
  callers depend on `engine.delegate`, so the extraction can happen behind it
  later with replay as the check.
- **Decisions are a queue over parked tickets, not a new park mechanism.**
  `forge_decide` lists every autopilot ticket a retry would reopen, with its
  note as the question, and answers through the existing return channel
  (advise, release, retry) or abandons. Autopilot itself still asks its local
  planner at respec; making it file a decision instead when a host is
  configured is not built.
- **`forge_review` reviews against `HEAD`.** A host ticket has no
  `baseline_tree` because nothing snapshotted it; the diff is the ticket's
  scope as it differs from the last commit.
- **The `host_edits` table is not built.** The guard hook refuses the edits
  instead of the ledger noticing them afterwards.
- **`forge-spec` stays.** The new `forge` plugin sits beside it; folding its
  skills in is left until host mode has carried real backlogs (phase 7, with
  pruning `ratify.py` and `respec.py`'s judge paths).

---

## The tic-tac-toe run — 2026-11-02/03

The test before merging: a browser tic-tac-toe game built entirely through the
tool surface, with the host (Claude Code) driving `forge tool` — the same
handlers MCP serves — and every call logged. The project is
`tictactoe_forge_test` beside this checkout; `host-session/` holds all 59 tool
calls and their answers.

**What was built.** Game rules, a perfect-play computer opponent and view
helpers (JS, `node:test`), delegated to Qwen with Nemotron reviewing; three
chalkboard assets — X, O and board — written as prompts by Qwen, rendered by
Flux 2 through ComfyUI, cut out by the project's GIMP finisher, tested with
Pillow by the tester and looked at by Claude; the page itself, withheld and
implemented by the host, then reviewed and discharged; a README through
autopilot. 12 contracts: 11 landed, one abandoned. 29 JS tests and the asset
tests green; checked in a browser and by a headless screenshot.

**What it cost.** Qwen 104 calls, 578k in / 196k out; Nemotron 22 calls,
89k / 46k; 13 renders (9 Flux 2, 4 Kontext edits); Claude 11 calls as the
viewing seat, $1.00. Everything but the viewing was local.

**Every tool was exercised**: status (with probe), verify (capture, against,
an attributed setup failure), offload (schema-validated, checked by hand),
contract (fields, `spec_file` both authors, revisions, a ratchet refusal),
objections, delegate, job (wait and cancel), inspect, review, close (land,
overrule, discharge — refused once before a review existed — and abandon),
decide (list and release), the guard hook against a running job and an open
ticket's test, and the one-delegation-at-a-time refusal.

**What the host had to decide**, which is the case for this design: a
contract stating a prohibition the softening check misread (reworded, and the
check fixed); a minimax scoring from the wrong side, verified by running the
set-aside code before revising; an O whose opaque centre would show on the
board (finisher changed, a tightened follow-up contract); a board whose cells
are not equal, which the reviewer correctly refused three times — accepted by
overrule because the page now sizes its grid from the measured lines, with the
argument and the measurements on the record.

**Twenty findings.** Fixed on this branch: managed servers never started by
host mode; the test suite starting a real llama-server through the machine
profile; tracebacks for an outage; image models missing from status; Windows
argv mangling non-ASCII; `spec_file` always `human`; objections leaving no
record of what a seat said; concurrent delegations; a negated criterion read
as a pinned value; a refusal not saying what an ID looks like; the tester never
shown the lint command or the ticket context; a stalled render not
quarantined; overrule unreachable for a review stall; the review naming the
wrong seat; a cancelled step left running; an abandoned ticket rebuilt on
delegate; a released ticket stranded in a finished run.

Open, and small: a block note that says a criterion "contradicts code this
ticket may not write" when the code is in scope; a ratchet refusal worded
"ratified into"; `diff_stat` omitting binary files; a cancelled attempt
counted against the ticket; and autopilot finishing a run `done` while a
ticket in it is withheld. One more is about contracts, not the tool: "names
the command" let the README invent `hybrid-forge serve`, and Nemotron passed
it — a criterion has to pin the value it means.
