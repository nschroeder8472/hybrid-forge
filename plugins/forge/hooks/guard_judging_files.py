"""PreToolUse guard: the host does not edit the files that judge delegated work.

LOOP-INVARIANTS section 3 — the party that benefits does not rule — applied to
the host. A host wants its tickets to land, so it may not:

- edit any file a ticket is writing while that ticket's job is running (the
  delegate's apply would race it, and the diff the reviewer reads would carry
  the host's edit as the delegate's), or
- edit a test file inside the scope of a delegated ticket that has not landed.
  That file encodes the criteria the ticket is judged by; changing it is
  grading the work. Revise the contract (`forge_contract`) or settle the ticket
  (`forge_close`) first.

"Instruction-following is not an access control" holds for a frontier model
too, which is why this is a hook and not a sentence in a skill.

Standard library only and read-only on the database, so it is fast, cannot
fail because forge is not importable from this Python, and cannot change a
run. Anything it cannot read allows the edit: a guard that blocks every edit
in a project with no forge state would be uninstalled, not obeyed.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import sys
from pathlib import Path

TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|[._-](test|spec)\.[a-z0-9]+$|(^|/)test_[^/]+$"
)

# Statuses after which a ticket's tests are no longer judging anything.
SETTLED = ("done", "skipped", "withheld", "split")


def find_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".hybridforge" / "run.db").is_file():
            return candidate
    return None


def matches(path: str, patterns: list[str]) -> bool:
    return any(path == p or fnmatch.fnmatchcase(path, p) for p in patterns)


def verdict(payload: dict) -> str:
    """The reason to deny this edit, or "" to allow it."""
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        return ""
    cwd = Path(payload.get("cwd") or ".").resolve()
    absolute = (cwd / target).resolve()
    root = find_root(absolute.parent) or find_root(cwd)
    if root is None:
        return ""
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError:
        return ""

    connection = None
    try:
        database = (root / ".hybridforge" / "run.db").as_uri()
        connection = sqlite3.connect(f"{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        host = connection.execute("SELECT value FROM control WHERE key = 'host_run'").fetchone()
        if host is None:
            return ""
        run_id = int(host["value"])
        running = {
            row["ticket_id"]: int(row["id"])
            for row in connection.execute(
                "SELECT id, ticket_id FROM jobs WHERE run_id = ? AND state = 'running'",
                (run_id,),
            )
        }
        tickets = connection.execute(
            "SELECT ticket_id, status, route, allowed_files FROM tickets WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    except (sqlite3.Error, ValueError):
        return ""
    finally:
        if connection is not None:
            connection.close()

    for ticket in tickets:
        try:
            scope = [str(p) for p in json.loads(ticket["allowed_files"] or "[]")]
        except ValueError:
            continue
        if not matches(relative, scope):
            continue
        ticket_id = ticket["ticket_id"]
        if ticket_id in running:
            return (
                f"{relative} is in the scope of {ticket_id}, which job "
                f"{running[ticket_id]} is working right now. Wait for it "
                f"(forge_job) or cancel it (forge_job cancel: true) first."
            )
        if (
            TEST_PATH.search(relative)
            and ticket["status"] not in SETTLED
            and not str(ticket["route"]).startswith("withheld")
        ):
            return (
                f"{relative} is a test in the scope of {ticket_id} "
                f"({ticket['status']}), so it judges that ticket's work. The "
                f"host does not edit the assertion a delegated ticket is graded "
                f"by. Revise the criteria with forge_contract, or settle the "
                f"ticket with forge_close, then edit."
            )
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = verdict(payload)
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"forge guard: {reason}",
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
