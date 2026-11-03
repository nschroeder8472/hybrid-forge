# Results — run 1, 2026-11-03

One run per arm, unattended, scored by the hidden suite after the scorer fix
(node's `spec` reporter had made every arm read 0/19 on the game checks; the
saved repositories were re-scored with `--test-reporter=tap`, nothing re-run).

| arm | accept | game | assets | page | opus $ | seat $ | claude $ | opus out tok | opus in+cache | local tok | renders | turns | wall | tickets | attempts | overrules | own tests | uncommitted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A-1 | 49/49 | 19/19 | 16/16 | 14/14 | 2.68 | 0.85 | 3.53 | 40,442 | 5,602,877 | 458,076 | 9 | 101 | 56m | 6/8 | 13 | 1 | 20 | 1 |
| B-1 | 49/49 | 19/19 | 16/16 | 14/14 | 0.95 | 0.00 | 0.95 | 23,262 | 891,252 | 0 | 0 | 44 | 3m | - | - | - | 29 | 0 |
| C-1 | 49/49 | 19/19 | 16/16 | 14/14 | 2.90 | 1.24 | 4.13 | 42,434 | 6,104,711 | 0 | 15 | 101 | 25m | 7/8 | 18 | 0 | 33 | 0 |

Claude $ is the Opus host's `total_cost_usd` plus each Claude seat's recorded
cost (A's seat cost is Opus looking at renders; C's is Sonnet and Haiku). Local
tokens cost electricity.

## Reading it

**Correctness: a tie.** All three pass 49/49. The suite cannot separate them,
which at this size is the finding: Opus alone gets a small, well-specified game
right first time.

**Cost and time: Opus alone wins by a wide margin.** $0.95 and 3 minutes,
against $3.53 / 56 minutes (A) and $4.13 / 25 minutes (C). The forge arms'
Opus host alone cost 2.8-3.1x Opus doing all the work, before any seat is
counted. The host read 5.6-6.1M input tokens (mostly cache) against 0.9M:
101 turns of polling jobs, reading results and revising, each turn re-reading
a context that grows with every tool result.

**Delegation did not hold.** In A the board render failed five attempts and
the host made the board itself; in C Sonnet's opponent failed twice and the
host wrote it, and the game took three attempts. Both hosts also wrote the
page. The pieces most worth delegating were the ones that came back to Opus.

**Art: different, not clearly better.** B drew the chalk from code with Pillow
and got a clean, coherent board with pieces centred in the cells. A and C have
photographic chalk from Flux 2, more texture and more realism; C's grid is
uneven and its pieces sit off the drawn cells. A person should rate the three
screenshots in forge-bench/*/screenshot.png side by side.

## What it says about forge

On a task this size the host's overhead is the cost, and it is larger than the
work. The claim forge rests on — spend frontier tokens on judgement and cheap
tokens on bulk — needs bulk to exist. Next, in order:

1. Cut host overhead and measure it again: `forge_job` polling and full
   results re-read every turn are most of the 5-6M input tokens.
2. A task where bulk dominates (a 10-15 ticket backlog on an existing repo),
   where Opus alone fills its own context.
3. Three repetitions per arm before believing any gap that is not this large.
