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

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .failures import distill

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
    attempt_base  INTEGER NOT NULL DEFAULT 0,
    spec          TEXT NOT NULL DEFAULT '',
    allowed_files TEXT NOT NULL DEFAULT '[]',
    reference_files TEXT NOT NULL DEFAULT '[]',
    criteria      TEXT NOT NULL DEFAULT '[]',
    needs         TEXT NOT NULL DEFAULT '[]',
    dep_stamp     TEXT NOT NULL DEFAULT '{}',
    baseline_tree TEXT NOT NULL DEFAULT '',
    context       TEXT NOT NULL DEFAULT '',
    blocked_note  TEXT NOT NULL DEFAULT '',
    original_spec     TEXT NOT NULL DEFAULT '',
    original_criteria TEXT NOT NULL DEFAULT '[]',
    original_context  TEXT NOT NULL DEFAULT '',
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
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    model                 TEXT NOT NULL,
    ts                    REAL NOT NULL,
    prompt_tokens         INTEGER NOT NULL DEFAULT 0,
    completion_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0
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
    # Attempts spent on this ticket by *earlier* retry cycles. `attempts` is
    # reset per cycle so the loop's max-attempts budget starts fresh, but the
    # artifact directory is named from the sum — otherwise a retry would write
    # over the very evidence that explains why the first cycle failed.
    attempt_base: int = 0
    spec: str = ""
    allowed_files: list[str] = field(default_factory=list)
    # Files the executor may read but must not write. Without these it works
    # entirely blind: it is asked to produce whole files against an existing
    # codebase it has never seen, so it guesses at export names and signatures
    # and the reviewer rejects the guess.
    reference_files: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    # Ticket ids that must reach `done` before this one is eligible. A ticket
    # is a testable unit, not a file lease: two tickets may both write
    # `src/lib.rs`, and what they need is an order, not exclusive ownership.
    # Declared in the plan, or derived at ingest from a file they share.
    needs: list[str] = field(default_factory=list)
    # What each dependency looked like when this ticket last passed, as
    # `{dep_id: fingerprint}`. A ticket earns `done` against a particular
    # version of what it was built on; when a respec moves that, the pass was
    # earned against a contract that no longer exists.
    dep_stamp: dict[str, str] = field(default_factory=dict)
    # The tree this ticket started from, captured the first time it ran and
    # kept for its whole life. A retry cycle inherits the previous cycle's work
    # in the tree — autoCommit is off and nothing reverts a failed ticket — so
    # re-snapshotting per run measures the ticket against its own output and
    # produces an empty diff for a reviewer that has nothing left to judge.
    baseline_tree: str = ""
    context: str = ""
    blocked_note: str = ""
    # The spec and criteria as the plan was ingested, never rewritten. Respec
    # revises a ticket from the *current* one, so without an anchor the tenth
    # revision is derived from the ninth and nothing is left of what a human
    # asked for. One ticket drifted until its criteria asserted the opposite
    # of the plan's, and every party downstream believed the drift.
    original_spec: str = ""
    original_criteria: list[str] = field(default_factory=list)
    # The plan's context paragraph, kept for the same reason. `context` is a
    # full replacement with no provenance of its own, and respec used it as a
    # rationale scratchpad: five tickets lost the plan's bare-path-line rule to
    # a sentence about why the executor keeps omitting scaffold files.
    original_context: str = ""

    @property
    def drifted(self) -> bool:
        """Whether the ticket now differs from what was ingested.

        False when there is no anchor to compare against — a run from before
        the originals were recorded reports no drift rather than claiming
        drift from an empty string.
        """
        if not self.original_spec:
            return False
        if self.spec != self.original_spec:
            return True
        if self.original_context and self.context != self.original_context:
            return True
        return bool(self.original_criteria) and self.criteria != self.original_criteria

    @property
    def attempt_number(self) -> int:
        """Globally increasing attempt index, used to name artifacts."""
        return self.attempt_base + self.attempts

    @property
    def fingerprint(self) -> str:
        """What a dependent of this ticket was actually depending on.

        Spec, criteria and writable scope — the three things a respec changes
        that alter what a dependent was built against. Deliberately not status
        or attempts: a ticket re-running and passing again with the same
        contract invalidates nothing.
        """
        payload = json.dumps(
            [self.spec, self.criteria, self.allowed_files], sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_row(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "route": self.route,
            "status": self.status,
            "position": self.position,
            "attempts": self.attempts,
            "attempt_base": self.attempt_base,
            "spec": self.spec,
            "allowed_files": json.dumps(self.allowed_files),
            "reference_files": json.dumps(self.reference_files),
            "criteria": json.dumps(self.criteria),
            "needs": json.dumps(self.needs),
            "dep_stamp": json.dumps(self.dep_stamp),
            "baseline_tree": self.baseline_tree,
            "context": self.context,
            "blocked_note": self.blocked_note,
            "original_spec": self.original_spec,
            "original_criteria": json.dumps(self.original_criteria),
            "original_context": self.original_context,
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
            attempt_base=row["attempt_base"],
            spec=row["spec"],
            allowed_files=json.loads(row["allowed_files"]),
            reference_files=json.loads(row["reference_files"]),
            criteria=json.loads(row["criteria"]),
            needs=json.loads(row["needs"]),
            dep_stamp=json.loads(row["dep_stamp"]),
            baseline_tree=row["baseline_tree"],
            context=row["context"],
            blocked_note=row["blocked_note"],
            original_spec=row["original_spec"],
            original_criteria=json.loads(row["original_criteria"]),
            original_context=row["original_context"],
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
        self._migrate()
        self._connection.commit()

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will
    # not add them to a database that already exists, so widen it here instead
    # of leaving older runs unable to open at all.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("usage", "cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("usage", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("usage", "cost_usd", "REAL NOT NULL DEFAULT 0"),
        ("tickets", "attempt_base", "INTEGER NOT NULL DEFAULT 0"),
        ("tickets", "reference_files", "TEXT NOT NULL DEFAULT '[]'"),
        ("tickets", "original_spec", "TEXT NOT NULL DEFAULT ''"),
        ("tickets", "original_criteria", "TEXT NOT NULL DEFAULT '[]'"),
        ("tickets", "original_context", "TEXT NOT NULL DEFAULT ''"),
        ("tickets", "needs", "TEXT NOT NULL DEFAULT '[]'"),
        ("tickets", "dep_stamp", "TEXT NOT NULL DEFAULT '{}'"),
        ("tickets", "baseline_tree", "TEXT NOT NULL DEFAULT ''"),
    )

    def _migrate(self) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )

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
                # Ingest is the only moment the original is knowable, so it is
                # captured here rather than asked for. Everything downstream
                # rewrites `spec`; nothing may write these again.
                row["original_spec"] = ticket.original_spec or ticket.spec
                row["original_criteria"] = json.dumps(
                    ticket.original_criteria or ticket.criteria
                )
                row["original_context"] = ticket.original_context or ticket.context
                connection.execute(
                    "INSERT OR REPLACE INTO tickets "
                    "(run_id, ticket_id, title, route, status, position, attempts, "
                    " attempt_base, spec, allowed_files, reference_files, criteria, needs, dep_stamp, "
                    " baseline_tree, context, "
                    " blocked_note, original_spec, original_criteria, original_context, "
                    " updated_at) "
                    "VALUES (:run_id, :ticket_id, :title, :route, :status, :position, "
                    ":attempts, :attempt_base, :spec, :allowed_files, :reference_files, "
                    ":criteria, :needs, :dep_stamp, :baseline_tree, :context, "
                    ":blocked_note, :original_spec, :original_criteria, :original_context, :now)",
                    {**row, "run_id": run_id, "now": now},
                )

    def next_ticket(self, run_id: int) -> Ticket | None:
        """The next eligible unit of work, or None when none is runnable.

        Eligible means pending and every ticket in `needs` already done. Ties
        break on position then id, so a plan's intended reading order still
        decides among tickets that could equally run now.

        None does not always mean the backlog is finished. With a validated
        acyclic graph it means one of two things — nothing is left, or what is
        left is waiting on a dependency that failed. `Orchestrator._finish`
        separates them; see `_park_unreachable`.
        """
        for ticket in self.list_tickets(run_id):
            if ticket.status not in (TICKET_PENDING, TICKET_RUNNING):
                continue
            if self.unmet_needs(run_id, ticket):
                continue
            return ticket
        return None

    def unmet_needs(self, run_id: int, ticket: Ticket) -> list[str]:
        """Dependencies of `ticket` that have not reached done."""
        if not ticket.needs:
            return []
        done = {
            row["ticket_id"]
            for row in self._connection.execute(
                "SELECT ticket_id FROM tickets WHERE run_id = ? AND status = ?",
                (run_id, TICKET_DONE),
            )
        }
        return [dep for dep in ticket.needs if dep not in done]

    def list_tickets(self, run_id: int) -> list[Ticket]:
        rows = self._connection.execute(
            "SELECT * FROM tickets WHERE run_id = ? ORDER BY position, id", (run_id,)
        ).fetchall()
        return [Ticket.from_row(row) for row in rows]

    def update_ticket(self, run_id: int, ticket: Ticket) -> None:
        # `original_spec`, `original_criteria` and `original_context` are
        # deliberately absent from this statement. They are the anchor a respec
        # is judged against, and an anchor that any caller can move is not one.
        with self._write() as connection:
            connection.execute(
                "UPDATE tickets SET status = :status, attempts = :attempts, "
                "attempt_base = :attempt_base, spec = :spec, "
                "allowed_files = :allowed_files, reference_files = :reference_files, "
                "criteria = :criteria, needs = :needs, dep_stamp = :dep_stamp, "
                "baseline_tree = :baseline_tree, context = :context, "
                "blocked_note = :blocked_note, updated_at = :now "
                "WHERE run_id = :run_id AND ticket_id = :ticket_id",
                {**ticket.as_row(), "run_id": run_id, "now": time.time()},
            )

    def ticket_failures(
        self, run_id: int, ticket_id: str, limit: int = 6
    ) -> list[dict[str, str]]:
        """What actually went wrong on a ticket, oldest first.

        The step log outlives the attempt loop's in-memory failure context, so
        this is the only durable record of the reviewer's reasoning once a
        ticket has been given up on. Ordered oldest-first because a repeated
        rejection across attempts is the strongest signal that the spec, not
        the implementation, is what needs changing.
        """
        rows = self._connection.execute(
            "SELECT name, detail FROM steps "
            "WHERE run_id = ? AND ticket_id = ? AND status = 'failed' AND detail != '' "
            "ORDER BY id DESC LIMIT ?",
            (run_id, ticket_id, limit * 4),
        ).fetchall()

        # Steps keep the raw output — it is the durable record. Distil here,
        # where it becomes prompt input, so a reader still gets everything and
        # the planner gets the diagnosis instead of 20k characters of warnings.
        failures: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in reversed(rows):
            detail = distill(row["detail"], limit=2500)
            if not detail:
                continue
            # The same lint error on all three attempts is one fact, not three.
            # Repeating it crowds out the other failures inside the budget.
            key = f"{row['name']}::{detail}"
            if key in seen:
                continue
            seen.add(key)
            failures.append({"name": row["name"], "detail": detail})
        return failures[-limit:]

    # Statuses a retry reopens by default: work that stopped without landing.
    RETRYABLE = (TICKET_FAILED, TICKET_BLOCKED, TICKET_SKIPPED)

    def reset_tickets(
        self,
        run_id: int,
        ticket_ids: list[str] | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[Ticket]:
        """Return exhausted tickets to the backlog and report what moved.

        Each reset rolls the spent attempts into `attempt_base` before zeroing
        `attempts`, so the next cycle gets a full budget while its artifacts
        land in fresh directories rather than on top of the failed ones.

        `ticket_ids` selects explicitly and ignores `statuses` — retrying a
        named ticket that happens to be `done` is a legitimate thing to ask
        for, and silently skipping it would be worse than doing it.
        """
        reset: list[Ticket] = []
        for ticket in self.list_tickets(run_id):
            if ticket_ids is not None:
                if ticket.ticket_id not in ticket_ids:
                    continue
            elif ticket.status not in (statuses or self.RETRYABLE):
                continue
            ticket.attempt_base += ticket.attempts
            ticket.attempts = 0
            ticket.status = TICKET_PENDING
            ticket.blocked_note = ""
            self.update_ticket(run_id, ticket)
            reset.append(ticket)
        return reset

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

    def record_usage(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO usage (model, ts, prompt_tokens, completion_tokens, "
                "cache_creation_tokens, cache_read_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    model,
                    time.time(),
                    prompt_tokens,
                    completion_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                    cost_usd,
                ),
            )

    def tokens_since(self, model: str, since: float) -> int:
        # Cache reads and writes count against the window the same as fresh
        # input does. Summing only prompt + completion here would let a
        # cache-heavy run sail past a tokens_per_window limit unnoticed.
        row = self._connection.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens "
            "+ cache_creation_tokens + cache_read_tokens), 0) AS total "
            "FROM usage WHERE model = ? AND ts >= ?",
            (model, since),
        ).fetchone()
        return int(row["total"])

    def cost_since(self, model: str, since: float) -> float:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total "
            "FROM usage WHERE model = ? AND ts >= ?",
            (model, since),
        ).fetchone()
        return float(row["total"])

    def requests_since(self, model: str, since: float) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM usage WHERE model = ? AND ts >= ?", (model, since)
        ).fetchone()
        return int(row["n"])

    def usage_summary(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT model, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(SUM(prompt_tokens + completion_tokens "
            "+ cache_creation_tokens + cache_read_tokens), 0) AS total_tokens "
            "FROM usage GROUP BY model ORDER BY total_tokens DESC"
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
