"""The two defects the blind-grading runs exposed, and their repairs.

Both were found by running `docs/BLIND-GRADING.md`'s `arm-blind` against a
linter it could not see. Neither was the thing the experiment was looking for,
and both are worse than the thing it was looking for.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from forge.failures import classify, signatures  # noqa: E402
import sample_workspace  # noqa: E402

# One run's real output, trimmed. Two files, three findings, two codes.
LINT_OUTPUT = (
    "./tests/stream_test.py:10:51: E501 line too long (71 > 50 characters)\n"
    "./tests/stream_test.py:15:51: E501 line too long (71 > 50 characters)\n"
    "./wordcount/stream.py:24:51: E501 line too long (61 > 50 characters)\n"
    "./wordcount/stream.py:3:1: F401 'os' imported but unused\n"
)


class TestLintOutputParsesAsDiagnostics(unittest.TestCase):
    """`path:line:col: E501 message` carries the word `error` nowhere, so every
    pattern in `_ERROR` missed it and a whole lint run parsed to zero blocks.

    `signatures` then returned the empty set. Its own docstring says that means
    *cannot attribute* rather than *no errors*, and both of its callers were
    reading it the other way: the compile gate announced `0 compile error(s) to
    answer` about 24 findings, and baseline amnesty could not tell a lint
    failure a ticket introduced from one it inherited."""

    def test_every_finding_is_a_signature(self):
        found = signatures(LINT_OUTPUT)

        self.assertEqual(len(found), 4)
        self.assertTrue(any("e501" in key for key in found))
        self.assertTrue(any("f401" in key for key in found))

    def test_two_findings_in_different_files_are_different_signatures(self):
        # The property baseline amnesty rests on. Collapsed together, a ticket
        # is forgiven its own failure because the same code fired somewhere it
        # may not open.
        one = signatures("./a/x.py:1:1: E501 line too long (60 > 50 characters)\n")
        other = signatures("./b/y.py:1:1: E501 line too long (60 > 50 characters)\n")

        self.assertEqual(len(one), 1)
        self.assertEqual(len(other), 1)
        self.assertNotEqual(one, other)

    def test_a_windows_path_parses_the_same_way(self):
        # flake8 prints the separator the platform gave it, and the runs this
        # comes from are on Windows.
        found = signatures(
            ".\\tests\\stream_test.py:10:51: E501 line too long (71 > 50 characters)\n"
        )

        self.assertEqual(len(found), 1)

    def test_the_class_names_the_code_and_the_file(self):
        # The parser fix reaches classification too, and improves it. The run
        # this comes from recorded every finding as one class — a masked
        # message, `lint : e5# line too long # # characters` — because nothing
        # parsed as a block, so `E501` at any width in any file was the same
        # thing. Named by code and file, two files are two classes.
        names = classify("lint", LINT_OUTPUT)

        self.assertIn("lint E501 in ./tests/stream_test.py", names)
        self.assertIn("lint E501 in ./wordcount/stream.py", names)
        self.assertIn("lint F401 in ./wordcount/stream.py", names)

    def test_a_continuation_line_does_not_open_a_block(self):
        # `tests/test_scan.py:12: in <module>` is pytest naming the location of
        # the failure above it. Read as a diagnostic of its own it would split
        # one failure into two and attribute neither.
        found = signatures(
            "E   AssertionError: assert 1 == 2\n"
            "tests/test_scan.py:12: in <module>\n"
        )

        self.assertEqual(len(found), 1)

    def test_a_lower_case_word_that_looks_like_a_code_is_not_one(self):
        # `_ERROR` is compiled IGNORECASE, so the code alternative is guarded
        # case-sensitively. Without that, prose mentioning a file is an error.
        self.assertEqual(signatures("see notes.md:12:1: ab12 for the rest\n"), set())


class TestTheCopyIsAGitRepository(unittest.TestCase):
    """Without one, `_snapshot` returns `""`, `baseline_tree` is never
    recorded, and `_quarantine` refuses to revert a failed ticket's files —
    correctly, because deleting on a guess could take a hand-written file. So
    the files stay, the next cycle reads them as pre-existing, and whatever
    they break is excused for every ticket after rather than fixed.

    Nine fixture runs went that way before the warning was read."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="forge-copy-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.base, ignore_errors=True)

    def test_a_copy_is_a_repository_with_the_fixture_committed(self):
        target = sample_workspace.copy_sample(self.base / "copy")

        self.assertTrue((target / ".git").is_dir())
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.strip(), "", "the copy starts dirty")

    def test_the_commit_holds_the_fixture_rather_than_an_empty_tree(self):
        target = sample_workspace.copy_sample(self.base / "copy")

        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
        tracked = set(listed.stdout.split())

        self.assertIn("wordcount/counter.py", tracked)
        self.assertIn(".flake8", tracked)
        self.assertIn("plugin/histogram/bars.py", tracked)

    def test_it_can_be_turned_off_for_inspecting_the_copy_itself(self):
        target = sample_workspace.copy_sample(self.base / "plain", repo=False)

        self.assertFalse((target / ".git").exists())


class TestDoctorSaysWhenQuarantineCannotWork(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="forge-doctor-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.base, ignore_errors=True)

    def _report(self, root: Path) -> str:
        import io
        from contextlib import redirect_stdout

        from forge.cli import _report_version_control
        from forge.config import Config

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            inside = _report_version_control(Config.load(root))
        return f"{inside}\n{buffer.getvalue()}"

    def test_a_tree_without_git_is_reported(self):
        target = sample_workspace.copy_sample(self.base / "plain", repo=False)

        report = self._report(target)

        self.assertTrue(report.startswith("False"))
        self.assertIn("not a git repository", report)
        self.assertIn("excused", report)

    def test_a_repository_is_not_reported(self):
        # Said only when it is a problem. A line printed on every healthy run
        # is a line nobody reads on the run where it matters.
        target = sample_workspace.copy_sample(self.base / "repo")

        report = self._report(target)

        self.assertTrue(report.startswith("True"))
        self.assertNotIn("not a git repository", report)


if __name__ == "__main__":
    unittest.main()
