"""The monitor dashboard: a small HTTP server over the run's SQLite state.

Read-mostly by design. The loop owns the state machine; this serves a snapshot
of it plus a live event stream, and exposes exactly three writes — pause,
resume, stop — through the same control table the loop already polls. The
dashboard never reaches into the loop's internals, so a crashed UI cannot take
a run with it, and a restarted UI reattaches with no handshake.

Bound to loopback by default, and there is no authentication of any kind. The
control endpoint can stop a running job, so widening the bind address hands
that button to anyone who can reach the port. Keep it on 127.0.0.1 and tunnel
in if you need it from elsewhere; if you do widen it, put something in front
that authenticates, because this server will not.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import parse

from ..config import Config
from ..loop import (
    CONTROL_KEY,
    CONTROL_PAUSE,
    CONTROL_RUN,
    CONTROL_STOP,
    CURRENT_RUN_KEY,
)
from ..routes import REASONS, describe, reason_of
from ..state import Store, Ticket
from ..tokens import format_tokens
from . import writes

INDEX = Path(__file__).with_name("index.html")

_ALLOWED_COMMANDS = {
    "pause": CONTROL_PAUSE,
    "resume": CONTROL_RUN,
    "run": CONTROL_RUN,
    "stop": CONTROL_STOP,
}

# A run in one of these is over; the loop is not inside it whatever the control
# table last recorded, so the dashboard falls back to showing the newest run.
_TERMINAL = ("done", "failed", "stopped", "blocked")


def _live_run(store: Store) -> Any:
    """The run the loop is working right now, or None if it is between runs.

    Read through the run's own status rather than trusted outright: the key is
    written when a run is entered and never cleared, so after `forge go` exits
    it names the last run worked, which is history.
    """
    recorded = store.get_control(CURRENT_RUN_KEY, "")
    if not recorded.isdigit():
        return None
    run = store.get_run(int(recorded))
    if run is None or run["status"] in _TERMINAL:
        return None
    return run


def evidence(ticket: Ticket) -> dict[str, Any] | None:
    """The evidence a parked ticket already has, or None when it is not parked.

    Parked means one of the four statuses a retry reopens — `Store.RETRYABLE` —
    read rather than restated so the two lists cannot drift. Every value is
    already on the ticket; this only shapes it for the page. The lists are
    capped and their text cut here rather than in the page, because `learned`
    and `human_note` are append-only by design and `/api/state` is polled for
    the life of a run, so an uncapped payload grows without bound on the one
    ticket that is stuck longest.
    """
    if ticket.status not in Store.RETRYABLE:
        return None
    reason = reason_of(ticket.route)
    return {
        "route": describe(ticket.route),
        "reason": REASONS[reason] if reason else "",
        "learned": [
            {"text": entry["text"][:400], "count": entry["count"]}
            for entry in ticket.learned[:5]
        ],
        "notes": [
            {"text": entry["text"][:400], "at": entry["at"]}
            for entry in ticket.human_note[-5:]
        ],
        "classes": list(ticket.cycle_classes),
        "volume": ticket.cycle_volume,
        "flat_cycles": ticket.flat_cycles,
        "distinct_classes": ticket.distinct_classes,
        "new_classes": list(ticket.new_classes[:5]),
    }


def ticket_detail(store: Store, config: Config, ticket_id: str) -> dict[str, Any] | None:
    """One ticket's whole story, for the run `snapshot()` is showing.

    The run is found the way `snapshot` finds it — the live one while it is
    non-terminal, otherwise the newest — so the page and the detail behind it
    can never disagree about which run they are reading. `None` when there is
    no run at all, or the run holds no ticket with that id.

    `failures` is one entry per failed step `Store.failed_steps` returns,
    oldest first, each detail cut to 4,000 characters: the same cap the page's
    step list already applies, so a ticket that failed a long test run does
    not ship its whole output to a browser.
    """
    run = _live_run(store) or store.latest_run()
    if run is None:
        return None
    run_id = int(run["id"])
    for ticket in store.list_tickets(run_id):
        if ticket.ticket_id != ticket_id:
            continue
        return {
            "id": ticket.ticket_id,
            "title": ticket.title,
            "status": ticket.status,
            "route": describe(ticket.route),
            "spec": ticket.spec,
            "criteria": list(ticket.criteria),
            "files": list(ticket.allowed_files),
            "note": ticket.blocked_note,
            "failures": [
                {"step": name, "detail": detail[:4000]}
                for name, detail in store.failed_steps(run_id, ticket_id)
            ],
        }
    return None


def snapshot(store: Store, config: Config) -> dict[str, Any]:
    """Everything the dashboard renders, in one read.

    The run the loop is inside, if it is inside one; otherwise the newest.

    `active_run()` looks like the right second choice and is not: it skips
    finished runs, so an older run someone left blocked outranks the one that
    just succeeded. A backlog that had gone six-for-six reported `run 7:
    blocked — 6 ticket(s) need a human`, naming a run two days stale, which
    reads as the run having just failed. Newest-wins is what fixed that.

    Newest-wins alone is not enough now that `forge go` drains its queue oldest
    first: the live run is often not the highest id, and the dashboard would
    show a run still waiting its turn while the loop worked another. So the
    loop records which run it entered, and that wins while it is non-terminal.
    A terminal one is exactly the stale case above, and falls back to newest.
    """
    run = _live_run(store) or store.latest_run()
    if run is None:
        return {"run": None, "tickets": [], "steps": [], "usage": [], "control": CONTROL_RUN}

    run_id = int(run["id"])
    tickets = store.list_tickets(run_id)
    counts = store.ticket_counts(run_id)

    return {
        "run": {
            "id": run_id,
            "status": run["status"],
            "goal": run["goal"],
            "source": run["source"],
            "note": run["note"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "finished_at": run["finished_at"],
        },
        "counts": counts,
        "progress": {
            "done": counts.get("done", 0),
            "total": len(tickets),
        },
        "tickets": [
            {
                "id": t.ticket_id,
                "title": t.title,
                "route": t.route,
                "status": t.status,
                "attempts": t.attempts,
                "files": t.allowed_files,
                "criteria": t.criteria,
                "note": t.blocked_note,
                **({"evidence": ev} if (ev := evidence(t)) is not None else {}),
            }
            for t in tickets
        ],
        "steps": [
            {
                "id": row["id"],
                "ticket": row["ticket_id"],
                "name": row["name"],
                "status": row["status"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "detail": (row["detail"] or "")[:4000],
            }
            for row in store.recent_steps(run_id, limit=30)
        ],
        "usage": [
            {
                **row,
                "total": row["total_tokens"],
                "display": format_tokens(row["total_tokens"]),
                "cached": row["cache_creation_tokens"] + row["cache_read_tokens"],
                "cost_display": (
                    f"${row['cost_usd']:.2f}" if row["cost_usd"] else ""
                ),
                "input": (
                    row["prompt_tokens"]
                    + row["cache_creation_tokens"]
                    + row["cache_read_tokens"]
                ),
                "output": row["completion_tokens"],
                "input_display": format_tokens(
                    row["prompt_tokens"]
                    + row["cache_creation_tokens"]
                    + row["cache_read_tokens"]
                ),
                "output_display": format_tokens(row["completion_tokens"]),
            }
            for row in store.usage_summary()
        ],
        "roles": config.roles,
        "control": store.get_control(CONTROL_KEY, CONTROL_RUN),
        "last_event_id": store.last_event_id(),
    }


class Handler(BaseHTTPRequestHandler):
    store: Store
    config: Config

    # Silence per-request logging; the run's own event log is the useful record.
    def log_message(self, *args: Any) -> None:  # noqa: A003
        return

    def handle_one_request(self) -> None:
        """Serve one request, treating a vanished client as normal.

        A closed tab, a refresh, or a laptop lid mid-stream tears the socket
        down under whichever write happens to be in flight. `socketserver`
        answers that by printing a traceback to stderr — into the middle of the
        run's output, where it reads as the loop having crashed. It has not:
        nothing about a run depends on a browser being attached.
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def finish(self) -> None:
        """Hand the socket back, then this thread's database connection.

        `ThreadingHTTPServer` runs each connection on a thread of its own, and
        `Store` opens a connection per thread. Nothing closed them, so every
        served connection left an open SQLite handle for the garbage collector
        to find — a Windows file lock on the run database, held for as long as
        refcounting took to notice. Closing here is per connection rather than
        per request, so keep-alive still reuses one.
        """
        try:
            super().finish()
        finally:
            self.store.close()

    # ------------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path, _, query = self.path.partition("?")

        if path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"dashboard template missing", "text/plain")
            return

        if path == "/api/state":
            self._send_json(snapshot(self.store, self.config))
            return

        if path.startswith("/api/ticket/"):
            ticket_id = parse.unquote(path[len("/api/ticket/"):])
            detail = ticket_detail(self.store, self.config, ticket_id)
            if detail is None:
                self._send_json({"error": "no such ticket"}, code=404)
            else:
                self._send_json(detail)
            return

        if path == "/api/events":
            self._stream_events(query)
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/api/control":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON"}, code=400)
                return

            requested = str(payload.get("command", "")).lower()
            command = _ALLOWED_COMMANDS.get(requested)
            if command is None:
                self._send_json(
                    {
                        "error": f"unknown command {requested!r}",
                        "allowed": sorted(_ALLOWED_COMMANDS),
                    },
                    code=400,
                )
                return

            self.store.set_control(CONTROL_KEY, command)
            self.store.log(None, f"Dashboard requested: {requested}", kind="control")
            self._send_json({"ok": True, "control": command})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, code=400)
            return

        peer = self.client_address[0] if self.client_address else ""
        status, body = writes.apply_write(self.store, self.config, self.path, payload, peer)
        self._send_json(body, code=status)

    # ------------------------------------------------------------------

    def _stream_events(self, query: str) -> None:
        """Server-sent events, resumable via `?after=<last event id>`.

        A reconnecting browser passes the last id it saw, so a dropped
        connection replays the gap instead of silently losing it.
        """
        after = 0
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "after" and value.isdigit():
                after = int(value)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_heartbeat = time.time()
        try:
            while True:
                rows = self.store.events_after(after)
                for row in rows:
                    after = int(row["id"])
                    event = {
                        "id": after,
                        "ts": row["ts"],
                        "level": row["level"],
                        "kind": row["kind"],
                        "message": row["message"],
                        "data": json.loads(row["data"] or "{}"),
                    }
                    self.wfile.write(f"id: {after}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()

                # Comment frames keep proxies from closing an idle stream.
                if time.time() - last_heartbeat > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.time()

                time.sleep(1.0)
        except ConnectionError:
            # Every way a client can vanish: BrokenPipeError and
            # ConnectionResetError on POSIX, ConnectionAbortedError on Windows
            # (WinError 10053), all of them subclasses of this. Catching the
            # two POSIX names left the Windows one to reach socketserver, which
            # printed a stack trace into the run's output every time a tab was
            # closed.
            return


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def is_exposed(host: str) -> bool:
    """True when this bind address reaches beyond the local machine."""
    return host.strip() not in LOOPBACK_HOSTS


def exposure_warning(config: Config) -> str:
    """Warning text for a non-loopback bind, or "" when bound to loopback.

    Worth saying out loud every time rather than only in the docs: the control
    endpoint can stop a running job and there is nothing here to stop anyone
    who can reach the port from pressing it.
    """
    if not is_exposed(config.ui.host):
        return ""
    where = "every network this machine is on" if config.ui.host in ("0.0.0.0", "::", "") \
        else f"anything that can reach {config.ui.host}"
    return (
        f"WARNING: the dashboard is bound to {config.ui.host}:{config.ui.port}, not loopback. "
        f"It has NO authentication and its stop button ends the run, so {where} can "
        "control this daemon. Set ui.host to 127.0.0.1 and tunnel in, or put an "
        "authenticating proxy in front of it."
    )


def bound_handler(config: Config, store: Store) -> type[Handler]:
    """The handler class with this run's store and config bound in.

    `serve` builds it this way, and a test can build the same class against a
    fixture store and bind it to an ephemeral port without starting a daemon.
    """
    return type("BoundHandler", (Handler,), {"store": store, "config": config})


def serve(config: Config, store: Store) -> ThreadingHTTPServer:
    """Start the dashboard on a background thread and return the server."""
    warning = exposure_warning(config)
    if warning:
        print(warning, file=sys.stderr)

    handler = bound_handler(config, store)
    server = ThreadingHTTPServer((config.ui.host, config.ui.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="forge-ui").start()
    return server


def url_for(config: Config) -> str:
    host = "localhost" if config.ui.host in ("0.0.0.0", "127.0.0.1", "") else config.ui.host
    return f"http://{host}:{config.ui.port}"
