"""The monitor dashboard: a small HTTP server over the run's SQLite state.

Read-mostly by design. The loop owns the state machine; this serves a snapshot
of it plus a live event stream, and exposes exactly three writes — pause,
resume, stop — through the same control table the loop already polls. The
dashboard never reaches into the loop's internals, so a crashed UI cannot take
a run with it, and a restarted UI reattaches with no handshake.

Bound to loopback by default. There is no authentication: the control endpoint
can stop a running job, so widening the bind address puts that button on your
network. Reach it over Tailscale rather than binding to 0.0.0.0, matching the
posture of the executor host.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Config
from ..loop import CONTROL_KEY, CONTROL_PAUSE, CONTROL_RUN, CONTROL_STOP
from ..state import Store
from ..tokens import format_tokens

INDEX = Path(__file__).with_name("index.html")

_ALLOWED_COMMANDS = {
    "pause": CONTROL_PAUSE,
    "resume": CONTROL_RUN,
    "run": CONTROL_RUN,
    "stop": CONTROL_STOP,
}


def snapshot(store: Store, config: Config) -> dict[str, Any]:
    """Everything the dashboard renders, in one read."""
    run = store.active_run() or store.latest_run()
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
                "total": row["prompt_tokens"] + row["completion_tokens"],
                "display": format_tokens(row["prompt_tokens"] + row["completion_tokens"]),
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

        if path == "/api/events":
            self._stream_events(query)
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/api/control":
            self._send(404, b"not found", "text/plain")
            return

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
                {"error": f"unknown command {requested!r}", "allowed": sorted(_ALLOWED_COMMANDS)},
                code=400,
            )
            return

        self.store.set_control(CONTROL_KEY, command)
        self.store.log(None, f"Dashboard requested: {requested}", kind="control")
        self._send_json({"ok": True, "control": command})

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
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(config: Config, store: Store) -> ThreadingHTTPServer:
    """Start the dashboard on a background thread and return the server."""
    handler = type("BoundHandler", (Handler,), {"store": store, "config": config})
    server = ThreadingHTTPServer((config.ui.host, config.ui.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="forge-ui").start()
    return server


def url_for(config: Config) -> str:
    host = "localhost" if config.ui.host in ("0.0.0.0", "127.0.0.1", "") else config.ui.host
    return f"http://{host}:{config.ui.port}"
