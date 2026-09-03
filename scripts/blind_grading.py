"""Build the two arms of the blind-grading experiment. See docs/BLIND-GRADING.md.

The convergence machinery in `docs/CONVERGENCE.md` has never run. Seven runs of
the fixture have tried to reach it by making the work harder, and all seven
landed on the first attempt, because the reference stall those features were
derived from was never about hard work: the models were graded against linter
and compiler configuration no prompt contained. Feature 1 fixed that, which is
why the fixture cannot reproduce it.

So this puts the defect back, under control, and changes one variable:

    arm-blind  loop.toolchainContext = false   the rule is not in the prompt
    arm-shown  loop.toolchainContext = true    it is

Everything else is identical — same ticket, same model, same `.flake8` carrying
a `max-line-length` no default satisfies, same attempt and cycle budget.

    python scripts/blind_grading.py <directory> [--attempts N] [--only ARM]

It writes both arms and ingests the ticket into each. It does not run the loop;
the commands to do that are printed at the end.

`--attempts` overrides `loop.maxAttempts`, which is what decides whether a
stalled ticket reaches the machinery this experiment is for. The first run of
`arm-blind` spent all three of the fixture's attempts and landed on the third,
so nothing was requeued and `_measure_cycle` never ran. At two it fails the
cycle instead. `--only` builds a single arm, for re-running one side without
disturbing the other's database.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_workspace import SAMPLE, copy_sample  # noqa: E402

ARMS = {"arm-blind": False, "arm-shown": True}

# Below anything a model writes unprompted. Run 3 of `GRIND.md` established
# what this executor gets right with no config in front of it: every delivered
# file was clean at 79 columns and clean at 88. A limit it would satisfy by
# accident measures nothing, and the point is a rule that can only be met by
# reading the file naming it.
COLUMNS = 50

# The four committed root-build files are longer than that and are not the
# ticket's work. Grandfathering them keeps the baseline green, which is what
# every fixture run needs and also what a real repository adopting a stricter
# rule actually does.
GRANDFATHERED = (
    "wordcount/__init__.py",
    "wordcount/counter.py",
    "tests/__init__.py",
    "tests/counter_test.py",
)

# Cycles, not attempts, are what the machinery measures: `_measure_cycle` runs
# over the tickets eligible for a retry cycle, and `reviewWhenStuck` fires on
# the second *flat* one. The fixture ships `retryCycles: 1`, which cannot reach
# either rung however many attempts a ticket burns inside its first cycle.
RETRY_CYCLES = 4

LINT_CONFIG = """[flake8]
# Deliberately stricter than anything a model writes unprompted, and the whole
# subject of the experiment: in `arm-blind` this file is not in any prompt, so
# the executor is graded against a number it can only learn from failures.
max-line-length = {columns}

# `plugin/` is the other build, graded by its own command from its own
# directory. Without this a root-build ticket fails on a file it may not open.
exclude =
    .git,
    __pycache__,
    .hybridforge,
    plugin

# Pre-existing files, not the ticket's work. The baseline has to be green or
# the run reports the fixture's own debt as the ticket's.
per-file-ignores =
{ignores}
"""


def _ticket(spec: str, ticket_id: str) -> str:
    """The named ticket, lifted out of a multi-ticket spec.

    Read from `GRIND.md` rather than copied into this script so the experiment
    cannot drift from the ticket the earlier runs used — the comparison is only
    worth anything while it is the same work.
    """
    match = re.search(
        rf"^## {re.escape(ticket_id)}:.*?(?=^## |\Z)", spec, re.M | re.S
    )
    if not match:
        raise SystemExit(f"{ticket_id} not found in the spec")
    return match.group(0).rstrip() + "\n"


def _write_arm(
    root: Path, toolchain_context: bool, ticket: str, attempts: int | None = None
) -> None:
    config_path = root / ".hybridforge" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["loop"]["toolchainContext"] = toolchain_context
    config["loop"]["retryCycles"] = RETRY_CYCLES
    if attempts is not None:
        config["loop"]["maxAttempts"] = attempts
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    ignores = "\n".join(f"    {path}:E501" for path in GRANDFATHERED)
    (root / ".flake8").write_text(
        LINT_CONFIG.format(columns=COLUMNS, ignores=ignores), encoding="utf-8"
    )
    (root / "BLIND.md").write_text(
        "# The blind-grading arm\n\n"
        "One ticket, lifted from `GRIND.md` so it is the same work three\n"
        "earlier runs landed on the first attempt. What differs is whether\n"
        "`.flake8` reaches the prompt. See `docs/BLIND-GRADING.md`.\n\n"
        + ticket,
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the blind-grading arms.")
    parser.add_argument("directory")
    parser.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="override loop.maxAttempts (the fixture ships 3)",
    )
    parser.add_argument(
        "--only",
        choices=sorted(ARMS),
        default=None,
        help="build one arm rather than both",
    )
    args = parser.parse_args(argv)

    base = Path(args.directory)
    if base.exists():
        print(f"error: {base} already exists; name a path that does not")
        return 1

    spec = (SAMPLE / "GRIND.md").read_text(encoding="utf-8")
    ticket = _ticket(spec, "GR-001")

    from forge.cli import main as forge_main

    arms = {args.only: ARMS[args.only]} if args.only else dict(ARMS)
    base.mkdir(parents=True)
    for arm, toolchain_context in arms.items():
        root = base / arm
        copy_sample(root)
        _write_arm(root, toolchain_context, ticket, args.attempts)
        code = forge_main(["--root", str(root), "ingest", str(root / "BLIND.md")])
        if code:
            shutil.rmtree(base, ignore_errors=True)
            print(f"error: ingest failed for {arm}")
            return code

    print(f"\nWritten under {base}\n")
    print("Run them one at a time — they share a GPU and a dashboard port:\n")
    for arm in arms:
        print(f"  forge --root {base / arm} go")
    print(
        "\nThen compare: attempts, whether `_convergence` reported flat, which\n"
        "rung of the ladder ran, and what the blocked note says. The\n"
        "predictions are written down in docs/BLIND-GRADING.md; read them\n"
        "before the results rather than after."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
