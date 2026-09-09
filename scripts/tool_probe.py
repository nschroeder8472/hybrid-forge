"""Put a model in front of the read tools and record which ones it reaches for.

`outline` is 10 of 597 recorded tool calls — 1.7% — and it is the one tool
written to prevent the behaviour that has cost this loop three failed runs:
reading large files to find a small thing. The ledger says it is avoided. It
cannot say why, and the three reasons want different fixes:

    discoverability  the description does not say when to use it
    insufficiency    it returns names and line numbers and never a body, so a
                     turn spent on it answers nothing and a read still follows
    habit            the model greps and reads whatever it is offered

There is already evidence for the second: six of ten `outline` calls were
followed immediately by a `read_file`, and not one ended a lookup.

This is the smallest thing that can tell them apart. It runs one bounded tool
conversation against a configured role's real provider, with the real
`Toolbox` over a real repository, and records every call. It is `_converse`
with the loop taken off, so what it measures is tool choice and nothing else.

    python scripts/tool_probe.py <arm> [--role executor] [--runs 3]

Arms differ in one thing each:

    as-shipped   the tools exactly as the loop offers them today
    sold         `outline` described so it says when to reach for it
    with-symbol  `read_symbol` offered as well, which is the tool that gives
                 `outline` somewhere to lead

Reads only. It writes nothing to the repository and starts no run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.config import Config  # noqa: E402
from forge.providers import Message  # noqa: E402
from forge.tools import TOOLS, Toolbox  # noqa: E402

# One ordinary question about an unfamiliar repository, of the shape the
# executor faces on every ticket: find where something is done, and find the
# convention for doing it. Deliberately not the retention ticket -- a task the
# model has already failed three times is a task it has opinions about.
TASK = """You are about to add a method to a class in this repository.

Answer two things, and use the tools to find out rather than guessing:

1. What does `Toolbox.run` do when it is given a tool name it does not know?
2. If you added a new tool, which two places would you have to change for the
   model to be able to call it?

Answer in a few sentences. Do not write any code."""

SOLD = (
    "List what a source file declares — every class, function and constant, "
    "with signatures and line numbers, and no bodies. Call this FIRST on any "
    "file you have not read: it costs a fraction of reading the file and tells "
    "you which part you actually need, so the read that follows is a few lines "
    "rather than a few hundred."
)


def toolset(arm: str) -> list:
    """The tools this arm offers, differing from the shipped set in one way."""
    if arm == "as-shipped":
        return [spec for spec in TOOLS if spec.name != "read_symbol"]
    if arm == "sold":
        return [
            replace(spec, description=SOLD) if spec.name == "outline" else spec
            for spec in TOOLS
            if spec.name != "read_symbol"
        ]
    if arm == "with-symbol":
        return list(TOOLS)
    raise SystemExit(f"unknown arm: {arm}")


def probe(config: Config, role: str, arm: str, turns: int) -> tuple[list[str], str]:
    """One bounded conversation. Returns the tool calls made, and the answer."""
    provider = config.provider_for(role)
    box = Toolbox(config.root)
    specs = toolset(arm)
    thread = [Message(role="user", content=TASK)]
    used: list[str] = []

    completion = None
    for remaining in range(turns, 0, -1):
        last = remaining == 1
        completion = provider.complete(
            thread,
            max_tokens=provider.capabilities().max_output_tokens,
            temperature=0.0,
            **({} if last else {"tools": specs}),
        )
        if not completion.tool_calls:
            break
        thread.append(
            Message(
                role="assistant",
                content=completion.text,
                tool_calls=completion.tool_calls,
            )
        )
        for call in completion.tool_calls:
            used.append(call.name)
            thread.append(
                Message(role="tool", content="", tool_result=box.run(call))
            )
    return used, (completion.text if completion else "")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Probe how a model picks tools.")
    parser.add_argument("arm", choices=("as-shipped", "sold", "with-symbol"))
    parser.add_argument("--role", default="executor")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    config = Config.load(args.root)
    print(f"arm={args.arm} role={args.role} model={config.provider_for(args.role).model}")
    print(f"tools offered: {', '.join(spec.name for spec in toolset(args.arm))}\n")

    totals: Counter[str] = Counter()
    for run in range(1, args.runs + 1):
        started = time.time()
        used, answer = probe(config, args.role, args.arm, args.turns)
        totals.update(used)
        print(
            f"  run {run}: {len(used)} call(s) in {time.time() - started:.0f}s — "
            + (", ".join(used) or "none")
        )
        print(f"    answered: {' '.join(answer.split())[:110]}")

    total = sum(totals.values())
    print(f"\n{total} tool call(s) over {args.runs} run(s):")
    for name, count in totals.most_common():
        print(f"  {name:<12} {count:>3}  {100 * count / max(1, total):4.1f}%")
    print(json.dumps({"arm": args.arm, "runs": args.runs, "calls": dict(totals)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
