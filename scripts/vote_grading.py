"""Build the two arms that ask what a sign-off vote's reasoning is worth.

`scripts/vote_cost.py` found that of 49 votes which ran to their reasoning
allotment none raised a single objection, while 27 of the 47 that stopped on
their own did. That is correlational and cannot settle the question it raises:
votes that never had less room cannot show what less room would have cost. So
this changes one variable and counts objections on both arms.

    arm-thinks    the fixture as it ships, and the control for both others
    arm-blurts    loop.voteThinking = false      the vote does not reason
    arm-revotes   loop.reviseBetweenPasses = false   nothing is rewritten

Everything else is identical -- same ticket, same models, same budgets, same
`ratifyPasses`. `arm-blurts` moves the *vote* alone: the planner's revision
keeps its reasoning, which the per-model `reasoning-budget` flag could not have
arranged, because planner and reviewer share a server with every other call
they make.

`arm-revotes` is the other half of the same question, and it was added after
`arm-blurts` answered. The second pass changed a ticket's outcome three times
in the record, and all three times the planner's revision had *failed* -- so
the roles voted again on words nobody had touched, and changed their minds
anyway. That makes the revision, which is the most expensive call in the pass
and failed in three of eight, a thing whose cost has never been separated from
the second vote's. This arm separates them: the pass repeats, the objections
still travel to the next vote, and only the rewrite is gone.

Read them as two independent comparisons against the same control, not as a
ladder. `arm-blurts` asks whether a vote needs to reason. `arm-revotes` asks
whether a second vote needs a revision in front of it.

**The ticket is chosen so the right answer is already known.** `GR-002` in
`examples/sample-project/GRIND.md` was written to be jointly impossible and is
not, and what makes it the right fixture is what the loop did with it twice:
all four roles refused to sign on pass 1, each naming the same defect -- each
`1/3` share rounds to `33`, so the displayed percentages sum to `99` -- and the
planner repaired the spec. Both runs, same defect, same repair. So a vote that
raises it is right and a vote that signs the ticket off has missed something
this fixture is known to contain.

Which makes the measurement a count rather than a judgement:

    did each role refuse to sign on pass 1?
    did its objection name the rounding defect?
    did the pass still end up repairing the spec?

`arm-blurts` losing any of those is the answer the roadmap entry says it cannot
currently get. `arm-blurts` keeping all of them, at a mean of a few hundred
completion tokens a vote instead of several thousand, is the other answer.

`arm-revotes` is read on the third question rather than the first two, because
its votes reason normally and pass 1 should be the control's pass 1. What was
open is what the *second* vote does with an objection nobody acted on.

Run on 2026-09-08: all four roles refused again on unchanged text, two of them
rejecting the standing position explicitly rather than the ticket. Nothing
reached a build call, the ticket parked, and sign-off cost 47% more than the
arm that revised -- a ticket nobody repairs cycles through the pass instead of
leaving it. So the revision is what turns a correct refusal into a shippable
ticket, and these roles do not flip merely for being asked twice.

    python scripts/vote_grading.py <directory> [--ticket GR-002] [--only ARM]

It writes every arm and ingests the ticket into each. It does not run the loop;
the commands to do that are printed at the end. Run them one at a time -- they
share a GPU.

Read `scripts/vote_cost.py`'s output on the finished runs rather than counting
by eye: it joins each vote to the note that vote produced, which is the whole
measurement.
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

# Each arm is the loop settings that differ from the fixture's, one variable at
# a time against `arm-thinks` -- which is the fixture as it ships, and the
# control for both of the others.
ARMS: dict[str, dict[str, object]] = {
    "arm-thinks": {"voteThinking": True, "reviseBetweenPasses": True},
    "arm-blurts": {"voteThinking": False, "reviseBetweenPasses": True},
    "arm-revotes": {"voteThinking": True, "reviseBetweenPasses": False},
}

# The defect both earlier runs of this ticket found, and the string a vote's
# objection has to be about for the arm to have kept what it is being asked to
# keep. Not matched mechanically -- a model will phrase it differently -- but
# recorded here so the reader is comparing against what actually happened
# rather than against a memory of it.
KNOWN_DEFECT = "each 1/3 share rounds to 33, so the displayed percentages sum to 99"


def _ticket(spec: str, ticket_id: str) -> str:
    """The named ticket, lifted out of a multi-ticket spec.

    Read from `GRIND.md` rather than copied into this script so the experiment
    cannot drift from the ticket the earlier runs used -- the comparison is
    only worth anything while it is the same work.
    """
    match = re.search(rf"^## {re.escape(ticket_id)}:.*?(?=^## |\Z)", spec, re.M | re.S)
    if not match:
        raise SystemExit(f"{ticket_id} not found in the spec")
    return match.group(0).rstrip() + "\n"


def _write_arm(root: Path, settings: dict[str, object], ticket: str) -> None:
    config_path = root / ".hybridforge" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["loop"].update(settings)
    # Both passes, because the measurement is what pass 1 raised *and* whether
    # the repair still happened. At one pass a refusal parks the ticket and the
    # second half of the question goes unasked.
    config["loop"]["ratifyPasses"] = 2
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    (root / "VOTE.md").write_text(
        "# The vote-reasoning arm\n\n"
        "One ticket, lifted from `GRIND.md` so it is the same work two earlier\n"
        "runs put to the roles. Both times all four refused to sign it on pass\n"
        "one, each naming the same defect:\n\n"
        f"> {KNOWN_DEFECT}\n\n"
        "What differs between the arms is whether a vote may reason before it\n"
        "answers. See the *Sign-off cost* entry in `docs/ROADMAP.md`.\n\n" + ticket,
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the vote-reasoning arms.")
    parser.add_argument("directory")
    parser.add_argument(
        "--ticket",
        default="GR-002",
        help="which GRIND.md ticket to put to the roles (default GR-002, the "
        "one whose defect both earlier runs found)",
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
    ticket = _ticket(spec, args.ticket)

    from forge.cli import main as forge_main

    arms = {args.only: ARMS[args.only]} if args.only else dict(ARMS)
    base.mkdir(parents=True)
    for arm, settings in arms.items():
        root = base / arm
        copy_sample(root)
        _write_arm(root, settings, ticket)
        code = forge_main(["--root", str(root), "ingest", str(root / "VOTE.md")])
        if code:
            shutil.rmtree(base, ignore_errors=True)
            print(f"error: ingest failed for {arm}")
            return code

    print(f"\nWritten under {base}\n")
    print("Run them one at a time -- they share a GPU and a dashboard port:\n")
    for arm in arms:
        print(f"  forge --root {base / arm} go")
    print(
        "\nThen read both arms with:\n\n"
        + "".join(
            f"  python scripts/vote_cost.py {base / arm}/.hybridforge/artifacts\n"
            for arm in arms
        )
        + "\nThe question is not which arm was cheaper -- that is known before\n"
        "the run. It is whether `arm-blurts` still refused to sign on pass one\n"
        "and still named the rounding defect.\n\n"
        "Run once already, on 2026-09-08, and it did neither: all four roles\n"
        "signed off at a mean of 20 completion tokens a vote, the defect\n"
        "reached the build as `AssertionError: 99 != 100`, and the arm cost\n"
        "73,040 tokens more overall than the one whose votes reasoned. The\n"
        "write-up is the *Sign-off cost* entry in docs/ROADMAP.md. Re-running\n"
        "this against other models is why the script is kept; write your own\n"
        "prediction down first either way."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
