# Results — a local decision model (Deem 9B) on forge's decisions, 2026-11-04

Deem 9B v1 (LibertAIDAI, Apache-2.0, revision 8063b7f), served locally with
its own `serve/deem_server.py` and the shipped `calibration_v13`, 18.8 GB on
the card, about 0.65-1.2 s per decision. Data from forge's recorded runs,
read-only. See `evaluate.py`, `baseline.py`, `data/`, `results/`.

## Decision 1 — what kind of failure is this? (128 verify outputs)

| | Accuracy |
|---|---|
| Keyword rules (`baseline.py`) | 100% — written after reading these outputs, so flattered |
| Deem 9B, every answer | 93.8% |
| Deem 9B, answers at confidence >= 0.7 | 100% on 120 of 128; 8 escalated |
| Deem 9B, answers at confidence >= 0.9 | 100% on 112 of 128; 16 escalated |

Deem's misses are Rust lints promoted to errors (`error: this if can be
collapsed`), read as compile failures, mostly at low confidence. Four labels
were corrected after the run (21, 88, 90, 122: compile errors under leading
warnings); both classifiers had disagreed with the hand label there.

## Decision 2 — will this ticket land on its first attempt? (109 tickets, 30% did)

| | AUC | Brier | Notes |
|---|---|---|---|
| Base rate | 0.50 | 0.211 | |
| Deem 9B | 0.53 | 0.261 | worse than the base rate; 6 tickets at >= 0.8, none landed |
| Opus, one call ($0.78) | 0.69 | 0.190 | calibrated in every bin; told the 30% base rate, Deem was not |

## Decision 3 — did the reviewer accept? Dropped

136 of 145 verdicts open with ACCEPT or REJECT and `parse_verdict` reads
every one correctly. Nothing for a model to add.

## What it says

A local decision model is a reasonable *classifier with an honest
confidence*: on failure kind its high-confidence answers were all right, so
"answer when sure, escalate when not" works. But hand-written rules already
solve that decision on this data, so it adds little there. On the decision
nothing handles today — will this ticket land — it is at chance and
overconfident, while Opus in one cheap call is meaningfully better. The
judgement that matters stays with the frontier model.
