"""Follow the sign-off passes into the attempts that came after them.

`scripts/ratify_cost.py` priced the second pass and left one question open:
`same` is not the same as `wasted`, because a pass that never moved the outcome
still rewrote the ticket, and a revised contract that ships anyway may build
better. Deciding that means leaving the sign-off record, which is what this
does -- it joins each pass to the attempts the ticket then spent.

It answers a second question the cost replay could not see at all. `_revise`
returns nothing and logs a warning when the planner's reply cannot be parsed or
runs out of output room, and the next pass then votes on *unchanged* text. A
sign-off record whose `changed` list is empty does not distinguish that from a
planner who read the objections and chose to change nothing, so the failures
are read from the run log where they are named.

Tickets are grouped by what actually happened between the votes:

    settled     pass 1 was unanimous; no revision, no second pass
    revised     a second pass voted on text the planner had rewritten
    re-voted    a second pass voted on the same text, the revision having failed

Reads a run database and its artifacts. Writes nothing.

    python scripts/ratify_value.py <workspace> [<workspace> ...]

A workspace is a directory holding `.hybridforge/run.db` and
`.hybridforge/artifacts`, or the `.hybridforge` directory itself.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SHIPPED = ("done",)
REVISION_FAILED = "could not revise the ticket"


def _hybridforge(root: Path) -> Path:
    return root if root.name == ".hybridforge" else root / ".hybridforge"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def failed_revisions(database: Path) -> set[tuple[int, str]]:
    """`(run, ticket)` for every pass whose planner could not revise.

    Read from the log rather than inferred from an empty `changed` list, which
    a planner that revised nothing produces just as well.
    """
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT run_id, message FROM events WHERE message LIKE ?",
        (f"%{REVISION_FAILED}%",),
    ).fetchall()
    connection.close()
    return {
        (int(row["run_id"]), row["message"].split(":", 1)[0].strip())
        for row in rows
        if ":" in row["message"]
    }


def tickets(database: Path) -> list[dict]:
    """Every ticket that reached a sign-off pass, with what it did afterwards.

    Empty for a database written before sign-off existed: the columns are
    absent rather than null, and an older run is not evidence about a pass it
    never had.
    """
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tickets)")}
    if not {"ratify_status", "ratify_passes"} <= columns:
        connection.close()
        return []
    rows = connection.execute(
        "SELECT run_id, ticket_id, status, attempts, ratify_status, ratify_passes "
        "FROM tickets WHERE ratify_passes > 0 ORDER BY run_id, ticket_id"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def after_sign_off(artifacts: Path, run_id: int, ticket_id: str) -> tuple[int, int]:
    """Tokens and model calls the ticket spent on everything but sign-off."""
    tokens = 0
    calls = 0
    for path in (artifacts / f"run-{run_id}" / ticket_id).rglob("*.json"):
        record = _load(path)
        step = record.get("step") or ""
        if step.startswith("ratify"):
            continue
        spent = int((record.get("usage") or {}).get("total_tokens") or 0)
        if spent:
            tokens += spent
            calls += 1
    return tokens, calls


def sign_offs(artifacts: Path, run_id: int, ticket_id: str) -> tuple[int, list[str]]:
    """The most votes any of the ticket's passes took, and what they rewrote.

    Read from the artifacts rather than from `tickets.ratify_passes`, which
    holds only the last sign-off: a ticket a respec has moved goes back to the
    roles, and the second record overwrites the first. The ticket that blocked
    after two passes and then passed a fresh one in a single pass reads as
    `settled` from the database and is nothing of the kind.
    """
    most = 0
    changed: list[str] = []
    for path in sorted((artifacts / f"run-{run_id}" / ticket_id).rglob("*-ratify.json")):
        record = _load(path)
        most = max(most, int(record.get("passes") or 0))
        for name in record.get("changed") or []:
            if name not in changed:
                changed.append(name)
    return most, changed


def observe(workspace: Path) -> list[dict]:
    """One row per ticket that was put to the roles, in run order."""
    forge = _hybridforge(workspace)
    database = forge / "run.db"
    artifacts = forge / "artifacts"
    if not database.exists():
        return []

    failed = failed_revisions(database)
    rows = []
    for ticket in tickets(database):
        run_id = int(ticket["run_id"])
        ticket_id = str(ticket["ticket_id"])
        passes, changed = sign_offs(artifacts, run_id, ticket_id)
        passes = passes or int(ticket["ratify_passes"] or 0)
        revision_failed = (run_id, ticket_id) in failed
        if passes < 2:
            group = "settled"
        elif revision_failed:
            group = "re-voted"
        else:
            group = "revised"
        tokens, calls = after_sign_off(artifacts, run_id, ticket_id)
        rows.append(
            {
                "workspace": workspace.name,
                "run": run_id,
                "ticket": ticket_id,
                "group": group,
                "passes": passes,
                "ratify_status": ticket["ratify_status"] or "",
                "status": ticket["status"] or "",
                "attempts": int(ticket["attempts"] or 0),
                "changed": changed,
                "after_tokens": tokens,
                "after_calls": calls,
            }
        )
    return rows


def summarise(rows: list[dict], name: str) -> None:
    """What one group did after sign-off, over the tickets that shipped."""
    if not rows:
        print(f"  {name:<10} no tickets")
        return
    shipped = [row for row in rows if row["status"] in SHIPPED]
    built = [row for row in shipped if row["attempts"]]
    attempts = sum(row["attempts"] for row in built)
    tokens = sum(row["after_tokens"] for row in built)
    mean_attempts = f"{attempts / len(built):.2f}" if built else "-"
    mean_tokens = f"{tokens // len(built):,}" if built else "-"
    print(
        f"  {name:<10} {len(rows):>2} ticket(s), {len(shipped):>2} shipped, "
        f"{len(built):>2} built   mean attempts {mean_attempts:>5}   "
        f"mean tokens after sign-off {mean_tokens:>10}"
    )


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv if not arg.startswith("--")]
    if not roots:
        print(__doc__)
        return 2

    rows: list[dict] = []
    for root in roots:
        if not root.exists():
            print(f"missing: {root}")
            return 1
        found = observe(root)
        if not found:
            print(f"no ratified tickets in {root} (a run.db predating sign-off?)")
        rows.extend(found)

    if not rows:
        print("nothing to replay")
        return 1

    print(f"tickets put to the roles: {len(rows)}\n")
    print("what happened after sign-off:")
    for name in ("settled", "revised", "re-voted"):
        summarise([row for row in rows if row["group"] == name], name)

    print("\nevery ticket, in run order:")
    print(
        f"  {'run':>3} {'ticket':<9} {'group':<9} {'sign-off':<10} {'status':<8} "
        f"{'att':>3} {'after tok':>10}  changed"
    )
    for row in rows:
        print(
            f"  {row['run']:>3} {row['ticket']:<9} {row['group']:<9} "
            f"{row['ratify_status']:<10} {row['status']:<8} {row['attempts']:>3} "
            f"{row['after_tokens']:>10,}  {', '.join(row['changed']) or '-'}"
        )

    revoted = [row for row in rows if row["group"] == "re-voted"]
    if revoted:
        print(
            f"\n{len(revoted)} of {len(rows)} sign-off passes voted twice on the same "
            f"text, the planner's revision having failed."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
