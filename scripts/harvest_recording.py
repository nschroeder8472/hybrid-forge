"""Lift a run's real step output into a fixture the suite can replay.

`docs/LOOP-INVARIANTS.md` §9: *a unit test asserts what you thought the output
looked like; the artifacts hold what it actually was.* Two of the repairs it
was written from were wrong on the first attempt and only replay caught it.

The blind-grading runs made the same point again. Every test in the suite fed
the failure parser hand-written strings like `src/a.ts(4,1): error TS2532: x`,
which parse correctly — while a real `flake8` run parsed to nothing at all, so
`signatures()` returned the empty set and both of its callers read that as *no
errors*. Synthetic fixtures had passed the whole time.

So recordings are harvested rather than typed:

    python scripts/harvest_recording.py <run.db> <name> [--steps 11,14,15]

It writes `tests/recordings/<name>.json` holding the chosen steps verbatim —
name, status and the exact `detail` the tool produced. With no `--steps` it
takes every failed step plus every step whose output was empty, which is the
pair that matters: what a tool says when it is unhappy, and the reply that says
nothing at all.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

RECORDINGS = Path(__file__).resolve().parent.parent / "tests" / "recordings"


def harvest(database: Path, steps: list[int] | None = None) -> list[dict]:
    """The chosen steps, or every failed and every empty one, verbatim."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id, ticket_id, name, status, detail FROM steps ORDER BY id"
    ).fetchall()
    connection.close()

    if steps is not None:
        chosen = [row for row in rows if row["id"] in set(steps)]
        missing = set(steps) - {row["id"] for row in chosen}
        if missing:
            raise SystemExit(f"no such step(s) in {database}: {sorted(missing)}")
    else:
        chosen = [
            row
            for row in rows
            if row["status"] == "failed" or not (row["detail"] or "")
        ]

    return [
        {
            "step": row["id"],
            "ticket": row["ticket_id"] or "",
            "name": row["name"],
            "status": row["status"],
            "detail": row["detail"] or "",
        }
        for row in chosen
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", type=Path, help="a run's .hybridforge/run.db")
    parser.add_argument("name", help="what to call the recording")
    parser.add_argument(
        "--steps",
        default=None,
        help="comma-separated step ids; default is every failed and empty step",
    )
    parser.add_argument(
        "--note",
        default="",
        help="one line on what this run was and why it is worth keeping",
    )
    args = parser.parse_args(argv)

    if not args.database.is_file():
        print(f"error: no database at {args.database}")
        return 1

    steps = (
        [int(value) for value in args.steps.split(",") if value.strip()]
        if args.steps
        else None
    )
    captured = harvest(args.database, steps)
    if not captured:
        print("error: nothing to record — no failed or empty steps")
        return 1

    RECORDINGS.mkdir(parents=True, exist_ok=True)
    target = RECORDINGS / f"{args.name}.json"
    target.write_text(
        json.dumps({"note": args.note, "steps": captured}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{target} — {len(captured)} step(s)")
    for entry in captured:
        summary = " ".join(entry["detail"].split())[:70]
        print(f"  {entry['step']:>3} {entry['name']:<16} {entry['status']:<7} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
