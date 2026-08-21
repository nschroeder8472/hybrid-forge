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
import re
import sqlite3
import threading
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
    kind          TEXT NOT NULL DEFAULT 'feature',
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
    charged_failures TEXT NOT NULL DEFAULT '[]',
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

def _criterion_key(criterion: str) -> str:
    """A criterion reduced to what it asserts, for comparing two spellings.

    Backticks, punctuation and case are presentation: a criterion reworded in
    only those has not become a second demand. `respec._key` strips a
    provenance note first and then defers to this, so both sides of the
    ratchet decide sameness the same way.
    """
    return re.sub(r"[^a-z0-9]+", "", criterion.lower())


# What a ticket is for. `bug` earns the reproduce-before-fix path; everything
# else is ordinary forward work.
TICKET_FEATURE = "feature"
TICKET_BUG = "bug"

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
    # What kind of work this is. `feature` tickets describe work that does not
    # exist yet and are verified against their criteria. A `bug` ticket
    # describes something that already misbehaves, and the loop treats it
    # differently in one decisive way: it must reproduce the fault before it is
    # allowed to fix it, and the ticket is only done when the test that proved
    # the fault passes. Criteria alone cannot carry that — a criterion is
    # satisfied the moment the code reads right, and both bugs shipped by one
    # green run read right.
    kind: str = TICKET_FEATURE
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
    # kept for its whole life. A retry cycle may inherit the previous cycle's
    # work in the tree — autoCommit is off, and quarantine cannot revert a
    # ticket whose baseline tree was never recorded — so re-snapshotting per
    # run measures the ticket against its own output and produces an empty diff
    # for a reviewer that has nothing left to judge. It is also what the revert
    # itself restores from, which is the second reason it must not move.
    baseline_tree: str = ""
    # Failure signatures this ticket has been seen to introduce, in any cycle.
    #
    # The baseline is re-taken every cycle on purpose: other tickets run in
    # between, and their breakage has to keep being excused or this ticket
    # spends its attempts on code it may not open. But quarantine cannot always
    # take a failed ticket's work back out, so its own breakage may be on disk
    # when the next cycle starts and the fresh baseline reads it as
    # pre-existing — amnesty for the exact errors it just wrote. That is not hypothetical: one run's debt climbed 3, 7, 13, 20
    # across seven tickets, every one of them passing verification.
    #
    # Charging is what separates the two. A signature recorded here is never
    # excused for this ticket again, however many cycles later it is seen,
    # while a failure that appeared while some other ticket was running is
    # still inherited normally.
    charged_failures: list[str] = field(default_factory=list)
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
            "kind": self.kind,
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
            "charged_failures": json.dumps(self.charged_failures),
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
            kind=row["kind"],
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
            charged_failures=json.loads(row["charged_failures"]),
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
        # One connection per thread, not one connection shared by all of them.
        # `forge go` hands this same Store to the dashboard, which serves every
        # request on a thread of its own, so the loop's writes and the
        # dashboard's reads were interleaving on a single sqlite3 connection.
        # That is undefined use of the driver whatever `check_same_thread`
        # says, and it eventually raised `bad parameter or other API misuse`
        # and killed a run mid-cycle. WAL is what makes the split cheap:
        # readers do not block the writer, and each thread gets its own cursor
        # state instead of trampling a shared one.
        self._local = threading.local()
        connection = self._connect()
        connection.executescript(SCHEMA)
        self._migrate()
        connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL so the dashboard can read while the loop writes.
        connection.execute("PRAGMA journal_mode=WAL")
        # A writer that arrives mid-checkpoint waits rather than raising. Five
        # seconds is far longer than anything this store does.
        connection.execute("PRAGMA busy_timeout=5000")
        self._local.connection = connection
        return connection

    @property
    def _connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        connection = getattr(self._local, "connection", None)
        return connection if connection is not None else self._connect()

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
        ("tickets", "kind", "TEXT NOT NULL DEFAULT 'feature'"),
        ("tickets", "needs", "TEXT NOT NULL DEFAULT '[]'"),
        ("tickets", "dep_stamp", "TEXT NOT NULL DEFAULT '{}'"),
        ("tickets", "baseline_tree", "TEXT NOT NULL DEFAULT ''"),
        ("tickets", "charged_failures", "TEXT NOT NULL DEFAULT '[]'"),
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
        """Close this thread's connection. Other threads keep their own.

        Enough for every caller there is: the CLI closes the store it opened,
        and the dashboard's threads are daemons that die with the process.
        """
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

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

    def unstarted_run(self) -> sqlite3.Row | None:
        """The newest run that was filed and never worked, if there is one.

        A backlog still open to more tickets. `RUN_IDLE` is written once, by
        `create_run`, and the orchestrator moves a run off it before its first
        step, so the status alone means "nothing has been spent here yet". The
        ticket check is the second half of that promise: a run holding worked
        tickets is not one a newly filed ticket should join, whatever its run
        status says. `attempt_base` is checked with it because `forge retry
        --all` returns every ticket to `pending` and sets the run back to
        `idle` — a run whose whole backlog has already been through the loop
        once looks untouched by status alone, and is not.

        Grouping filed work rather than draining it: two reports filed back to
        back belong on one backlog, in the order they were written. What
        happens to work that was *not* grouped this way — filed behind a run
        already in flight — is `resumable_runs`.
        """
        return self._connection.execute(
            "SELECT r.* FROM runs r WHERE r.status = ? AND NOT EXISTS ("
            "  SELECT 1 FROM tickets t WHERE t.run_id = r.id AND ("
            "    t.status != ? OR t.attempts != 0 OR t.attempt_base != 0"
            "  )"
            ") ORDER BY r.id DESC LIMIT 1",
            (RUN_IDLE, TICKET_PENDING),
        ).fetchone()

    def next_position(self, run_id: int) -> int:
        """Where a ticket appended to this run belongs in the reading order.

        `list_tickets` orders by position then id, and positions start at 0,
        so an appended ticket left at the default would sort second rather
        than last.
        """
        row = self._connection.execute(
            "SELECT MAX(position) AS last FROM tickets WHERE run_id = ?", (run_id,)
        ).fetchone()
        return 0 if row is None or row["last"] is None else int(row["last"]) + 1

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

    def resumable_runs(self) -> list[sqlite3.Row]:
        """Every run with work still queued, oldest first.

        The whole queue, because `forge go` drains it rather than working the
        newest and leaving the rest. Four commands open runs — `ingest`, `bug`,
        `go --plan` and `retry` — and taking only the highest id meant anything
        filed behind a run that then blocked waited for a human to notice it.
        Noticing was unlikely: `forge status` shows one run too, so the stranded
        work was not on screen anywhere.

        Oldest first, which is the order it was filed in.

        `done` and `failed` are excluded, as they are by `resumable_run`. A done
        run has nothing queued by definition; a failed one died of something —
        an unreachable role, a crash — that re-entering the same backlog does
        not fix, and draining into it would turn one failure into several.
        """
        return list(
            self._connection.execute(
                "SELECT r.* FROM runs r WHERE r.status NOT IN (?, ?) AND EXISTS ("
                "  SELECT 1 FROM tickets t WHERE t.run_id = r.id AND t.status IN (?, ?)"
                ") ORDER BY r.id",
                (RUN_DONE, RUN_FAILED, TICKET_PENDING, TICKET_RUNNING),
            ).fetchall()
        )

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
                    "(run_id, ticket_id, title, route, kind, status, position, attempts, "
                    " attempt_base, spec, allowed_files, reference_files, criteria, needs, dep_stamp, "
                    " baseline_tree, charged_failures, context, "
                    " blocked_note, original_spec, original_criteria, original_context, "
                    " updated_at) "
                    "VALUES (:run_id, :ticket_id, :title, :route, :kind, :status, :position, "
                    ":attempts, :attempt_base, :spec, :allowed_files, :reference_files, "
                    ":criteria, :needs, :dep_stamp, :baseline_tree, :charged_failures, :context, "
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
                "UPDATE tickets SET status = :status, kind = :kind, attempts = :attempts, "
                "attempt_base = :attempt_base, spec = :spec, "
                "allowed_files = :allowed_files, reference_files = :reference_files, "
                "criteria = :criteria, needs = :needs, dep_stamp = :dep_stamp, "
                "baseline_tree = :baseline_tree, "
                "charged_failures = :charged_failures, context = :context, "
                "blocked_note = :blocked_note, updated_at = :now "
                "WHERE run_id = :run_id AND ticket_id = :ticket_id",
                {**ticket.as_row(), "run_id": run_id, "now": time.time()},
            )

    def promote_criteria(
        self, run_id: int, ticket_id: str, criteria: list[str]
    ) -> tuple[Ticket | None, list[str]]:
        """Adopt criteria into the plan's contract on a human's say-so.

        The one path that moves `original_criteria` after ingest, and it exists
        because the alternative was worse. Respec refuses a criterion the plan
        states nowhere, which is right — the party being judged does not get to
        add to the standard — but the refusal left a human editing `plan.md`
        and re-ingesting the whole backlog to accept a single line, redoing
        work that had already passed. So a person can adopt one here, and what
        they adopt becomes plan-authored: protected by the ratchet from the
        next revision onwards, exactly as if they had written it in the plan.

        Deliberately not reachable from the loop. Every other caller writes
        through `update_ticket`, which cannot touch the anchor at all.

        Returns `(ticket, adopted)`; `ticket` is None when the id is unknown.
        Criteria the ticket already carries are skipped rather than duplicated.
        """
        matching = [t for t in self.list_tickets(run_id) if t.ticket_id == ticket_id]
        if not matching:
            return None, []
        ticket = matching[0]

        known = {_criterion_key(c) for c in ticket.criteria}
        adopted = []
        for criterion in criteria:
            key = _criterion_key(criterion)
            if not key or key in known:
                continue
            known.add(key)
            adopted.append(criterion)
        if not adopted:
            return ticket, []

        ticket.criteria = ticket.criteria + adopted
        ticket.original_criteria = ticket.original_criteria + adopted
        with self._write() as connection:
            connection.execute(
                "UPDATE tickets SET criteria = :criteria, "
                "original_criteria = :original_criteria, updated_at = :now "
                "WHERE run_id = :run_id AND ticket_id = :ticket_id",
                {**ticket.as_row(), "run_id": run_id, "now": time.time()},
            )
        return ticket, adopted

    def proposed_criteria(self, run_id: int) -> dict[str, list[str]]:
        """Criteria respec proposed and the loop refused, by ticket.

        Read back out of the run log rather than kept in a table of their own:
        the refusal is already an event, and a second store of the same fact is
        a second thing to keep true. Anything the ticket has since acquired —
        adopted here, or written into the plan and re-ingested — is filtered
        out, so the list is what is still outstanding rather than a history.
        """
        rows = self._connection.execute(
            "SELECT data FROM events WHERE run_id = ? AND kind = 'ticket' "
            "AND data LIKE '%\"minted\"%' ORDER BY id",
            (run_id,),
        ).fetchall()

        on_ticket = {
            ticket.ticket_id: {_criterion_key(c) for c in ticket.criteria}
            for ticket in self.list_tickets(run_id)
        }
        pending: dict[str, list[str]] = {}
        for row in rows:
            try:
                data = json.loads(row["data"])
            except json.JSONDecodeError:
                continue
            ticket_id = data.get("ticket", "")
            if ticket_id not in on_ticket:
                continue
            for criterion in data.get("minted", []):
                key = _criterion_key(criterion)
                if key in on_ticket[ticket_id]:
                    continue
                # A ticket that failed the same way twice proposed the same
                # criterion twice. It is one outstanding decision, not two.
                if key in {_criterion_key(c) for c in pending.get(ticket_id, [])}:
                    continue
                pending.setdefault(ticket_id, []).append(criterion)
        return pending

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

    def reproduced(self, run_id: int, ticket_id: str) -> str:
        """The output that proved this bug, or "" if it was never reproduced.

        The `reproduce` step is recorded `ok` when the test the loop wrote
        *failed* — that failure is the proof, and it is the one place in the
        pipeline where a red suite is the desired outcome.

        Durable because the fix erases the evidence. Once the bug is fixed the
        test passes, and a retry cycle re-running reproduction would find
        nothing wrong and park a ticket whose work is already done. Asking the
        step log instead answers the only question that matters on a second
        pass: was this fault ever real.
        """
        row = self._connection.execute(
            "SELECT detail FROM steps "
            "WHERE run_id = ? AND ticket_id = ? AND name = 'reproduce' "
            "AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (run_id, ticket_id),
        ).fetchone()
        return row["detail"] if row else ""

    def failed_steps(self, run_id: int, ticket_id: str) -> list[tuple[str, str]]:
        """Every failed step this ticket recorded, as `(name, output)`, oldest first.

        `ticket_failures` deduplicates and distils, which is right for prompt
        material and wrong for the one caller that needs to know how many times
        the same thing happened rather than what it was.
        """
        rows = self._connection.execute(
            "SELECT name, detail FROM steps "
            "WHERE run_id = ? AND ticket_id = ? "
            "AND status = 'failed' AND detail != '' ORDER BY id",
            (run_id, ticket_id),
        ).fetchall()
        return [(row["name"], row["detail"]) for row in rows]

    def retire_reproduction(self, run_id: int, ticket_id: str) -> None:
        """Stop `reproduced` from answering with a proof no longer trusted.

        Marked rather than deleted. The step keeps its output, so a human
        reading the run still sees what the retired test reported and why the
        loop stopped believing it; `reproduced` asks for status `ok` and no
        longer finds it, so the next pass writes a fresh reproduction.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE steps SET status = 'superseded' "
                "WHERE run_id = ? AND ticket_id = ? AND name = 'reproduce' "
                "AND status = 'ok'",
                (run_id, ticket_id),
            )

    def steps_for_replay(
        self, run_id: int | None = None, ticket_id: str | None = None
    ) -> list[sqlite3.Row]:
        """Recorded step output, for re-reading with the current parsers.

        The fallback behind `forge replay` for runs whose artifacts were never
        written or have been deleted. Clipped at 20k characters by whoever
        wrote it, and carrying no record of what the parser made of it at the
        time — so these can be re-read but not checked for a difference.
        """
        clauses, values = ["detail != ''"], []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if ticket_id:
            clauses.append("ticket_id = ?")
            values.append(ticket_id)
        return list(
            self._connection.execute(
                f"SELECT id, run_id, ticket_id, name, status, detail FROM steps "
                f"WHERE {' AND '.join(clauses)} ORDER BY id",
                tuple(values),
            ).fetchall()
        )

    def ruled_out(self, run_id: int, ticket_id: str) -> list[tuple[str, str]]:
        """Hypotheses this bug ticket has already disproved, oldest first.

        Read back out of the run log rather than kept in a column of its own:
        each re-diagnosis already logs what it dropped and why, and a second
        store of the same fact is a second thing to keep true.

        The list is what stops the third hypothesis being the first one again.
        A planner handed only "that was wrong, try again" proposes the same
        files with the same reasoning, because from where it sits nothing has
        changed.
        """
        rows = self._connection.execute(
            "SELECT data FROM events WHERE run_id = ? AND kind = 'ticket' "
            "AND data LIKE '%\"ruled_out\"%' ORDER BY id",
            (run_id,),
        ).fetchall()

        found: list[tuple[str, str]] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except json.JSONDecodeError:
                continue
            if data.get("ticket") != ticket_id:
                continue
            spec = str(data.get("ruled_out", "")).strip()
            if spec:
                found.append((spec, str(data.get("disproof", "")).strip()))
        return found

    def ticket_turns(
        self, run_id: int, ticket_id: str, limit: int = 2
    ) -> list[tuple[str, str]]:
        """Prior attempts as `(what the executor replied, what failed)`, oldest first.

        The executor has never seen its own output. It is handed the spec, the
        files as they exist on disk, and the failures — with nothing anywhere
        saying that it wrote those files. That is the state behind "Looking at
        the files provided, I can see they already implement the spec
        correctly": a model reading its own work as somebody else's.

        Both halves of each turn are already durable. The build step keeps the
        raw reply, and the step that failed next keeps why. Rebuilding the
        conversation here rather than holding it in the attempt loop is what
        keeps the daemon's state machine the only state machine: transport
        stays stateless, the shape is conversational, and a retry cycle
        inherits the thread the same way `ticket_failures` inherits failures.

        A reply with no failure after it is dropped rather than paired with the
        next one along. An attempt can end without a failed step — a reply the
        harness could not read is refused before anything runs — and attaching
        that reply to a later, unrelated failure would tell the executor its
        code caused something it never reached.
        """
        rows = self._connection.execute(
            "SELECT name, status, detail FROM steps "
            "WHERE run_id = ? AND ticket_id = ? ORDER BY id",
            (run_id, ticket_id),
        ).fetchall()

        turns: list[tuple[str, str]] = []
        reply = ""
        for row in rows:
            if row["name"] == "build":
                # A new build ends the previous turn whatever came of it, so an
                # unpaired reply is discarded here rather than carried forward.
                reply = row["detail"] if row["status"] == "ok" else ""
                continue
            if not reply or row["status"] != "failed" or not row["detail"]:
                continue
            turns.append((reply, distill(row["detail"], limit=2500)))
            reply = ""
        return turns[-limit:] if limit else turns

    def ticket_rejections(
        self, run_id: int, ticket_id: str, limit: int = 3
    ) -> list[str]:
        """Verdicts the reviewer has already rejected this ticket with, oldest first.

        The attempt loop keeps these in memory, and a retry cycle calls into it
        fresh — so a second cycle's reviewer met a ticket it had already
        rejected three times as though for the first time, and re-raised the
        same objections from scratch. The nudge that a repeated rejection means
        the spec is wrong could never fire, because the list it reads was
        always empty at the moment it mattered.

        Kept whole rather than distilled: a verdict is prose the reviewer wrote
        for its own successor, and `distill` is built for compiler output.
        """
        rows = self._connection.execute(
            "SELECT detail FROM steps "
            "WHERE run_id = ? AND ticket_id = ? AND name = 'review' "
            "AND status = 'failed' AND detail != '' "
            "ORDER BY id DESC LIMIT ?",
            (run_id, ticket_id, limit),
        ).fetchall()
        return [row["detail"] for row in reversed(rows)]

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
