"""The executor edits a file instead of restating it.

A whole-file reply costs the size of the *existing* file, not of the change.
Running `RETENTION.md` against this repository on 2026-09-08 spent 2,647,873
tokens across three attempts and wrote nothing, because both files the ticket
allowed — 96,548 and 110,903 characters — exceed on their own what the executor
is given to answer with. The sample project cannot show this: its files are one
and two kilobytes.

The safety of the replacement form is one rule. A search block has to match
exactly once, because a block that matches twice and is applied to whichever
came first corrupts a file quietly, in a place nobody is looking, and that is
worse than the failure it replaces.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.patch import (
    FileEdit,
    Replacement,
    apply_edits,
    describe_unparsed,
    names_a_file,
    parse_output,
)
from forge.prompts import EXECUTOR_SYSTEM

ONE = """forge/state.py
```
<<<<<<< SEARCH
def a():
    return 1
=======
def a():
    return 2
>>>>>>> REPLACE
```
"""

TWO = """forge/state.py
```
<<<<<<< SEARCH
def a():
=======
def one():
>>>>>>> REPLACE
<<<<<<< SEARCH
def b():
=======
def two():
>>>>>>> REPLACE
```
"""


class TestReadingAReplacementBlock(unittest.TestCase):
    def test_one_replacement_carries_both_halves(self):
        edit = parse_output(ONE).edits[0]

        self.assertEqual(edit.path, "forge/state.py")
        self.assertEqual(len(edit.replacements), 1)
        self.assertEqual(edit.replacements[0].search, "def a():\n    return 1")
        self.assertEqual(edit.replacements[0].replace, "def a():\n    return 2")

    def test_two_replacements_keep_their_order(self):
        edit = parse_output(TWO).edits[0]

        self.assertEqual(
            [(r.search, r.replace) for r in edit.replacements],
            [("def a():", "def one():"), ("def b():", "def two():")],
        )

    def test_a_body_with_no_markers_is_still_a_whole_file(self):
        edit = parse_output("a.py\n```\nx = 1\n```\n").edits[0]

        self.assertEqual(edit.replacements, [])
        self.assertEqual(edit.content, "x = 1\n")

    def test_content_holds_the_new_code_so_the_guards_can_read_it(self):
        """`foreign_bindings`, `laundered_assertions` and `weakened_criteria`
        all read `content`. Leaving it empty on a patch-shaped reply would take
        three guards out at once, silently."""
        edit = parse_output(TWO).edits[0]

        self.assertEqual(edit.content, "def one():\ndef two():")

    def test_an_unfinished_marker_is_read_as_a_file_rather_than_lost(self):
        """A truncated reply, or a file that itself contains the marker. Both
        were whole files before this existed and stay whole files."""
        edit = parse_output("a.py\n```\n<<<<<<< SEARCH\nhalf\n```\n").edits[0]

        self.assertEqual(edit.replacements, [])
        self.assertIn("half", edit.content)


class TestApplyingAReplacement(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()

    def _file(self, text: str, name: str = "a.py") -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_unique_match_is_replaced(self):
        path = self._file("before\ntarget\nafter\n")

        written = apply_edits(
            self.root,
            [FileEdit("a.py", "new", [Replacement("target", "new")])],
        )

        self.assertEqual(written, ["a.py"])
        self.assertEqual(path.read_text(encoding="utf-8"), "before\nnew\nafter\n")

    def test_a_match_that_is_not_unique_is_refused_and_nothing_is_written(self):
        path = self._file("target\ntarget\n")

        with self.assertRaises(ValueError) as caught:
            apply_edits(
                self.root,
                [FileEdit("a.py", "new", [Replacement("target", "new")])],
            )

        self.assertIn("matched 2 times", str(caught.exception))
        self.assertIn("unique", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "target\ntarget\n")

    def test_a_match_that_is_absent_is_refused_and_nothing_is_written(self):
        path = self._file("nothing like it\n")

        with self.assertRaises(ValueError) as caught:
            apply_edits(
                self.root,
                [FileEdit("a.py", "new", [Replacement("target", "new")])],
            )

        self.assertIn("matched 0 times", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "nothing like it\n")

    def test_a_later_failure_leaves_the_file_as_it_was(self):
        """Every replacement lands on one in-memory copy and the file is
        written once. A refusal partway through is not half an edit."""
        path = self._file("first\nsecond\n")

        with self.assertRaises(ValueError):
            apply_edits(
                self.root,
                [
                    FileEdit(
                        "a.py",
                        "x",
                        [
                            Replacement("first", "1st"),
                            Replacement("absent", "never"),
                        ],
                    )
                ],
            )

        self.assertEqual(path.read_text(encoding="utf-8"), "first\nsecond\n")

    def test_a_later_search_may_match_what_an_earlier_one_wrote(self):
        path = self._file("alpha\n")

        apply_edits(
            self.root,
            [
                FileEdit(
                    "a.py",
                    "x",
                    [Replacement("alpha", "beta"), Replacement("beta", "gamma")],
                )
            ],
        )

        self.assertEqual(path.read_text(encoding="utf-8"), "gamma\n")

    def test_an_empty_search_is_refused_rather_than_matching_everywhere(self):
        path = self._file("anything\n")

        with self.assertRaises(ValueError) as caught:
            apply_edits(self.root, [FileEdit("a.py", "x", [Replacement("", "x")])])

        self.assertIn("empty SEARCH", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "anything\n")

    def test_a_file_that_does_not_exist_cannot_be_edited(self):
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                self.root,
                [FileEdit("missing.py", "x", [Replacement("a", "b")])],
            )

        self.assertIn("does not exist", str(caught.exception))
        self.assertIn("whole", str(caught.exception))

    def test_a_whole_file_edit_is_written_exactly_as_before(self):
        apply_edits(self.root, [FileEdit("new.py", "x = 1\n")])

        self.assertEqual(
            (self.root / "new.py").read_text(encoding="utf-8"), "x = 1\n"
        )


class TestTheExecutorIsToldAboutIt(unittest.TestCase):
    """A shape the parser accepts and the prompt never mentions is dead code."""

    def test_the_replacement_form_is_in_the_build_instructions(self):
        self.assertIn("<<<<<<< SEARCH", EXECUTOR_SYSTEM)
        self.assertIn(">>>>>>> REPLACE", EXECUTOR_SYSTEM)

    def test_it_says_the_search_has_to_match_once(self):
        self.assertIn("exactly once", EXECUTOR_SYSTEM)

    def test_it_says_a_new_file_is_still_sent_whole(self):
        self.assertIn("has to be sent whole", EXECUTOR_SYSTEM)

    def test_it_says_the_blocks_come_before_any_prose(self):
        """The attempt lost to narration was lost to a rule nothing stated."""
        self.assertIn("Blocks first, prose never", EXECUTOR_SYSTEM)


class TestWhetherAReplyGotAsFarAsNamingAFile(unittest.TestCase):
    """Which of two opposite corrections a cut-off reply is owed."""

    def test_a_closed_block_names_one(self):
        self.assertTrue(names_a_file("app.py\n```\nx = 1\n```"))

    def test_a_path_line_whose_fence_never_closed_still_names_one(self):
        # What a reply cut off mid-file looks like: the block is open, so
        # `parse_output` yields nothing, and the path line is the evidence.
        self.assertTrue(names_a_file("app.py\n```python\ndef half("))

    def test_prose_alone_names_nothing(self):
        self.assertFalse(
            names_a_file(
                "I've reviewed the code. The problem is in parse(), which "
                "needs a guard for the empty case."
            )
        )

    def test_a_filename_inside_a_sentence_is_not_a_path_line(self):
        """Otherwise every narration would be read as having started work."""
        self.assertFalse(
            names_a_file("First I will change app.py, then the test beside it.")
        )


class TestATextToolCallIsNamedForWhatItIs(unittest.TestCase):
    """A model out of tool turns sometimes writes the call out instead of
    making it. The path it asks to read sits on a line of its own, so the
    path-and-fence heuristics claimed it had "named files but did not fence
    their contents" -- a correction about a mistake it had not made, which left
    it no reason to stop making the one it had. Three attempts of one real
    ticket went that way, twice over, across three runs.
    """

    WHOLE_FILE = "a.py\n```\nx = 1\n```\n"

    UNFENCED = "a.py\nx = 1\n"

    CALL = (
        "I have enough context. Let me verify one detail first.\n\n"
        "<tool_call>\n<function=read_file>\n<parameter=path>\n"
        "forge/config.py\n</parameter>\n</function>\n</tool_call>\n"
    )

    def test_it_is_not_reported_as_a_missing_fence(self):
        said = describe_unparsed(self.CALL)

        self.assertNotIn("did not fence", said)

    def test_it_says_the_call_was_only_text(self):
        said = describe_unparsed(self.CALL)

        self.assertIn("tool call written out as text", said)

    def test_it_says_what_to_do_instead(self):
        said = describe_unparsed(self.CALL)

        self.assertIn("Answer now from what you have already read", said)
        self.assertIn("BLOCKED:", said)

    def test_a_reply_that_parsed_is_still_left_alone(self):
        """The guard runs on replies with no files in them. One that parsed
        is not the caller's question however it is worded."""
        self.assertEqual(describe_unparsed(self.WHOLE_FILE), "")

    def test_an_ordinary_missing_fence_still_says_so(self):
        said = describe_unparsed(self.UNFENCED)

        self.assertIn("did not fence", said)


if __name__ == "__main__":
    unittest.main()
