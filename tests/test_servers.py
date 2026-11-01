"""The supervisor that starts, keeps and stops a run's servers.

Each test runs a real process: a few lines of stdlib HTTP server that answers
`/kind` with what it is and `/models` with what it serves. A supervisor tested
against a fake `Popen` would prove the bookkeeping and nothing about the part
that goes wrong — a child that outlives its parent, a port still held, a pid
that no longer means what the record says.

What this module holds to (docs/CREATIVE-TOOLS.md, *Forge runs the servers*):

1. A server forge started is recorded, found again by a fresh supervisor — the
   daemon that resumes after a crash — and stopped by it.
2. A server forge did not start is used when it serves what is needed, refused
   by name when it does not, and never stopped.
3. A port held by something else is refused, not fought over.
4. A server that dies is restarted once; the second death is an error carrying
   the log's tail.
5. A run stops what runs started and leaves what `forge llama start` started.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.processes import kill_tree  # noqa: E402
from forge.servers import (  # noqa: E402
    OWNER_CLI,
    OWNER_RUN,
    ServerError,
    ServerSpec,
    Supervisor,
    pid_alive,
)

STUB = r'''
import http.server, json, sys
port, kind, models = int(sys.argv[1]), sys.argv[2], sys.argv[3].split(",")
print("stub starting", flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/kind": kind, "/models": models}.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

CRASHER = 'import sys; print("fatal: no such checkpoint", flush=True); sys.exit(3)'


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(port: int, path: str) -> object:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
        return json.loads(r.read())


class StubCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stub = self.dir / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.port = free_port()
        self.strays: list[subprocess.Popen[bytes]] = []

    def tearDown(self) -> None:
        for proc in self.strays:
            if proc.poll() is None:
                kill_tree(proc.pid)
            proc.wait(timeout=10)
        for name in Supervisor(self.dir).records():
            Supervisor(self.dir).stop(name)
        time.sleep(0.3)
        self.tmp.cleanup()

    def spec(
        self,
        models: str = "a,b",
        want: tuple[str, ...] = ("a",),
        argv: list[str] | None = None,
    ) -> ServerSpec:
        port = self.port

        def identify() -> bool:
            try:
                return get(port, "/kind") == "stub"
            except OSError:
                return False

        def ready() -> str | None:
            try:
                have = get(port, "/models")
            except OSError as exc:
                return f"it did not answer: {exc}"
            missing = [m for m in want if m not in (have or [])]  # type: ignore[operator]
            return f"it does not serve {', '.join(missing)}" if missing else None

        return ServerSpec(
            name="stub",
            argv=argv or [sys.executable, str(self.stub), str(port), "stub", models],
            port=port,
            identify=identify,
            ready=ready,
            startup_seconds=20,
        )

    def outside(self, kind: str = "stub", models: str = "a,b") -> int:
        """A server started by somebody other than forge."""
        proc = subprocess.Popen(
            [sys.executable, str(self.stub), str(self.port), kind, models],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.strays.append(proc)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                get(self.port, "/kind")
                return proc.pid
            except OSError:
                time.sleep(0.1)
        self.fail("outside stub never came up")


class StartTests(StubCase):
    def test_starts_records_and_a_fresh_supervisor_still_owns_it(self) -> None:
        first = Supervisor(self.dir).ensure(self.spec())
        self.assertEqual(first.how, "started")
        self.assertTrue(pid_alive(first.pid or 0))

        # The daemon that resumes after a crash is a new supervisor.
        again = Supervisor(self.dir).ensure(self.spec())
        self.assertEqual(again.how, "owned")
        self.assertEqual(again.pid, first.pid)

        Supervisor(self.dir).stop_owned()
        time.sleep(0.3)
        self.assertFalse(pid_alive(first.pid or 0))
        self.assertEqual(Supervisor(self.dir).records(), {})

    def test_a_server_that_exits_while_starting_reports_its_log(self) -> None:
        crasher = self.dir / "crash.py"
        crasher.write_text(CRASHER, encoding="utf-8")
        with self.assertRaises(ServerError) as caught:
            Supervisor(self.dir).ensure(self.spec(argv=[sys.executable, str(crasher)]))
        self.assertIn("exited with code 3", str(caught.exception))
        self.assertIn("no such checkpoint", str(caught.exception))
        self.assertEqual(Supervisor(self.dir).records(), {})

    def test_a_started_server_missing_a_model_is_stopped_not_left(self) -> None:
        spec = self.spec(models="b", want=("a",))
        spec.startup_seconds = 2
        with self.assertRaises(ServerError) as caught:
            Supervisor(self.dir).ensure(spec)
        self.assertIn("does not serve a", str(caught.exception))
        self.assertEqual(Supervisor(self.dir).records(), {})


class AdoptTests(StubCase):
    def test_a_running_server_with_the_right_models_is_adopted_and_left_running(self) -> None:
        pid = self.outside()
        supervisor = Supervisor(self.dir)
        self.assertEqual(supervisor.ensure(self.spec()).how, "adopted")
        self.assertEqual(supervisor.stop_owned(), [])
        self.assertTrue(pid_alive(pid))

    def test_a_running_server_with_the_wrong_models_is_refused_by_name(self) -> None:
        self.outside(models="c")
        with self.assertRaises(ServerError) as caught:
            Supervisor(self.dir).ensure(self.spec(want=("a",)))
        self.assertIn("forge did not start", str(caught.exception))
        self.assertIn("does not serve a", str(caught.exception))

    def test_a_port_held_by_something_else_is_refused_by_port(self) -> None:
        self.outside(kind="other")
        with self.assertRaises(ServerError) as caught:
            Supervisor(self.dir).ensure(self.spec())
        self.assertIn(f"port {self.port} is in use", str(caught.exception))


class KeepTests(StubCase):
    def test_one_death_restarts_and_the_second_is_an_error_with_the_log(self) -> None:
        supervisor = Supervisor(self.dir)
        spec = self.spec()
        first = supervisor.ensure(spec)
        self.assertIsNone(supervisor.check(spec))

        kill_tree(first.pid or 0)
        time.sleep(0.5)
        note = supervisor.check(spec)
        self.assertIsNotNone(note)
        second = supervisor.ensure(spec)
        self.assertEqual(second.how, "owned")
        self.assertNotEqual(second.pid, first.pid)

        kill_tree(second.pid or 0)
        time.sleep(0.5)
        with self.assertRaises(ServerError) as caught:
            supervisor.check(spec)
        self.assertIn("died again", str(caught.exception))
        self.assertIn("stub starting", str(caught.exception))

    def test_an_adopted_server_is_never_restarted(self) -> None:
        self.outside()
        supervisor = Supervisor(self.dir)
        supervisor.ensure(self.spec())
        self.assertIsNone(supervisor.check(self.spec()))


class OwnerTests(StubCase):
    def test_a_run_leaves_a_server_forge_llama_start_started(self) -> None:
        started = Supervisor(self.dir, owner=OWNER_CLI).ensure(self.spec())
        self.assertEqual(Supervisor(self.dir, owner=OWNER_RUN).stop_owned(), [])
        self.assertTrue(pid_alive(started.pid or 0))
        # And a run still uses it as its own rather than adopting or refusing.
        self.assertEqual(Supervisor(self.dir, owner=OWNER_RUN).ensure(self.spec()).how, "owned")
        self.assertEqual(Supervisor(self.dir).stop("stub"), started.pid)

    def test_a_record_whose_process_is_gone_is_dropped(self) -> None:
        started = Supervisor(self.dir).ensure(self.spec())
        kill_tree(started.pid or 0)
        time.sleep(0.5)
        self.assertIsNone(Supervisor(self.dir).record("stub"))
        self.assertEqual(Supervisor(self.dir).records(), {})


if __name__ == "__main__":
    unittest.main()
