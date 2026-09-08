"""Join each sign-off vote to its own note, and ask what its reasoning bought.

`scripts/ratify_cost.py` found that sign-off is the largest line in either
evidence tree, and `scripts/ratify_value.py` found that most of a vote's output
budget goes on reasoning before a four-line verdict. The obvious remedy --
turning reasoning off for the vote -- assumes something nobody had checked:
that the reasoning is not what finds the defects. Deciding whether a ticket can
be built is a judgement, and a judgement is the last place to remove thinking
from on a cost argument alone.

So this joins every vote *call* to the note that call produced. Within a pass
the roles vote in `ratify_order` and the calls are recorded in that order, with
one planner revision between passes, so the two line up positionally and the
join is checked on the role name rather than assumed.

What it reports is whether a vote that spent more reasoning said more. Two
groups matter, and the second is the one with teeth: a vote that stopped on its
own reached a conclusion, while a vote that ran to its allotment did not -- it
emitted its verdict because it was out of room. If objections come only from
the first group, then reasoning to the ceiling is not thinking harder about a
hard ticket, and a smaller allowance costs nothing the record can see.

It stays correlational, which is the whole of what an artifact tree can say. A
model given less room might conclude sooner and miss something real, and votes
that never had less room cannot show that. The experiment that would settle it
is the one in `docs/BLIND-GRADING.md`: one ticket, one variable, objections
counted on both arms.

Reads artifacts only. Nothing here touches a database or a running loop.

    python scripts/vote_cost.py <tree> [<tree> ...]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# A vote whose completion count is one of these stopped because it ran out of
# reasoning room rather than because it had finished: the value repeats across
# unrelated tickets and prompts, which a count of generated text does not do.
# Derived from the data rather than configured -- see `allotments`.
ALLOTMENT_REPEATS = 4


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def segments(attempt: Path) -> list[tuple[dict, list[tuple[Path, dict]]]]:
    """Each sign-off record in the attempt, with the calls that belong to it."""
    found: list[tuple[dict, list[tuple[Path, dict]]]] = []
    pending: list[tuple[Path, dict]] = []
    for path in sorted(attempt.glob("*.json")):
        record = _load(path)
        step = record.get("step") or ""
        if step.startswith("ratify-"):
            pending.append((path, record))
        elif step == "ratify":
            found.append((record, pending))
            pending = []
    return found


def votes(root: Path) -> list[dict]:
    """Every vote whose call could be matched to the note it produced.

    A pass is `len(roles)` votes; passes are separated by one planner revision.
    An attempt whose call count does not match that shape is skipped rather
    than aligned on a guess -- a join off by one would attribute every reply to
    the wrong role.
    """
    found: list[dict] = []
    for attempt in sorted(p for p in root.rglob("attempt-*") if p.is_dir()):
        for record, calls in segments(attempt):
            notes = record.get("notes") or []
            passes = int(record.get("passes") or 0)
            roles = len([note for note in notes if note.get("pass") == 1])
            if not roles or len(calls) != passes * roles + (passes - 1):
                continue
            for number in range(1, passes + 1):
                start = (number - 1) * roles + (number - 1)
                pass_notes = [note for note in notes if note.get("pass") == number]
                for (path, call), note in zip(calls[start:start + roles], pass_notes):
                    if call.get("role") != note.get("role"):
                        continue
                    usage = call.get("usage") or {}
                    found.append(
                        {
                            "path": path,
                            "role": str(note.get("role") or ""),
                            "tokens": int(usage.get("completion_tokens") or 0),
                            "signed": bool(note.get("signed")),
                            "points": len(note.get("blocking") or [])
                            + len(note.get("suggestions") or []),
                        }
                    )
    return found


def allotments(found: list[dict]) -> set[int]:
    """Completion counts that repeat often enough to be a ceiling, not a length.

    A model that stops when it has finished produces a different number every
    time. One that stops because it is out of room produces the same number
    across unrelated tickets, so the repeated values are the ceilings without
    anyone having to know what they were configured to be.
    """
    counts = Counter(vote["tokens"] for vote in found if vote["tokens"])
    return {value for value, seen in counts.items() if seen >= ALLOTMENT_REPEATS}


def mean(values: list[int]) -> str:
    return f"{sum(values) // len(values):,}" if values else "-"


def group(found: list[dict], name: str, keep) -> None:
    chosen = [vote for vote in found if keep(vote)]
    tokens = [vote["tokens"] for vote in chosen]
    median = f"{sorted(tokens)[len(tokens) // 2]:,}" if tokens else "-"
    print(f"  {name:<36} n={len(chosen):>3}  mean {mean(tokens):>7}  median {median:>7}")


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv if not arg.startswith("--")]
    if not roots:
        print(__doc__)
        return 2

    found: list[dict] = []
    for root in roots:
        if not root.exists():
            print(f"missing: {root}")
            return 1
        found.extend(votes(root))

    if not found:
        print("no sign-off votes could be joined to their notes")
        return 1

    print(f"votes joined to their own note: {len(found)}\n")
    print("what a vote said, against what its reasoning cost:")
    group(found, "raised a blocking point or suggestion", lambda v: v["points"])
    group(found, "said NONE to both", lambda v: not v["points"])
    group(found, "refused to sign", lambda v: not v["signed"])
    group(found, "signed", lambda v: v["signed"])

    ceilings = allotments(found)
    capped = [vote for vote in found if vote["tokens"] in ceilings]
    ended = [vote for vote in found if vote["tokens"] not in ceilings]
    print(f"\nreasoning allotments found in the data: {sorted(ceilings)}")
    for name, chosen in (("ran to the allotment", capped), ("stopped on its own", ended)):
        if not chosen:
            continue
        raised = sum(1 for vote in chosen if vote["points"])
        refused = sum(1 for vote in chosen if not vote["signed"])
        print(
            f"  {name:<22} n={len(chosen):>3}   raised something {raised:>3} "
            f"({100 * raised / len(chosen):3.0f}%)   refused {refused:>3} "
            f"({100 * refused / len(chosen):3.0f}%)"
        )

    print("\nper role -- the mean is not the same story in every seat:")
    for role in sorted({vote["role"] for vote in found}):
        seat = [vote for vote in found if vote["role"] == role]
        raised = [vote["tokens"] for vote in seat if vote["points"]]
        silent = [vote["tokens"] for vote in seat if not vote["points"]]
        print(
            f"  {role:<9} raised n={len(raised):>2} mean {mean(raised):>7}   "
            f"silent n={len(silent):>2} mean {mean(silent):>7}"
        )

    empty = [vote for vote in found if not vote["signed"] and not vote["points"]]
    if empty:
        print(
            f"\nrefusals naming nothing: {len(empty)} -- `resolve` counts the vote "
            f"against the ticket, but the revision is built from the blocking "
            f"points, so the planner is handed a no with nothing to fix:"
        )
        for vote in empty:
            print(f"  {vote['path']}  ({vote['role']}, {vote['tokens']:,} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
