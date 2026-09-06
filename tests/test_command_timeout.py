"""`_shell` runs under a bound it can keep, and says what the command said.

HT-002. `forge/processes.py` holds the bound; this is the half that reaches it:
the configured limit, and a step that ends `failed` carrying the output rather
than one sentence about a timeout.

The step status is asserted on purpose. The incident behind this work left a
step at `running` for eight hours, and every brake in the loop — attempts,
convergence, the escalation ladder — was waiting on that row rather than on the
command.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from forge.config import Config, LoopSettings

from tests.test_forge import _stub_orchestrator

TIMEOUT = 5
MODELS = {"m": {"kind": "openai", "model": "x", "contextWindow": 8192}}
ROLES = {role: "m" for role in ("planner", "executor", "tester", "reviewer")}


def command_for(root: Path, name: str, body: str) -> str:
    """A python script in `root`, and the quoted command line that runs it."""
    path = root / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


class TestTheConfiguredLimit(unittest.TestCase):
    """`loop.commandTimeoutSeconds`, read and written."""

    def _config_file(self, root: Path, loop: dict) -> None:
        path = root / ".hybridforge" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"models": MODELS, "roles": ROLES, "loop": loop}),
            encoding="utf-8",
        )

    def test_the_default_is_the_value_it_was_hard_coded_to(self):
        self.assertEqual(LoopSettings().command_timeout_seconds, 1800)

    def test_a_loop_block_that_says_nothing_keeps_the_default(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self._config_file(root, {"maxAttempts": 3})

        self.assertEqual(Config.load(root).loop.command_timeout_seconds, 1800)

    def test_the_camel_case_key_is_read(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self._config_file(root, {"commandTimeoutSeconds": 45})

        self.assertEqual(Config.load(root).loop.command_timeout_seconds, 45)

    def test_it_survives_being_written_back(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self._config_file(root, {"commandTimeoutSeconds": 45})
        config = Config.load(root)

        payload = json.loads(config.write().read_text(encoding="utf-8"))

        self.assertEqual(payload["loop"]["commandTimeoutSeconds"], 45)
        self.assertEqual(Config.load(root).loop.command_timeout_seconds, 45)


class TestAWedgedCommandEndsItsStep(unittest.TestCase):
    """The behaviour the eight-hour step did not have.

    One wedged command for the whole class: each test below reads the same
    call rather than paying the timeout again. This suite runs on every verify
    step of every ticket, so the assertions are cheap and the waiting is not.
    """

    @classmethod
    def setUpClass(cls):
        cls.orch, root, cls.run_id = _stub_orchestrator()
        cls.orch.config.loop.command_timeout_seconds = TIMEOUT
        command = command_for(root, "hang.py", """
            import time
            print('working', flush=True)
            time.sleep(120)
        """)

        started = time.monotonic()
        cls.result = cls.orch._shell(cls.run_id, "test", command, "TT-001")
        cls.elapsed = time.monotonic() - started
        cls.step = cls.orch.store.recent_steps(cls.run_id, limit=10)[0]

    def test_it_fails_rather_than_hanging_on_the_command(self):
        self.assertFalse(self.result.ok)
        self.assertLess(self.elapsed, TIMEOUT + 45)

    def test_the_failure_names_the_limit(self):
        self.assertIn(str(TIMEOUT), self.result.detail)

    def test_the_failure_keeps_what_the_command_printed(self):
        self.assertIn("working", self.result.detail)

    def test_the_step_is_closed_as_failed_rather_than_left_running(self):
        self.assertEqual(self.step["name"], "test")
        self.assertEqual(self.step["status"], "failed")


class TestAnOrdinaryCommandIsUnaffected(unittest.TestCase):
    """The bound changes nothing for a command that ends on its own."""

    def setUp(self):
        self.orch, self.root, self.run_id = _stub_orchestrator()
        self.orch.config.loop.command_timeout_seconds = TIMEOUT

    def _newest_step(self):
        return self.orch.store.recent_steps(self.run_id, limit=10)[0]

    def test_a_command_that_finishes_passes_and_carries_its_output(self):
        command = command_for(self.root, "fine.py", "print('fine')")

        result = self.orch._shell(self.run_id, "test", command, "TT-001")

        self.assertTrue(result.ok)
        self.assertIn("fine", result.detail)
        self.assertEqual(self._newest_step()["status"], "ok")

    def test_a_command_that_fails_reports_failed_without_a_timeout_note(self):
        command = command_for(self.root, "bad.py", """
            import sys
            print('broken')
            sys.exit(1)
        """)

        result = self.orch._shell(self.run_id, "test", command, "TT-001")

        self.assertFalse(result.ok)
        self.assertIn("broken", result.detail)
        self.assertNotIn("was killed after", result.detail)


if __name__ == "__main__":
    unittest.main()
