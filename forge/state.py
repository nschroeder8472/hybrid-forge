"""SQLite-backed run state.

State lives on disk rather than in a conversation, and that is the whole reason
the loop can be autonomous. A daemon that is killed, a host that reboots, a
context window that fills — none of them lose the backlog, because none of them
were holding it. Restarting picks up at the last committed step.

One file per repository: `.hybridforge/run.db`. It is deliberately excluded
from git — it is a mutable log, not a reviewable artifact. The reviewable
artifacts are the tickets, which stay as markdown.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL,
    goal         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    ticket_id     TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    route         TEXT NOT NULL DEFAULT 'delegate',
    status        TEXT NOT NULL DEFAULT 'pending',
    position      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    spec          TEXT NOT NULL DEFAULT '',
    allowed_files TEXT NOT NULL DEFAULT '[]',
    criteria      TEXT NOT NULL DEFAULT '[]',
    context       TEXT NOT NULL DEFAULT '',
    blocked_note  TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL,
    UNIQUE(run_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    ticket_id  TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL DEFAULT 'info',
    kind     TEXT NOT NULL DEFAULT 'log',
    message  TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    model             TEXT NOT NULL,
    ts                REAL NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS control (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run  ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model, ts);
CREATE INDEX IF NOT EXISTS idx_steps_run   ON steps(run_id, id);
"""

# Run-level states. WAITING_BUDGET is the one that matters for unattended
# operation: it means "not stuck, not failed, just early" and the daemon will
# resume on its own.
RUN_IDLE = "idle"
RUN_PLANNING = "planning"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_WAITING_BUDGET = "waiting_budget"
RUN_BLOCKED = "blocked"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_STOPPED = "stopped"

TICKET_PENDING = "pending"
TICKET_RUNNING = "running"
TICKET_DONE = "done"
TICKET_BLOCKED = "blocked"
TICKET_FAILED = "failed"
TICKET_SKIPPED = "skipped"


@dataclass
class Ticket:
    ticket_id: str
    title: str = ""
    route: str = "delegate"
    status: str = TICKET_PENDING
    position: int = 0
    attempts: int = 0
    spec: str = ""
    allowed_files: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    context: str = ""
    blocked_note: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "route": self.route,
            "status": self.status,
            "position": self.position,
            "attempts": self.attempts,
            "spec": self.spec,
            "allowed_files": json.dumps(self.allowed_files),
            "criteria": json.dumps(self.criteria),
            "context": self.context,
            "blocked_note": self.blocked_note,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Ticket":
        return cls(
            ticket_id=row["ticket_id"],
            title=row["title"],
            route=row["route"],
            status=row["status"],
            position=row["position"],
            attempts=row["attempts"],
            spec=row["spec"],
            allowed_files=json.loads(row["allowed_files"]),
            criteria=json.loads(row["criteria"]),
            context=row["context"],
            blocked_note=row["blocked_note"],
        )


