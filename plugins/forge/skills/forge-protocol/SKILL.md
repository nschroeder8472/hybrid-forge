---
name: forge-protocol
description: Use when the forge MCP tools (forge_status, forge_contract, forge_delegate, forge_verify, forge_close, ...) are available and there is implementation work that a cheaper model could do — deciding what to delegate and what to keep, writing a contract a local 27B model can execute, reading a delegation result, and settling a ticket. Also use before editing files yourself in a project with a .hybridforge directory, so your own work is verified the same way.
---

# Forge protocol

You are the host: you plan, scope, write contracts, read results and decide.
The delegates — executor, tester, reviewer, usually local models — do the bulk
generation. Forge sits between you, and enforces the standards on everything
that passes through it. Spend your tokens on judgement; spend theirs on bulk.

## Start

1. `forge_status` — who sits in each seat, what the verify commands are, what
   tickets and jobs exist.
2. `forge_verify` with `capture: "start"` before anyone edits anything. Every
   later verification is attributed against it.

## What to delegate

Delegate when all hold:

- the approach is settled — you could describe the result exactly;
- it is confined to files you can list;
- success is stated as assertions a tester can encode;
- a wrong answer is caught by tests, types or review.

Do it yourself — route the contract `withheld:<reason>` so the record says why
— when it touches security, concurrency, a public interface or migration,
compliance, a profiling judgement, or a question still open. Also do it
yourself when writing the contract would take longer than the work.

## Writing a contract (`forge_contract`)

The executor is capable and literal. It cannot ask you a question mid-attempt;
an ambiguity comes back as `BLOCKED:` or as a plausible wrong answer.

- **`spec`**: what to build, in the words you would check it against. Name the
  functions, types, files and libraries. Copy any table, list of error
  strings, or mapping verbatim — a summarised table is a deleted one.
- **`allowed_files`**: everything it may write, including the test file the
  tester will write (`tests/<thing>_test.py` or the project's convention).
- **`reference_files`**: everything it must read — signatures it calls,
  types it must match, **and an existing passing test to copy the shape of**.
  Forge widens this from the files your spec names, and shows you the result.
- **`criteria`**: concrete, checkable outputs: "`parse('a,b')` returns
  `['a', 'b']`", not "parses correctly". Say what to do, not what to avoid —
  a prohibition is satisfied by doing nothing. Never "tests pass" or "lint
  exits 0": the harness runs those itself, and a tester told to encode them
  writes tests that shell out to the suite.
- **`needs`**: tickets whose output this one reads. Forge orders tickets that
  share a writable file on its own; it cannot see a dependency through a type.

Read the warnings it returns before delegating. Each one names a failure seen
in a real run.

Once an attempt exists the contract is on the record: you can sharpen a
criterion, never drop one; numbers a person wrote never move; no contract may
excuse a failing check. If the contract is wrong in a way that needs one of
those, abandon the ticket and open a new one — the record then says so.

## Delegating (`forge_delegate`, `forge_job`)

`forge_delegate` returns a job. Pass `wait` (seconds) to block for it, or poll
with `forge_job`. Local attempts take minutes; do other work meanwhile.

Read the result, not the transcript:

| `status` | Meaning | Your move |
|---|---|---|
| `landed` | verified green, reviewer accepted | `forge_close` `land` |
| `failed` | attempts exhausted; work set aside | read `verify[].failure` and `review.reasons`; sharpen the contract and delegate again, or do it yourself |
| `blocked` | executor said what it needs (`note`) | usually a missing file in scope or a spec ambiguity — revise and delegate again |
| `halted` | the tree or toolchain cannot verify anything (`reason`) | fix the machine or the red tree first; no ticket can |
| `cancelled` | you cancelled it | — |

`verify[].errors` are diagnostics forge attributed to this ticket; failures
that pre-date it are excused by a measured baseline, never by assertion. Use
`forge_inspect` with a `step` id when you need a whole output.

Repeated failure on the same evidence is information about the contract or the
seat, not about luck. Two failed delegations on one ticket: change the contract,
change the seat (`seats: {"executor": "<model>"}`), or do it yourself.

## Settling (`forge_close`)

- `land` — a `landed` ticket. `commit: true` commits its scope.
- `overrule` — the reviewer rejected work that verified green, and you can show
  it was wrong. `argument` must cite the `path:line` that answers the
  objection. The work is restored and re-verified; anything red refuses it.
  Overrules are counted. Check the claim against the file before you make it.
- `discharge` — you implemented it yourself. `against` names the baseline you
  captured before starting; nothing may be introduced.
- `abandon` — give it up, with a reason. Its work stays salvageable under
  `.hybridforge/abandoned/`.

## Your own edits

The same standards apply to you. Before editing: `forge_verify` `capture`.
After: `forge_verify` `against`. `clean: true` means you introduced nothing;
an `introduced` failure is yours to fix. A step with `environment` set never
reached the code.

A hook stops you editing a test inside the scope of a delegated ticket that has
not landed, and any file of a ticket whose job is running. The test encodes the
criteria the delegate is judged by; editing it is grading the work. Revise the
contract or settle the ticket first.

## Checking a delegate's claim

A seat's assertion about the tree — "there is no setter for X", "this takes
longer than 1000 ms", "the body is 0.14 deep so this cannot be 0.155" — is a
claim, not evidence. Check it with a grep, a file read or a ten-line
calculation before acting on it. See the `claim-verification` skill.
