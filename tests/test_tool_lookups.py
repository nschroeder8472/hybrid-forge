"""Two tools for the two-call lookup the ledger is full of.

Across 597 recorded tool calls the executor spent 61.6% on `read_file` and
31.3% on `grep`, and **104 of 187 greps were followed immediately by a
`read_file` of the file just matched**. A match is a line number and nothing
else, so seeing what it means costs a second call; and a definition is found by
grepping for `def name` and then reading a range around it chosen by eye, which
is a guess that either cuts the definition off or drags in its neighbours.

`grep` now takes `context`, and `read_symbol` reads a definition whole by name.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.providers import ToolCall
from forge.tools import TOOL_NAMES, Toolbox

SOURCE = '''"""A module."""


def alpha(one, two):
    return one + two


class Thing:
    """Docstring."""

    def method(self, value):
        inner = value * 2
        return inner

    @property
    def decorated(self):
        return 1


def omega():
    return alpha(1, 2)
'''


class TestGrepContext(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "mod.py").write_text(SOURCE, encoding="utf-8")
        self.box = Toolbox(self.root)

    def _grep(self, **arguments) -> str:
        return self.box.run(ToolCall("1", "grep", arguments)).content

    def test_without_context_a_match_is_one_line(self):
        """What it always was, and what every caller already parses."""
        out = self._grep(pattern=r"def method")

        self.assertEqual(out.strip(), "mod.py:11: def method(self, value):")

    def test_context_returns_the_lines_either_side(self):
        out = self._grep(pattern=r"def method", context=2)

        self.assertIn("Docstring", out)
        self.assertIn("inner = value * 2", out)

    def test_the_matched_line_is_still_marked_apart(self):
        """A reader has to be able to tell which line the pattern actually hit
        from the ones that came along with it."""
        out = self._grep(pattern=r"def method", context=1)
        hit = [line for line in out.splitlines() if "def method" in line]

        self.assertTrue(hit[0].split("mod.py:")[1].startswith("11:"))
        self.assertIn("mod.py:10-", out)

    def test_context_is_capped_rather_than_unbounded(self):
        """It answers the follow-up question; it is not a way to read a file."""
        out = self._grep(pattern=r"def method", context=9999)

        self.assertLessEqual(len(out.splitlines()), 60)

    def test_a_context_that_is_not_a_number_is_refused(self):
        out = self._grep(pattern=r"def method", context="lots")

        self.assertIn("whole number", out)


class TestReadSymbol(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "mod.py").write_text(SOURCE, encoding="utf-8")
        self.box = Toolbox(self.root)

    def _read(self, **arguments) -> str:
        return self.box.run(ToolCall("1", "read_symbol", arguments)).content

    def test_it_is_offered_to_the_model(self):
        self.assertIn("read_symbol", TOOL_NAMES)

    def test_a_function_comes_back_whole(self):
        out = self._read(path="mod.py", name="alpha")

        self.assertIn("def alpha(one, two):", out)
        self.assertIn("return one + two", out)
        self.assertNotIn("class Thing", out)

    def test_a_method_is_addressed_through_its_class(self):
        out = self._read(path="mod.py", name="Thing.method")

        self.assertIn("inner = value * 2", out)
        self.assertNotIn("def omega", out)

    def test_a_bare_method_name_is_found_too(self):
        """A caller who knows the method and not the class is the common case,
        and refusing them costs a turn."""
        out = self._read(path="mod.py", name="method")

        self.assertIn("inner = value * 2", out)

    def test_a_decorator_comes_with_the_definition(self):
        """A function shown without its `@property` behaves differently from
        the one on disk."""
        out = self._read(path="mod.py", name="Thing.decorated")

        self.assertIn("@property", out)

    def test_a_class_comes_back_with_its_methods(self):
        out = self._read(path="mod.py", name="Thing")

        self.assertIn("class Thing", out)
        self.assertIn("def method", out)

    def test_the_lines_are_numbered_so_a_later_edit_can_be_placed(self):
        out = self._read(path="mod.py", name="alpha")

        self.assertIn("mod.py:4-5", out)

    def test_a_name_that_is_absent_answers_with_the_names_that_are(self):
        """Otherwise finding out what to ask for costs another call, and that
        call is `outline` — which the caller could have run first and did not."""
        out = self._read(path="mod.py", name="missing")

        self.assertIn("declares no `missing`", out)
        self.assertIn("alpha", out)
        self.assertIn("Thing", out)

    def test_a_missing_name_argument_says_what_one_looks_like(self):
        out = self._read(path="mod.py")

        self.assertIn("`name` is required", out)

    def test_a_file_that_is_not_python_says_what_to_use_instead(self):
        (self.root / "notes.md").write_text("# hello\n", encoding="utf-8")

        out = self._read(path="notes.md", name="hello")

        self.assertIn("Python only", out)
        self.assertIn("grep", out)


if __name__ == "__main__":
    unittest.main()
