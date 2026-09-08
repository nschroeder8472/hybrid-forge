"""Replay the recorded sign-off passes and price `loop.ratifyPasses`.

`scripts/review_cost.py` found that the paid role spends most of its tokens on
sign-off rather than on the review step, and that the larger half of the bill
is an existing config knob rather than machinery anybody has to build.
`docs/CONFIG.md` calls `ratifyPasses: 2` "a judgement about the cost of the
failure it prevents", which is exactly the shape of claim this repository
replays rather than argues.

So this asks what the second pass bought. The counterfactual is computable
rather than estimated: a pass is every role voting, `ratify.resolve` decides
the outcome from those votes alone, and a unanimous pass returns before any
revision happens. Running the production `resolve` over the recorded pass-1
votes therefore *is* what `ratifyPasses: 1` would have produced for that
ticket, and the recorded status is what it actually produced.

Four outcomes matter, and only two of them are worth paying for:

    rescued   pass 1 would have parked the ticket; the revision saved it
    caught    pass 1 would have shipped it; a later pass blocked it
    same      the outcome never moved, and the passes bought text only
    n/a       the pass never reached a second vote

Cost is attributed structurally. Within an attempt the sign-off calls appear in
order -- one vote per role, then the planner's revision, then the next pass --
so everything after the first `len(roles)` calls is what `ratifyPasses: 1`
would not have spent. One attempt can hold more than one sign-off pass, because
a respec moves the contract and the ticket is put to the roles again, so calls
are windowed between consecutive `ratify` step records rather than read from
the directory as a whole. An attempt whose call count still does not match that
shape is reported rather than guessed at.

Reads artifacts only. Nothing here touches a database or a running loop.

    python scripts/ratify_cost.py <tree> [<tree> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.ratify import UNAVAILABLE, Vote, resolve  # noqa: E402

SHIPS = ("unanimous", "majority", "split")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def votes_of(notes: list[dict], pass_number: int) -> list[Vote]:
    """The recorded votes of one pass, as `resolve` wants them.

    `Vote.as_note` does not record `error`, so a role the provider could not
    reach replays as an unsigned vote rather than as an outage. The recorded
    status names the outages, and they are excluded rather than replayed.
    """
    return [
        Vote(
            role=note.get("role") or "",
            signed=bool(note.get("signed")),
            blocking=list(note.get("blocking") or []),
            suggestions=list(note.get("suggestions") or []),
        )
        for note in notes
        if note.get("pass") == pass_number
    ]


def segments(attempt: Path) -> list[tuple[Path, dict, list[dict]]]:
    """Each sign-off pass in the attempt, with the calls that belong to it.

    A respec puts the ticket to the roles again, so one attempt can hold two
    `ratify` step records. Reading the directory as a whole would charge the
    first pass for the second one's calls, so each record takes the calls made
    since the previous record and no others.
    """
    found: list[tuple[Path, dict, list[dict]]] = []
    pending: list[dict] = []
    for path in sorted(attempt.glob("*.json")):
        record = _load(path)
        step = record.get("step") or ""
        if step.startswith("ratify-"):
            pending.append(record)
        elif step == "ratify":
            found.append((path, record, pending))
            pending = []
    if pending:
        # Votes with no step record after them: the run stopped mid-pass. Real
        # tokens that no completed pass can be charged for, so they are handed
        # back under a null record and reported rather than dropped.
        found.append((attempt, {}, pending))
    return found


def observe(path: Path, record: dict, calls: list[dict]) -> dict | None:
    """One row per recorded sign-off pass: what it decided, and what it cost."""
    notes = record.get("notes") or []
    passes = int(record.get("passes") or 0)
    if not notes or passes < 1:
        return None

    first = votes_of(notes, 1)
    roles = len(first)
    tokens = [int((call.get("usage") or {}).get("total_tokens") or 0) for call in calls]

    # One vote per role per pass, plus one planner revision between passes.
    expected = passes * roles + (passes - 1)
    first_pass_tokens = sum(tokens[:roles])
    later = sum(tokens[roles:]) if len(tokens) == expected else 0

    status = record.get("status") or ""
    at_one = resolve(first) if first else UNAVAILABLE
    if status == UNAVAILABLE or at_one == UNAVAILABLE:
        outcome = "unavailable"
    elif passes < 2:
        outcome = "n/a"
    elif at_one in SHIPS and status not in SHIPS:
        outcome = "caught"
    elif at_one not in SHIPS and status in SHIPS:
        outcome = "rescued"
    else:
        outcome = "same"

    return {
        "path": path,
        "ticket": record.get("ticket") or "",
        "roles": roles,
        "passes": passes,
        "status": status,
        "at_one": at_one,
        "outcome": outcome,
        "changed": list(record.get("changed") or []),
        "first_pass_tokens": first_pass_tokens,
        "later_tokens": later,
        "calls": len(calls),
        "expected_calls": expected,
    }


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv if not arg.startswith("--")]
    if not roots:
        print(__doc__)
        return 2

    rows: list[dict] = []
    abandoned: list[tuple[Path, list[dict]]] = []
    for root in roots:
        if not root.exists():
            print(f"missing: {root}")
            return 1
        for attempt in sorted(p for p in root.rglob("attempt-*") if p.is_dir()):
            for path, record, calls in segments(attempt):
                if not record:
                    abandoned.append((path, calls))
                    continue
                row = observe(path, record, calls)
                if row:
                    rows.append(row)

    if not rows:
        print("no recorded sign-off passes found")
        return 1

    multi = [row for row in rows if row["passes"] > 1]
    first_tokens = sum(row["first_pass_tokens"] for row in rows)
    later_tokens = sum(row["later_tokens"] for row in rows)
    total = first_tokens + later_tokens

    print(f"sign-off passes replayed: {len(rows)}")
    print(f"  reached a second pass:  {len(multi)}")
    if abandoned:
        stranded = sum(
            int((call.get("usage") or {}).get("total_tokens") or 0)
            for _, calls in abandoned
            for call in calls
        )
        print(
            f"  stopped mid-pass:       {len(abandoned)} "
            f"({stranded:,} tokens, charged to no pass)"
        )
    unattributed = [row for row in rows if row["calls"] != row["expected_calls"]]
    if unattributed:
        print(f"  call count unexplained: {len(unattributed)} (cost not attributed)")
        for row in unattributed[:5]:
            print(
                f"    {row['path']}: {row['calls']} call(s), "
                f"expected {row['expected_calls']}"
            )

    print("\ntokens:")
    print(f"  pass 1            {first_tokens:>12,}  ({100 * first_tokens / total:4.1f}%)")
    print(f"  revision + later  {later_tokens:>12,}  ({100 * later_tokens / total:4.1f}%)")
    print(f"  total             {total:>12,}")
    print(f"\n  `ratifyPasses: 1` would not have spent {later_tokens:,} of them.")

    print("\nwhat the later passes bought:")
    for name in ("rescued", "caught", "same", "unavailable", "n/a"):
        hits = [row for row in rows if row["outcome"] == name]
        if not hits:
            continue
        tokens = sum(row["later_tokens"] for row in hits)
        print(f"  {name:<12} {len(hits):>3}   {tokens:>12,} tokens")

    print("\nevery pass that reached a second vote:")
    print(
        f"  {'ticket':<10} {'at 1':<10} {'final':<10} {'outcome':<10} "
        f"{'later tok':>10}  changed"
    )
    for row in multi:
        print(
            f"  {row['ticket']:<10} {row['at_one']:<10} {row['status']:<10} "
            f"{row['outcome']:<10} {row['later_tokens']:>10,}  "
            f"{', '.join(row['changed']) or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
