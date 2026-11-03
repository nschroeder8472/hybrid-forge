# Bench: does forge beat Opus alone?

The same task, three ways, scored by one hidden acceptance suite.

| Arm | Driver | Who builds | Art |
|---|---|---|---|
| A | Opus, as forge host | Qwen executor and tester, Nemotron reviewer (local) | Flux 2 via ComfyUI; Opus looks at it |
| B | Opus, alone | Opus | Opus draws it from code; no image model |
| C | Opus, as forge host | Sonnet executor, Haiku tester, Sonnet reviewer | Flux 2 via ComfyUI; Sonnet looks at it |
| D | Opus, with the smith's tools (anvil, gauge, mold) | Opus | Flux 2 candidates via `mold`; Opus chooses |

Every arm gets `TASK.md` verbatim on stdin, starts from `seed/` as a fresh git
repository, and runs headless as `claude -p --model opus` with user settings
and user MCP servers excluded. The forge arms get the `forge` MCP server, the
guard hook, and the `forge-protocol` and `claim-verification` skills as an
appended system prompt (`arms/host.md` says how to use them); B gets
`arms/alone.md`. Nobody intervenes.

## Scoring

`acceptance/` is never copied into an arm's repository (the page probe is
copied in for the check and removed after it). 49 checks:

- `game.accept.mjs` — 19: the pinned rules, error order, first-line-wins, and
  the opponent exhaustively, as X and as O.
- `assets_accept.py` — 16: the pinned asset files, sizes, transparency and
  opacity, and loose colour checks for the words the task states.
- `page_accept.py` + `probe.html` — 14: headless Edge plays the page through
  the pinned DOM, including the computer opponent, and takes a screenshot.

The suite was validated before any arm ran: the hand-steered run in
`tictactoe_forge_test` passes 49/49, and the empty seed fails it.

Cost: `claude -p` reports the host's `total_cost_usd`; forge records each
Claude seat's cost in `run.db`. Both are summed. Local tokens are reported
separately, because they cost electricity, not dollars.

What 49 checks cannot judge is how the art looks. Each run keeps
`screenshot.png` for a person to rate, side by side.

## Running

```bash
python run_arm.py B --rep 1     # results in D:\Users\Nik\Documents\Projects\forge-bench\B-1
python run_arm.py C --rep 1
python run_arm.py A --rep 1     # needs the GPU for llama-server and ComfyUI
python report.py
```

Run arms one at a time: A and C share the GPU through ComfyUI, and a
delegation reads the whole tree.
