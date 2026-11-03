---
name: claim-verification
description: Use when a delegate seat (executor, tester or reviewer in a forge result) makes a claim about the code, the arithmetic, or the tree — an objection, a BLOCKED note, a review rejection — and you are about to act on it: revise a contract, overrule a review, or abandon a ticket. Covers turning the claim into a check you run rather than an opinion you weigh.
---

# Claim verification

A delegate's statement about the tree is a claim. Acting on an unchecked claim
is how the loop lost runs: a reviewer that rejected correct code because it
read "a dead world" as state 3 when the ABI says 1; a planner that said a fall
took longer than 1000 ms when the arithmetic says 457; a reviewer that said no
API could set `player.y` when the field is public; a planner that raised the
same false geometry objection on every vote. Every one was settled in seconds
by checking, and none by thinking harder.

## The rule

Before you revise, overrule or abandon on the strength of a seat's claim, turn
it into a check and run it. Record the check in the argument you pass.

## What is checkable

| Claim shape | Check |
|---|---|
| "there is no API / function / field for X" | grep for the name; read the definition |
| "X returns / equals / is Y" | read the line; or run a three-line script |
| arithmetic — time, distance, size, count | compute it, with the spec's own numbers |
| "the spec says X" | quote the spec; a revision may have changed it — `forge_inspect` the ticket for `original_spec` |
| "this file is outside my scope" | compare with `allowed_files` in the contract |
| "this test is wrong" | run it in isolation; read the criterion it encodes |
| about a rendering | look at the capture; count pixels if it matters |
| "this was already failing" | `forge_verify` against a baseline; never take it on assertion |

## Outcomes

- **The claim is true**: act on it — revise the contract, widen the scope, or
  do the work yourself. Name the seat that was right.
- **The claim is false**: proceed, and if it blocked a ticket, overrule with
  the `path:line` that refutes it. If one seat keeps making false claims, the
  fix is a different seat (`seats` on `forge_delegate`), not repeated
  overrules.
- **You cannot check it**: say so, and treat it as open. Do not settle it by
  preference.