class Store:
    """All persistent state for one repository's runs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # WAL so the dashboard can read while the loop writes.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, goal: str, source: str = "") -> int:
        now = time.time()
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (status, goal, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (RUN_IDLE, goal, source, now, now),
            )
        return int(cursor.lastrowid)

    def set_run_status(self, run_id: int, status: str, note: str = "") -> None:
        now = time.time()
        finished = now if status in (RUN_DONE, RUN_FAILED, RUN_STOPPED) else None
        with self._write() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, note = ?, updated_at = ?, "
                "finished_at = COALESCE(?, finished_at) WHERE id = ?",
                (status, note, now, finished, run_id),
            )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def active_run(self) -> sqlite3.Row | None:
        """The run currently in flight, if any.

        Includes WAITING_BUDGET and BLOCKED — the first will clear itself, the
        second clears when a human edits the ticket.
        """
        return self._connection.execute(
            "SELECT * FROM runs WHERE status NOT IN (?, ?, ?) ORDER BY id DESC LIMIT 1",
            (RUN_DONE, RUN_FAILED, RUN_STOPPED),
        ).fetchone()

    def resumable_run(self) -> sqlite3.Row | None:
        """The run a restarted daemon should pick up.

        Broader than `active_run` on purpose: a run that was stopped — by the
        user, by Ctrl-C, or by the process being killed — is resumable as long
        as tickets remain. Treating "stopped" as permanently terminal would
        mean an interrupted overnight run could never be continued, only
        re-ingested, which is the opposite of what durable state is for.
        """
        active = self.active_run()
        if active is not None:
            return active
        return self._connection.execute(
            "SELECT r.* FROM runs r WHERE r.status = ? AND EXISTS ("
            "  SELECT 1 FROM tickets t WHERE t.run_id = r.id AND t.status IN (?, ?)"
            ") ORDER BY r.id DESC LIMIT 1",
            (RUN_STOPPED, TICKET_PENDING, TICKET_RUNNING),
        ).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    def add_tickets(self, run_id: int, tickets: list[Ticket]) -> None:
        now = time.time()
        with self._write() as connection:
            for position, ticket in enumerate(tickets):
                row = ticket.as_row()
                row["position"] = position if not ticket.position else ticket.position
                connection.execute(
                    "INSERT OR REPLACE INTO tickets "
                    "(run_id, ticket_id, title, route, status, position, attempts, spec, "
                    " allowed_files, criteria, context, blocked_note, updated_at) "
                    "VALUES (:run_id, :ticket_id, :title, :route, :status, :position, "
                    ":attempts, :spec, :allowed_files, :criteria, :context, :blocked_note, :now)",
                    {**row, "run_id": run_id, "now": now},
                )

    def next_ticket(self, run_id: int) -> Ticket | None:
        """The next unit of work, or None when the backlog is exhausted.

        Ordering is by position then id, so a plan's intended sequence is
        honored — later tickets often assume earlier ones landed.
        """
        row = self._connection.execute(
            "SELECT * FROM tickets WHERE run_id = ? AND status IN (?, ?) "
            "ORDER BY position, id LIMIT 1",
            (run_id, TICKET_PENDING, TICKET_RUNNING),
        ).fetchone()
        return Ticket.from_row(row) if row else None

    def list_tickets(self, run_id: int) -> list[Ticket]:
        rows = self._connection.execute(
            "SELECT * FROM tickets WHERE run_id = ? ORDER BY position, id", (run_id,)
        ).fetchall()
        return [Ticket.from_row(row) for row in rows]

    def update_ticket(self, run_id: int, ticket: Ticket) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE tickets SET status = :status, attempts = :attempts, spec = :spec, "
                "allowed_files = :allowed_files, criteria = :criteria, context = :context, "
                "blocked_note = :blocked_note, updated_at = :now "
                "WHERE run_id = :run_id AND ticket_id = :ticket_id",
                {**ticket.as_row(), "run_id": run_id, "now": time.time()},
            )

    def ticket_counts(self, run_id: int) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS n FROM tickets WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def start_step(self, run_id: int, ticket_id: str, name: str) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT INTO steps (run_id, ticket_id, name, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (run_id, ticket_id, name, time.time()),
            )
        return int(cursor.lastrowid)

    def end_step(self, step_id: int, status: str, detail: str = "") -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE steps SET status = ?, ended_at = ?, detail = ? WHERE id = ?",
                (status, time.time(), detail[:20000], step_id),
            )

    def recent_steps(self, run_id: int, limit: int = 40) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # Events (the dashboard's feed)
    # ------------------------------------------------------------------

    def log(
        self,
        run_id: int | None,
        message: str,
        *,
        level: str = "info",
        kind: str = "log",
        data: dict[str, Any] | None = None,
    ) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT INTO events (run_id, ts, level, kind, message, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, time.time(), level, kind, message[:8000], json.dumps(data or {})),
            )
        return int(cursor.lastrowid)

    def events_after(self, event_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (event_id, limit)
            ).fetchall()
        )

    def last_event_id(self) -> int:
        row = self._connection.execute("SELECT MAX(id) AS m FROM events").fetchone()
        return int(row["m"] or 0)

    # ------------------------------------------------------------------
    # Usage ledger (satisfies budget.UsageLedger)
    # ------------------------------------------------------------------

    def record_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO usage (model, ts, prompt_tokens, completion_tokens) "
                "VALUES (?, ?, ?, ?)",
                (model, time.time(), prompt_tokens, completion_tokens),
            )

    def tokens_since(self, model: str, since: float) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total "
            "FROM usage WHERE model = ? AND ts >= ?",
            (model, since),
        ).fetchone()
        return int(row["total"])

    def requests_since(self, model: str, since: float) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM usage WHERE model = ? AND ts >= ?", (model, since)
        ).fetchone()
        return int(row["n"])

    def usage_summary(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT model, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM usage GROUP BY model ORDER BY prompt_tokens DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Control channel
    # ------------------------------------------------------------------

    def set_control(self, key: str, value: str) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO control (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_control(self, key: str, default: str = "") -> str:
        row = self._connection.execute(
            "SELECT value FROM control WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
