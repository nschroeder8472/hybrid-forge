"""The dashboard's ticket detail endpoint, read and served.

TI-001. `ticket_detail` in `forge/ui/server.py` gathers one ticket's whole
story — spec, criteria, files, route, and its failed steps oldest first — for
the run `snapshot()` is showing, and this pins both the read and the wiring
that serves it: `do_GET` answers `/api/ticket/<id>` with the dict, 404s an id
the run does not hold, and leaves `/api/state` alone.

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
from forge.state import Store, Ticket
from forge.ui import server as ui_server


def _config(root: Path) -> Config:
    return Config(
        root=root,
        models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
        roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
    )


def _ticket() -> Ticket:
    return Ticket(
        "T-1",
        title="the parked one",
        route="withheld:security",
        status="blocked",
        spec="build the thing",
        allowed_files=["x.py"],
        criteria=["a", "b"],
    )


def _store(root: Path, ticket: Ticket, failures: list[tuple[str, str]]) -> Store:
    store = Store(root / "t.db")
    run_id = store.create_run("goal")
    store.add_tickets(run_id, [ticket])
    for name, detail in failures:
        step_id = store.start_step(run_id, ticket.ticket_id, name)
        store.end_step(step_id, "failed", detail)
    return store


def _request(
    server: ThreadingHTTPServer, method: str, path: str, body: bytes
) -> tuple[int, dict]:
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
        # 404 is an answer this suite asserts rather than an accident.
        # `urlopen` raises on it and carries the body on the exception.
        with answer:
            return answer.code, json.loads(answer.read().decode("utf-8"))


class TestTicketDetail(unittest.TestCase):
    """The read itself, against a store the test owns."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.config = _config(self.root)

    def _detail(self, failures: list[tuple[str, str]]) -> dict | None:
        store = _store(self.root, _ticket(), failures)
        self.addCleanup(store.close)
        return ui_server.ticket_detail(store, self.config, "T-1")

    def test_the_spec_comes_back_whole(self):
        detail = self._detail([])

        self.assertEqual(detail["spec"], "build the thing")

    def test_criteria_and_files_come_back_as_lists(self):
        detail = self._detail([])

        self.assertEqual(detail["criteria"], ["a", "b"])
        self.assertEqual(detail["files"], ["x.py"])

    def test_the_route_is_described(self):
        detail = self._detail([])

        self.assertEqual(detail["route"], "withheld: security")

    def test_a_failed_step_comes_back_with_its_detail(self):
        detail = self._detail([("tests", "tester kept softening a criterion")])

        self.assertEqual(
            detail["failures"],
            [{"step": "tests", "detail": "tester kept softening a criterion"}],
        )

    def test_a_long_detail_is_cut_to_four_thousand_characters(self):
        detail = self._detail([("tests", "x" * 5000)])

        self.assertEqual(len(detail["failures"][0]["detail"]), 4000)

    def test_failures_come_back_oldest_first(self):
        detail = self._detail(
            [("tests", "the first failure"), ("review", "the second failure")]
        )

        self.assertEqual(
            detail["failures"],
            [
                {"step": "tests", "detail": "the first failure"},
                {"step": "review", "detail": "the second failure"},
            ],
        )

    def test_an_id_the_run_does_not_hold_is_none(self):
        store = _store(self.root, _ticket(), [])
        self.addCleanup(store.close)

        self.assertIsNone(ui_server.ticket_detail(store, self.config, "NOPE"))


class TestTheTicketDetailEndpointIsServed(unittest.TestCase):
    """One server, bound to an ephemeral loopback port, for the whole set."""

    def setUp(self):
        # `mkdtemp` rather than `TemporaryDirectory`, which is what every other
        # store-backed test here uses. `Store` opens a connection per thread and
        # `close()` can only close the caller's, so the handler threads keep
        # theirs — and Windows refuses to unlink a database file a connection
        # still holds, which failed every test in this class through its
        # cleanup rather than its assertions.
        root = Path(tempfile.mkdtemp()).resolve()
        self.store = _store(root, _ticket(), [("tests", "tester kept softening a criterion")])
        self.addCleanup(self.store.close)
        self.config = _config(root)
        handler = ui_server.bound_handler(self.config, self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # Something has to accept the connections. Without this the first
        # request blocks on a socket nobody is listening to, and `shutdown()`
        # blocks in turn on an event only `serve_forever` ever sets.
        serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        serving.start()
        # Cleanups run last-registered-first, so this reads bottom-up:
        # `shutdown` leaves the serve loop, the join waits for the thread to
        # notice, and `server_close` releases the socket once nothing is in it.
        self.addCleanup(self.server.server_close)
        self.addCleanup(serving.join, 5)
        self.addCleanup(self.server.shutdown)

    def test_the_ticket_detail_is_served(self):
        status, body = _request(self.server, "GET", "/api/ticket/T-1", b"")

        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "T-1")

    def test_an_id_the_run_does_not_hold_is_a_404(self):
        status, body = _request(self.server, "GET", "/api/ticket/NOPE", b"")

        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "no such ticket")

    def test_the_state_endpoint_still_answers(self):
        status, body = _request(self.server, "GET", "/api/state", b"")

        self.assertEqual(status, 200)
        self.assertIsInstance(body["tickets"], list)


if __name__ == "__main__":
    unittest.main()
