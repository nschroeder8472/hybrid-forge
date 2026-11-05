# Verdict — what four benches said about forge-as-a-tool

November 2026. Every claim here is measured in `bench/`, with the runs, the
scores and the blind votes kept beside the code that produced them.

## The premise, and what happened to it

Hybrid Forge was built on one bet: that enough structure — scoped contracts,
attributed verification, ratchets, review — lets cheaper models do work worth
having, so a frontier model's tokens go on judgement and cheap tokens go on
bulk. Restructured as tools for Claude, the same bet became "Claude delegates,
the tools enforce the standards."

It did not hold.

| Bench | What won | The number that decided it |
|---|---|---|
| Tic-tac-toe in a browser (`bench/tictactoe`) | Opus alone | Every arm 49/49 on the hidden suite. Opus alone $0.95-1.64; Opus hosting forge $3.53-4.13, because supervising cost more than doing — 5.6-6.1M input tokens against 0.9M. The pieces most worth delegating came back to Opus. |
| Its chalk art, rated blind | Opus alone (Pillow) | Preferred over every Flux build, 6-0 in the cross matchups, a strict ordering with no ties. |
| A 36-frame chest sprite sheet (`bench/chest`) | Opus with Blender | Both arms built one 3D model and rendered every angle. Given an image model, Opus correctly declined it: separate renders cannot hold one object consistent. |
| Six painted item icons (`bench/icons`), rated blind | **Opus with `mold`** | Flux preferred 6-0-1, at a third of the cost ($1.05 vs $3.28) and half the time. |
| A local decision model, Deem 9B (`bench/decide`) | Rules, then Opus | Failure kind: 93.8%, 100% when confident, but four keyword rules already solve it. Will a ticket land first try: chance (AUC 0.53, worse than the base rate); Opus in one $0.78 call: AUC 0.69, calibrated. |

Every time a weaker model's judgement replaced Claude's — a local reviewer, a
sign-off vote, a respec, a decision model — the result got worse or more
expensive. Claude's judgement was never the bottleneck; at this generation of
frontier model, it is the cheapest part of the work to keep.

## What survives

**`mold`, for art Claude cannot make from code.** Painted, detailed,
single-image work: icons, illustrations, textures. It is the one measured win,
and it is a narrow one — the same tool lost on clean graphic art and was
rightly ignored on a multi-view sprite sheet. Its shape is what made it work:
several candidates in one call, finished and measured, on one contact sheet,
with Claude choosing.

**`gauge`** — measurements and contact sheets for that choice. Small, and it
makes the one look cheap.

**`anvil`** — failure attribution against a baseline, plus the import and
manifest gates. It costs almost nothing; nothing measured it changing an
outcome. A convenience, kept because it is free.

**The benches.** Same task, fresh repositories, headless `claude -p`, hidden
acceptance suites validated in both directions, cost from the CLI's own JSON,
and blind rating pages for what only a person can judge. They are what turned
"seems to work" into an answer, and they are the way to test the next idea.

**The loop fixes found along the way** — the tester shown its lint command,
renders quarantined like any apply, negated criteria not read as pinned values,
released tickets reopening a finished run — help the overnight daemon too and
stay.

## What is retired

Host mode: contracts as the unit of work, delegation to seats, jobs,
objections, review and decision tools for a host, the guard hook and the host
skills. Kept intact at the tag `archive/host-mode` with the design
([FORGE-AS-A-TOOL.md](FORGE-AS-A-TOOL.md)) and its benchmark arms.

## What is not decided

**The overnight daemon (`forge go`).** It spends no frontier tokens, which is
its one advantage, and the runs behind it needed a person about once a ticket
on hard backlogs. Whether that is worth keeping is a question about who runs it
and why — local-only work that cannot leave the machine is the strongest case
— not one these benches answer.

**A large backlog.** No bench gave Opus a task big enough to fill its own
context, the one regime where offloading bulk might still pay. If that case
matters, it is the next bench to build; the harness is ready for it.

## The rule this leaves

Give Claude tools that do things it cannot — render pixels, measure them, run
a toolchain — and keep every judgement with Claude. A tool that tries to judge
for it has, so far, always cost more than it saved.
