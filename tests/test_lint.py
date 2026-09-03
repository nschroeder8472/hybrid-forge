"""The repository is flake8-clean, and that is enforced rather than suggested.

Lint was switched on because the fixture could not produce the failure the loop
was built for. `examples/sample-project` graded a ticket on its unit tests and
nothing else — `lint` and `typecheck` were both `skip` — while the run every
convergence feature was derived from failed overwhelmingly on lint and compiler
output: 1,125 trailing-whitespace occurrences on one ticket, 512 `TS2532` on
another, and 117 of one ticket's 160 lint failures with whitespace as their only
problem. A fixture that cannot fail that way cannot exercise the brakes.

Grading generated code by a linter this project does not run on itself would be
the wrong way round, so this runs the same tool over the same tree, and a
missing flake8 fails rather than skips: an enforcement that quietly disappears
on the machine that lacks the tool enforces nothing.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestTheRepositoryIsClean(unittest.TestCase):
    def test_flake8_is_installed(self):
        # Required, not optional. `pip install -e ".[dev]"` brings it in.
        result = subprocess.run(
            [sys.executable, "-m", "flake8", "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            "flake8 is required: install it with `pip install -e \".[dev]\"`",
        )

    def test_the_tree_has_no_findings(self):
        # `.flake8` at the root carries the rules; nothing is passed here, so a
        # reader running `flake8` by hand gets exactly what this gets.
        result = subprocess.run(
            [sys.executable, "-m", "flake8"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            "flake8 findings:\n" + result.stdout + result.stderr,
        )


class TestTheFixtureIsGradedByItsOwnLinter(unittest.TestCase):
    """Each build's lint command, run as written from its own directory. An
    equivalent command would prove something about this test rather than about
    the fixture, and the fixture shipping red would stop every run at the
    baseline."""

    SAMPLE = ROOT / "examples" / "sample-project"

    def _lint(self, build: str) -> subprocess.CompletedProcess:
        import os

        from forge.config import Config

        config = Config.load(self.SAMPLE)
        workspace = next(w for w in config.workspaces if w.root == build)
        command = workspace.covering("lint", ".py")[0]
        self.assertTrue(command, f"{build} has no lint command for .py")
        self.assertNotEqual(command, "skip", f"{build} still skips lint for .py")
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(  # noqa: S602 - the fixture's own command, by design
            command,
            shell=True,
            cwd=workspace.path(self.SAMPLE),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )

    def test_the_root_build_lints_clean(self):
        result = self._lint(".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_plugin_build_lints_clean(self):
        result = self._lint("plugin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_build_carries_a_config_inside_its_own_workspace(self):
        # `toolchain_context` resolves a linter config by walking from a
        # writable file up to its *workspace* root. A config above that root is
        # one the roles working in the build are graded against and never
        # shown, which is the exact failure Feature 1 exists to stop.
        for build in (".", "plugin"):
            self.assertTrue((self.SAMPLE / build / ".flake8").is_file(), build)


if __name__ == "__main__":
    unittest.main()
