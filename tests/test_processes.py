"""`run_command`: a bound that holds even when the shell is not what hung.

HT-001. The behaviour under test is not "a timeout exists" — `subprocess.run`
has one and it did not stop an eight-hour verify step. It is that a process
which outlived its shell is dead afterwards, and that the parent returns at all
while that process is holding the pipes it inherited.

The timings here are deliberate and are the second thing this file is about.
Ratification rejected an earlier draft of these criteria for racing interpreter
start against a 3-second bound, so:

  * the timeout is 6 seconds, against a Python start of a few hundred
    milliseconds — around twenty times the headroom, so `working` and
    `started.txt` are not in a race with the kill;
  * the grandchild waits 11 seconds before writing `survived.txt` — safely
    past the 6-second bound, so a grandchild that wrote it *before* the kill
    was even due cannot fail a working implementation;
  * that file is checked 14 seconds after the call returned, about 20 seconds
    after the grandchild began, so a survivor has had well over the time it
    needs and an absent file means killed rather than not-yet.

Every number here is the smallest one that keeps both margins, because this
file runs on every verify step of every ticket.

    python -m unittest discover tests
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from forge.processes import Outcome, run_command

# Long enough that a loaded box cannot make the interpreter miss it, short
# enough that the suite does not notice. Python starts in a few hundred
# milliseconds, so this is roughly twenty times the headroom needed; a
# three-second bound was rejected at ratification for racing that start.
TIMEOUT = 6
# What the grandchild waits before claiming it survived. It has to be safely
# longer than TIMEOUT: a grandchild that wrote its file *before* the kill was
# due would fail this suite against an implementation that works.
GRANDCHILD_SLEEP = 11
# How long after the call returns the claim is checked. Past the sleep above
# with room to spare, so an absent file means killed rather than not-yet.
SETTLE = 14


def workspace() -> Path:
    """A directory the child can write into. Not cleaned: Windows keeps a
    handle on anything a killed grandchild touched, and the temp directory is
    the operating system's to sweep."""
    return Path(tempfile.mkdtemp()).resolve()


def script(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def invoke(path: Path) -> str:
    """The command line that runs `path`, quoted for cmd.exe and for sh."""
    return f'"{sys.executable}" "{path}"'


class TestAnOrdinaryCommand(unittest.TestCase):
    """The path every verify step takes, where nothing goes wrong."""

    def test_it_returns_the_output_and_a_zero_code(self):
        root = workspace()
        path = script(root, "hello.py", "print('hi')")

        outcome = run_command(invoke(path), root, TIMEOUT)

        self.assertIsInstance(outcome, Outcome)
        self.assertEqual(outcome.returncode, 0)
        self.assertIn("hi", outcome.output)
        self.assertFalse(outcome.timed_out)

    def test_stderr_arrives_in_the_same_output(self):
        root = workspace()
        path = script(root, "noise.py", """
            import sys
            sys.stdout.write('out\\n')
            sys.stderr.write('boom\\n')
        """)

        outcome = run_command(invoke(path), root, TIMEOUT)

        self.assertIn("out", outcome.output)
        self.assertIn("boom", outcome.output)

    def test_a_failing_command_reports_its_own_exit_code(self):
        root = workspace()
        path = script(root, "fail.py", """
            import sys
            sys.exit(3)
        """)

        outcome = run_command(invoke(path), root, TIMEOUT)

        self.assertEqual(outcome.returncode, 3)
        self.assertFalse(outcome.timed_out)

    def test_it_runs_in_the_directory_it_was_given(self):
        root = workspace()
        (root / "marker.txt").write_text("here", encoding="utf-8")
        path = script(root, "listing.py", """
            import os
            print(sorted(os.listdir('.')))
        """)

        outcome = run_command(invoke(path), root, TIMEOUT)

        self.assertIn("marker.txt", outcome.output)


class TestACommandThatOverstaysIsKilled(unittest.TestCase):
    """The timeout on its own, before the grandchild is brought into it."""

    def test_a_sleeping_command_is_cut_and_says_so(self):
        root = workspace()
        path = script(root, "sleeper.py", """
            import time
            time.sleep(120)
        """)

        started = time.monotonic()
        outcome = run_command(invoke(path), root, TIMEOUT)
        elapsed = time.monotonic() - started

        self.assertTrue(outcome.timed_out)
        self.assertNotEqual(outcome.returncode, 0)
        self.assertLess(elapsed, TIMEOUT + 45)


class TestTheTreeDiesNotJustTheShell(unittest.TestCase):
    """The failure this module exists for.

    A grandchild of the shell, holding the pipes the shell was given, is what
    turned a 1,800-second timeout into an eight-hour step. So: the command
    prints, spawns something that outlives it, and sleeps far past the bound.
    """

    @classmethod
    def setUpClass(cls):
        """One timed-out call, waited out once. Each assertion below reads it.

        Done here rather than per test because the call costs the timeout and
        the wait costs the settle window, and this suite runs on every verify
        step of every ticket. Paying that twice to assert two things about one
        event buys nothing.
        """
        cls.root = workspace()
        script(cls.root, "grandchild.py", f"""
            import pathlib, time
            here = pathlib.Path(__file__).parent
            (here / 'started.txt').write_text('yes', encoding='utf-8')
            time.sleep({GRANDCHILD_SLEEP})
            (here / 'survived.txt').write_text('yes', encoding='utf-8')
        """)
        parent = script(cls.root, "parent.py", """
            import subprocess, sys, pathlib, time
            print('working', flush=True)
            here = pathlib.Path(__file__).parent
            subprocess.Popen([sys.executable, str(here / 'grandchild.py')], cwd=str(here))
            time.sleep(120)
        """)

        started = time.monotonic()
        cls.outcome = run_command(invoke(parent), cls.root, TIMEOUT)
        cls.elapsed = time.monotonic() - started

        # Wait out the window in which a living grandchild would announce
        # itself. Polled rather than slept, so a failed kill is provable as
        # soon as the file appears instead of at the deadline.
        cls.survived = cls.root / "survived.txt"
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline and not cls.survived.exists():
            time.sleep(0.5)

    def test_the_call_returns_rather_than_blocking_on_the_survivor(self):
        self.assertTrue(self.outcome.timed_out)
        self.assertLess(self.elapsed, TIMEOUT + 45)

    def test_it_returns_what_the_command_managed_to_say(self):
        self.assertIn("working", self.outcome.output)

    def test_the_grandchild_really_ran(self):
        # What makes its later absence mean something: an implementation that
        # spawns nothing also leaves no `survived.txt`.
        self.assertTrue(
            (self.root / "started.txt").exists(),
            "the grandchild never ran, so the next test proves nothing",
        )

    def test_the_grandchild_did_not_outlive_the_kill(self):
        self.assertFalse(
            self.survived.exists(),
            f"the grandchild outlived the kill: it woke after "
            f"{GRANDCHILD_SLEEP}s and wrote {self.survived.name}",
        )


class TestTheTemporaryFileDoesNotAccumulate(unittest.TestCase):
    """Output goes to a file per call, and the file goes away."""

    def test_a_completed_command_leaves_no_log_behind(self):
        root = workspace()
        path = script(root, "quiet.py", "print('done')")
        before = {p.name for p in Path(tempfile.gettempdir()).glob("forge-cmd-*")}

        run_command(invoke(path), root, TIMEOUT)

        after = {p.name for p in Path(tempfile.gettempdir()).glob("forge-cmd-*")}
        self.assertEqual(after - before, set())


if __name__ == "__main__":
    unittest.main()
