"""The package type-checks, and that is enforced rather than suggested.

`.flake8` was the only static analysis this project ran on itself, and flake8
does not read types. Every shape defect here has cost a whole test run to find:
a keyword argument threaded through five providers, a `Callable` alias that had
become a `Protocol` and no longer described the function passed to it, a
dataclass field whose emptiness would have silently blinded three guards.

What the first clean run actually found is worth recording, because it is not
what the argument for a type checker usually promises. There were no live
crashes. There was one name bound to two unrelated lists in one function, three
`lastrowid` reads that would raise rather than return if SQLite ever handed
back what its own stubs say it can, a `Caller` protocol whose third parameter
was named `max_tokens` against an implementation that called it `budget` — fine
until the first caller that passes it by keyword — and half a dozen locals
holding two types at different points. Traps rather than failures, which is the
class of thing a test suite is worst at finding.

`tests/test_lint.py` makes the same argument for flake8 and this follows it
exactly: a missing mypy fails rather than skips, because an enforcement that
disappears on the machine without the tool enforces nothing.

    python -m unittest discover tests
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestThePackageTypeChecks(unittest.TestCase):
    def test_mypy_is_installed(self):
        # Required, not optional. `pip install -e ".[dev]"` brings it in.
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            'mypy is required: install it with `pip install -e ".[dev]"`',
        )

    def test_the_package_has_no_findings(self):
        # `[tool.mypy]` in pyproject.toml carries the settings, `files` among
        # them, so a reader running `mypy` by hand gets exactly what this gets.
        result = subprocess.run(
            [sys.executable, "-m", "mypy"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            "mypy findings:\n" + result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
