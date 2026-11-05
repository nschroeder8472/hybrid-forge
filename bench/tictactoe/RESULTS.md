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

---

# Run 2 — arm D, the smith's tools, and a second Opus-alone run

| arm | accept | game | assets | page | opus $ | seat $ | claude $ | opus out tok | opus in+cache | local tok | renders | turns | wall | tickets | attempts | overrules | own tests | uncommitted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A-1 | 49/49 | 19/19 | 16/16 | 14/14 | 2.68 | 0.85 | 3.53 | 40,442 | 5,602,877 | 458,076 | 9 | 101 | 56m | 6/8 | 13 | 1 | 20 | 1 |
| B-1 | 49/49 | 19/19 | 16/16 | 14/14 | 0.95 | 0.00 | 0.95 | 23,262 | 891,252 | 0 | 0 | 44 | 3m | - | - | - | 29 | 0 |
| B-2 | 49/49 | 19/19 | 16/16 | 14/14 | 1.64 | 0.00 | 1.64 | 43,856 | 1,115,419 | 0 | 0 | 67 | 7m | - | - | - | 40 | 0 |
| C-1 | 49/49 | 19/19 | 16/16 | 14/14 | 2.90 | 1.24 | 4.13 | 42,434 | 6,104,711 | 0 | 15 | 101 | 25m | 7/8 | 18 | 0 | 33 | 0 |
| D-1 | 49/49 | 19/19 | 16/16 | 14/14 | 1.39 | 0.00 | 1.39 | 28,692 | 1,101,687 | 0 | 0 | 68 | 11m | - | - | 0 | 36 | 1 |

D-1's `renders` reads 0 because mold did not yet meter its renders; it made
28 in five batches (4, 6, 6, 6, 6), and ran anvil twice. Mold now records each
render in the usage table.

**D: $1.39, 11 minutes, 49/49.** Opus did the work itself and used mold for the
art and anvil to check. Against Opus alone ($0.95 and $1.64 over two runs, mean
$1.30) that is 1.07x — inside the 1.3x the design predicted, and inside the
baseline's own spread: B's two runs differ by 73%. Against the host arms it is
about a third of the cost and a fifth to a half of the time.

**What D bought for that:** Flux 2 art chosen from 28 finished, measured
candidates, at the price of Opus alone. Whether that art beats Opus drawing
chalk with Pillow is the open question, and it is a matter of taste: B-2's
procedural chalk is clean and coherent; D-1's is photographic, pieces well
seated in the drawn grid. Compare forge-bench/*/screenshot.png side by side.

**What this settles, at n = 1-2:** the smith's tools cost roughly nothing over
Opus alone on a small task, where host mode cost 3x. It does not show they make
the code better — nothing could here, every arm scores 49/49 — and it does not
test the cheap-model tools (bellows, striker), which need a task where bulk
dominates.

---

# Blind art rating — 2026-11-03

One rater (the project owner, who had not seen any results), a blind page:
the five finished games as anonymous letters, all ten pairings once in random
order with random sides, then pick-one-of-five for the X, the O and the board.
Letters were reshuffled per section; the key stayed on disk.

**Whole game, pairwise (wins-losses):**

| Build | Art made by | Record |
|---|---|---|
| B-2 | Opus alone, Pillow | 4-0 |
| B-1 | Opus alone, Pillow | 3-1 (lost only to B-2) |
| A-1 | Flux 2 via host mode, local seats | 2-2 |
| D-1 | Flux 2 via `mold`, Opus choosing | 1-3 |
| C-1 | Flux 2 via host mode, Claude seats | 0-4 |

No ties, and the ten choices form a strict order with no cycles, so the
preferences were consistent. In the six matchups between an Opus-alone build
and a Flux build, Opus alone won all six (1 in 64 by chance).

**Pieces:** X — B-2. Board — B-2. O — D-1, the one Flux pick.

**What it says.** On this task the image pipeline did not buy better art.
Opus drawing chalk from code was preferred over Flux 2 every time it met it,
including when Opus itself chose the Flux candidates through `mold`. With
correctness tied and cost lower, Opus alone wins this task outright. The case
left for `mold` is art Opus cannot plausibly draw from code — painted
characters, scenes, textures — which this task did not ask for.
