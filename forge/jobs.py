"""Background work a host started, recorded so it outlives the call that began it.

A delegation runs for minutes against a local executor; a tool call that
blocked for that long would hold the host and trip client timeouts. So a job
runs on a worker thread of the process that started it — the MCP server, for
a host — and its row in `jobs` is how anything else learns how it went.

The row is the truth, the thread is not. A server that dies mid-job leaves a
row saying `running` for a process that no longer exists; `reap` finds those
and says so, and puts the ticket back where a host can pick it up, rather than
leaving a job that will never finish and a ticket nobody may revise.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from typing import Any, Callable

from .providers import ProviderError
from .servers import ServerError, pid_alive
from .state import TICKET_PENDING, TICKET_RUNNING, Store

RUNNING = "running"
DONE = "done"
ERROR = "error"
LOST = "lost"

# Jobs this process is running right now. A row naming this pid that is not in
# here belongs to an earlier process that happened to get the same pid.
_live: set[int] = set()
_live_lock = threading.Lock()


def create(store: Store, run_id: int, ticket_id: str, kind: str = "delegate") -> int:
    with store._write() as connection:
        cursor = connection.execute(
            "INSERT INTO jobs (run_id, ticket_id, kind, state, pid, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, ticket_id, kind, RUNNING, os.getpid(), time.time()),
        )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def finish(store: Store, job_id: int, state: str, result: dict[str, Any]) -> None:
    with store._write() as connection:
        connection.execute(
            "UPDATE jobs SET state = ?, result = ?, ended_at = ? WHERE id = ?",
            (state, json.dumps(result), time.time(), job_id),
        )


def get(store: Store, job_id: int) -> dict[str, Any] | None:
    row = store._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job: dict[str, Any] = {
        "job": row["id"],
        "ticket": row["ticket_id"],
        "kind": row["kind"],
        "state": row["state"],
        "seconds": round((row["ended_at"] or time.time()) - row["started_at"], 1),
    }
    if row["cancel"] and row["state"] == RUNNING:
        job["cancelling"] = True
    if row["state"] != RUNNING:
        job["result"] = json.loads(row["result"] or "{}")
    return job


def running_for(store: Store, run_id: int, ticket_id: str) -> int | None:
    row = store._connection.execute(
        "SELECT id FROM jobs WHERE run_id = ? AND ticket_id = ? AND state = ? "
        "ORDER BY id DESC LIMIT 1",
        (run_id, ticket_id, RUNNING),
    ).fetchone()
    return int(row["id"]) if row else None


def active(store: Store) -> list[dict[str, Any]]:
    rows = store._connection.execute(
        "SELECT id FROM jobs WHERE state = ? ORDER BY id", (RUNNING,)
    ).fetchall()
    return [job for row in rows if (job := get(store, int(row["id"]))) is not None]


def request_cancel(store: Store, job_id: int) -> None:
    with store._write() as connection:
        connection.execute("UPDATE jobs SET cancel = 1 WHERE id = ?", (job_id,))


def reap(store: Store) -> list[int]:
    """Mark jobs whose process is gone as lost, and free their tickets."""
    rows = store._connection.execute(
        "SELECT id, run_id, ticket_id, pid FROM jobs WHERE state = ?", (RUNNING,)
    ).fetchall()
    lost: list[int] = []
    for row in rows:
        pid = int(row["pid"])
        if pid == os.getpid():
            with _live_lock:
                if int(row["id"]) in _live:
                    continue
        elif pid_alive(pid):
            continue
        finish(
            store,
            int(row["id"]),
            LOST,
            {"status": "lost", "reason": "the process running this job ended before "
             "it finished; the ticket is back to pending and its attempts so far "
             "are in the step log"},
        )
        with store._write() as connection:
            connection.execute(
                "UPDATE tickets SET status = ? WHERE run_id = ? AND ticket_id = ? "
                "AND status = ?",
                (TICKET_PENDING, row["run_id"], row["ticket_id"], TICKET_RUNNING),
            )
        lost.append(int(row["id"]))
    return lost


def start(
    open_store: Callable[[], Any], job_id: int, work: Callable[[Any], dict]
) -> threading.Thread:
    """Run `work` on a worker thread and record how it ended.

    `open_store` builds the worker's own handle on the project — config and
    store — because a config is mutated per delegation and must not be shared
    with the caller's. Not a daemon thread: a process asked to exit mid-job
    waits for the step in hand rather than abandoning a half-applied patch.
    """

    def run() -> None:
        with _live_lock:
            _live.add(job_id)
        handle = None
        try:
            handle = open_store()
            result = work(handle)
            finish(handle.store, job_id, DONE, result)
        except (ProviderError, ServerError) as exc:
            # A model or server that is not there is an answer, not a crash:
            # the host needs the reason, not the stack that carried it.
            if handle is not None:
                finish(handle.store, job_id, ERROR, {
                    "status": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "hint": "forge_status with probe: true shows which seats answer",
                })
        except Exception as exc:  # noqa: BLE001 - a job records why it died
            if handle is not None:
                finish(handle.store, job_id, ERROR, {
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-3000:],
                })
        finally:
            with _live_lock:
                _live.discard(job_id)
            if handle is not None:
                handle.close()

    thread = threading.Thread(target=run, name=f"forge-job-{job_id}")
    thread.start()
    return thread


def wait(store: Store, job_id: int, seconds: float, poll: float = 0.5) -> dict[str, Any] | None:
    """The job, once it ends or `seconds` pass — whichever is first."""
    deadline = time.time() + max(0.0, seconds)
    while True:
        job = get(store, job_id)
        if job is None or job["state"] != RUNNING or time.time() >= deadline:
            return job
        time.sleep(poll)
