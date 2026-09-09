"""The dashboard's write endpoints, served over a real socket.

HW-004. `apply_write` in `forge/ui/writes.py` turns one decoded request into
one of the three writes — a note, a criterion, a release — and this pins the
wiring that hands it the request: `do_POST` reads the body, decodes it, and
sends back what `apply_write` returns, while `/api/control` keeps the four
enumerated commands it always had.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from forge.config import Config
from forge.loop import CONTROL_KEY, CONTROL_PAUSE, CONTROL_RUN
from forge.state import Store, Ticket
from forge.ui import server as ui_server


def _config(root: Path) -> Config:
    return Config(
        root=root,
        models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
        roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
    )


def _store(root: Path) -> Store:
    store = Store(root / "t.db")
    run_id = store.create_run("goal")
    store.add_tickets(
        run_id,
        [Ticket("T-1", title="the parked one", route="withheld:security", status="blocked")],
    )
    return store


def _request(server: ThreadingHTTPServer, method: str, path: str, body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=body,
        method=method,
    )
    # Bounded on purpose: a regression that stops the server answering should
    # fail this suite in ten seconds rather than hold the verify step open
    # until somebody kills it.
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as answer:
        # 400 and 404 are answers this suite asserts rather than accidents.
        # `urlopen` raises on them and carries the body on the exception.
        with answer:
            return answer.code, json.loads(answer.read().decode("utf-8"))


class TestTheWriteEndpointsAreServed(unittest.TestCase):
    """One server, bound to an ephemeral loopback port, for the whole set."""

    def setUp(self):
        # `mkdtemp` rather than `TemporaryDirectory`, which is what every other
        # store-backed test here uses. `Store` opens a connection per thread and
        # `close()` can only close the caller's — and Windows refuses to unlink
        # a database file a connection still holds, which failed every test in
        # this class through its cleanup rather than its assertions. The
        # handler now closes its thread's connection as the connection ends,
        # which is what `test_a_served_connection_does_not_leave_one_open`
        # pins; the directory is still left behind rather than trusted to a
        # cleanup ordering.
        root = Path(tempfile.mkdtemp()).resolve()
        self.store = _store(root)
        self.addCleanup(self.store.close)
        self.config = _config(root)
        handler = ui_server.bound_handler(self.config, self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # Something has to accept the connections. Without this the first
        # request blocks on a socket nobody is listening to, and `shutdown()`
        # blocks in turn on an event only `serve_forever` ever sets — one run
        # sat in this file for eight hours.
        serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        serving.start()
        # Cleanups run last-registered-first, so this reads bottom-up:
        # `shutdown` leaves the serve loop, the join waits for the thread to
        # notice, and `server_close` releases the socket once nothing is in it.
        self.addCleanup(self.server.server_close)
        self.addCleanup(serving.join, 5)
        self.addCleanup(self.server.shutdown)

    def test_a_served_connection_does_not_leave_one_open(self):
        """The dashboard used to hand every connection thread's handle to the GC.

        `ThreadingHTTPServer` runs a thread per connection and `Store` opens a
        connection per thread, so a browser polling the dashboard left a trail
        of open SQLite handles — each one a Windows lock on the run database —
        for refcounting to clear whenever it got to them.
        """
        before = len(self.store._open)

        for _ in range(3):
            status, _ = _request(self.server, "POST", "/api/control", b'{"command": "pause"}')
            self.assertEqual(status, 200)

        # `urlopen` does not reuse a connection, so each request above was its
        # own thread with its own connection. All three are closed.
        self.assertEqual(len(self.store._open), before)

    def test_bound_handler_binds_the_store_and_the_config(self):
        handler = ui_server.bound_handler(self.config, self.store)

        self.assertIs(handler.store, self.store)
        self.assertIs(handler.config, self.config)

    def test_a_note_lands_in_the_ticket(self):
        status, body = _request(
            self.server, "POST", "/api/ticket/T-1/note", b'{"text": "the fixtures exist now"}'
        )

        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        ticket = self.store.list_tickets(self.store.latest_run()["id"])[0]
        self.assertEqual(len(ticket.human_note), 1)
        self.assertEqual(ticket.human_note[0]["text"], "the fixtures exist now")

    def test_a_body_that_is_not_json_keeps_the_control_answer(self):
        status, body = _request(self.server, "POST", "/api/ticket/T-1/note", b"not json")

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid JSON")

    def test_the_control_command_still_writes_the_control_table(self):
        status, body = _request(self.server, "POST", "/api/control", b'{"command": "pause"}')

        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(self.store.get_control(CONTROL_KEY, CONTROL_RUN), CONTROL_PAUSE)

    def test_an_unknown_path_is_a_404(self):
        status, _ = _request(self.server, "POST", "/api/nope", b"{}")

        self.assertEqual(status, 404)

    def test_the_state_endpoint_still_answers(self):
        status, body = _request(self.server, "GET", "/api/state", b"")

        self.assertEqual(status, 200)
        self.assertEqual([entry["id"] for entry in body["tickets"]], ["T-1"])

    def test_a_release_moves_the_route(self):
        status, _ = _request(
            self.server,
            "POST",
            "/api/ticket/T-1/route",
            b'{"route": "delegate", "text": "answered"}',
        )

        self.assertEqual(status, 200)
        ticket = self.store.list_tickets(self.store.latest_run()["id"])[0]
        self.assertEqual(ticket.route, "delegate")


if __name__ == "__main__":
    unittest.main()
