"""The fixture under `examples/sample-project` has to stay runnable.

It exists so a change to the loop can be *run* rather than only unit-tested,
and a fixture that has quietly rotted is worse than none: the run it produces
fails for its own reasons, and the change under test is exonerated or blamed by
whichever of them happened first.

So every property a run depends on is pinned here — the tree is green, the
spec parses, every path it names belongs to a build, and the seeded defect is
still a defect. None of these spend a token or need a model.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from forge import toolchain  # noqa: E402
from forge.config import Config  # noqa: E402
from forge.ingest import ingest  # noqa: E402
import sample_workspace  # noqa: E402
from sample_workspace import copy_sample  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


def _copy_tree(source: Path, target: Path) -> Path:
    """`copy_sample`'s copy, pointed at an arbitrary tree.

    The script copies the fixture by design — it is the only source anyone
    should be starting a run from. Testing what it *skips* needs a source that
    has some of that to skip, which the fixture must never have.
    """
    import shutil

    shutil.copytree(source, target, ignore=sample_workspace.SKIP)
    return target


class TestTheSampleProjectIsConfigured(unittest.TestCase):
    def setUp(self):
        self.config = Config.load(SAMPLE)

    def test_the_config_loads_and_validates(self):
        # A fixture whose own config is refused cannot be run at all, and the
        # refusal would read as a fault in whatever loop change came next.
        self.config.validate()

    def test_it_declares_two_builds(self):
        # The layout's whole point: the loop has to resolve a ticket to the
        # build that owns it and run that build's command from that build's
        # directory. One workspace exercises none of that.
        self.assertEqual([w.root for w in self.config.workspaces], [".", "plugin"])

    def test_both_builds_run_python_by_an_exact_command_not_a_catch_all(self):
        # A catch-all reads as coverage in every report and proves nothing
        # about the files, which is the failure the per-language work exists
        # to stop. This fixture must not model it.
        for workspace in self.config.workspaces:
            command, how = workspace.covering("test", ".py")
            self.assertTrue(command, workspace.root)
            self.assertEqual(how, "exact", workspace.root)

    def test_the_manifests_are_where_discovery_would_find_them(self):
        # `discover_workspaces` is what the wizard offers on a first setup. A
        # fixture whose builds it cannot see is not the shape it teaches.
        self.assertEqual(toolchain.discover_workspaces(SAMPLE), [".", "plugin"])

    def test_each_build_holds_one_language(self):
        # Both builds are Python on purpose: a second toolchain would make the
        # fixture unrunnable on a machine that has forge but not node.
        for build in (".", "plugin"):
            census = toolchain.census(SAMPLE, build, others=[".", "plugin"])
            self.assertEqual(sorted(census), [".py"], build)


class TestTheFixtureIsOnlyTheFixture(unittest.TestCase):
    """A run inside the fixture writes code, tickets, a database and an
    artifact tree. None of that is the fixture, and a copy of it committed by
    accident is the same failure as a rotted one: the next run starts from the
    last run's output and nothing says so.

    `.gitignore` keeps it out of a commit by allow-list; this keeps it out of
    the working tree, which is what a person reads."""

    EXPECTED = {
        ".hybridforge/.gitignore",
        ".hybridforge/config.json",
        "BUG.md",
        "README.md",
        "HARD.md",
        "SPEC.md",
        "STALL.md",
        "plugin/histogram/__init__.py",
        "plugin/histogram/bars.py",
        "plugin/pyproject.toml",
        "plugin/tests/__init__.py",
        "plugin/tests/bars_test.py",
        "pyproject.toml",
        "tests/__init__.py",
        "tests/counter_test.py",
        "wordcount/__init__.py",
        "wordcount/counter.py",
    }

    def test_it_holds_exactly_the_committed_files(self):
        found = {
            path.relative_to(SAMPLE).as_posix()
            for path in SAMPLE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }

        self.assertEqual(
            found,
            self.EXPECTED,
            "the fixture has files a run left behind, or is missing its own",
        )

    def test_the_spec_names_files_the_fixture_does_not_have_yet(self):
        # The other half of the same invariant: the backlog is work, not a
        # description of what is already there. A ticket whose files all exist
        # is one the loop can satisfy by changing nothing.
        tickets, _how, _derived = ingest(
            (SAMPLE / "SPEC.md").read_text(encoding="utf-8")
        )
        missing = [
            path
            for ticket in tickets
            for path in ticket.allowed_files
            if not (SAMPLE / path).exists()
        ]

        self.assertEqual(
            sorted(missing),
            [
                "plugin/tests/label_width_test.py",
                "tests/report_test.py",
                "tests/top_words_test.py",
                "wordcount/report.py",
            ],
        )


class TestTheSampleProjectIsGreen(unittest.TestCase):
    """`requireGreenBaseline` stops a run over a red tree, so a fixture that
    ships red cannot be used for anything. Each build's own configured command
    is run here, as written, from its own directory — an equivalent command
    would prove something about this test rather than about the fixture."""

    def _run(self, build: str) -> subprocess.CompletedProcess:
        import os

        config = Config.load(SAMPLE)
        workspace = next(w for w in config.workspaces if w.root == build)
        command = workspace.covering("test", ".py")[0]
        # Byte-code off, or checking that the fixture is clean leaves
        # `__pycache__` directories inside the fixture on the way past.
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(  # noqa: S602 - the fixture's own command, by design
            command,
            shell=True,
            cwd=workspace.path(SAMPLE),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )

    def test_the_root_builds_suite_passes(self):
        result = self._run(".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_plugin_builds_suite_passes(self):
        result = self._run("plugin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestTheSampleBacklogParses(unittest.TestCase):
    def setUp(self):
        self.tickets, self.how, self.derived = ingest(
            (SAMPLE / "SPEC.md").read_text(encoding="utf-8")
        )

    def test_the_spec_is_used_verbatim_rather_than_replanned(self):
        # Planned means a model rephrased the criteria, and then the fixture is
        # measuring the planner rather than the change under test.
        self.assertEqual(self.how, "parsed")

    def test_it_yields_the_three_tickets_in_order(self):
        self.assertEqual(
            [ticket.ticket_id for ticket in self.tickets],
            ["SP-001", "SP-002", "SP-003"],
        )

    def test_one_ticket_waits_on_another(self):
        # Scheduling, dependency stamps and the reopen-stale-dependents path
        # are all dead code against a backlog of independent tickets.
        needs = {ticket.ticket_id: ticket.needs for ticket in self.tickets}
        self.assertEqual(needs["SP-002"], ["SP-001"])

    def test_one_ticket_lands_in_the_second_build(self):
        config = Config.load(SAMPLE)
        owners = {
            ticket.ticket_id: sorted(
                {
                    workspace.root
                    for path in ticket.allowed_files
                    if (workspace := config.workspace_for(path)) is not None
                }
            )
            for ticket in self.tickets
        }
        self.assertEqual(owners["SP-003"], ["plugin"])

    def test_no_ticket_straddles_two_builds(self):
        # Refused at ingest, and rightly: the two halves would be verified by
        # different commands with no answer about which one judges the ticket.
        config = Config.load(SAMPLE)
        for ticket in self.tickets:
            builds = {
                workspace.root
                for path in ticket.allowed_files
                if (workspace := config.workspace_for(path)) is not None
            }
            self.assertEqual(len(builds), 1, ticket.ticket_id)

    def test_every_path_the_spec_names_belongs_to_a_build(self):
        # An unowned file is refused at ingest, which would make the fixture
        # unusable in exactly the way it is meant to catch elsewhere.
        config = Config.load(SAMPLE)
        for ticket in self.tickets:
            for path in ticket.allowed_files + ticket.reference_files:
                self.assertIsNotNone(config.workspace_for(path), path)

    def test_every_ticket_names_its_own_test_file(self):
        # With none designated the loop invents a path outside the ticket's
        # scope, and then the executor is refused every time it tries to repair
        # what the tester wrote there. One real ticket died precisely there.
        for ticket in self.tickets:
            self.assertTrue(
                any("test" in path for path in ticket.allowed_files),
                f"{ticket.ticket_id} has no test file in its allowed files",
            )

    def test_every_ticket_carries_criteria_a_stub_could_not_satisfy(self):
        for ticket in self.tickets:
            self.assertGreaterEqual(len(ticket.criteria), 4, ticket.ticket_id)


class TestTheStallBacklogCannotSucceed(unittest.TestCase):
    """`SPEC.md` is work the loop can do; `STALL.md` is work it cannot. Every
    brake — the attempt budget, convergence, the respec, the park — only ever
    runs on a ticket that is going nowhere, and a fixture whose runs all finish
    green never asks one of them a question."""

    def setUp(self):
        self.tickets, self.how, _derived = ingest(
            (SAMPLE / "STALL.md").read_text(encoding="utf-8")
        )

    def test_it_is_one_parsed_ticket(self):
        self.assertEqual(self.how, "parsed")
        self.assertEqual([t.ticket_id for t in self.tickets], ["ST-001"])

    def test_the_impossible_criterion_is_about_a_file_the_ticket_may_not_write(self):
        # The defect is a spec defect and it is invisible to a reader who
        # checks the criteria and the scope separately: the last criterion
        # demands behaviour from `wordcount/counter.py`, which is a reference
        # file here — readable, not writable.
        ticket = self.tickets[0]

        self.assertIn("wordcount/counter.py", ticket.reference_files)
        self.assertNotIn("wordcount/counter.py", ticket.allowed_files)
        self.assertIn("count_words", ticket.criteria[-1])

    def test_the_criterion_contradicts_the_code_as_it_stands(self):
        # If somebody fixes the seeded defect in the fixture, this backlog
        # quietly becomes satisfiable and stops testing anything.
        sys.path.insert(0, str(SAMPLE))
        written = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            from wordcount.counter import count_words
        finally:
            sys.dont_write_bytecode = written
            sys.path.pop(0)

        self.assertNotEqual(
            count_words("Hello, world!"), {"hello": 1, "world": 1}
        )

    def test_the_stall_ticket_is_not_part_of_the_ordinary_backlog(self):
        # Ingested instead of `SPEC.md`, never alongside it.
        spec, _how, _derived = ingest((SAMPLE / "SPEC.md").read_text(encoding="utf-8"))

        self.assertNotIn("ST-001", [ticket.ticket_id for ticket in spec])


class TestTheHardBacklogIsHardAndNotImpossible(unittest.TestCase):
    """`SPEC.md` lands on the first attempt and `STALL.md` cannot be landed at
    all, so neither exercises the middle of the loop: several attempts, a
    failure set that shrinks, convergence measured on it, the ladder climbing.
    `HARD.md` is the one that should end done and take its time."""

    def setUp(self):
        self.tickets, self.how, _derived = ingest(
            (SAMPLE / "HARD.md").read_text(encoding="utf-8")
        )

    def test_it_is_one_parsed_ticket(self):
        self.assertEqual(self.how, "parsed")
        self.assertEqual([t.ticket_id for t in self.tickets], ["HP-001"])

    def test_every_criterion_is_satisfiable_by_a_correct_implementation(self):
        # The difference between this backlog and `STALL.md`, and the property
        # that decides which one the loop is being tested against. Checked by
        # implementing the spec here and reading the criteria back.
        from decimal import ROUND_HALF_UP, Decimal

        def shares(counts):
            if not counts:
                return []
            total = sum(counts.values())
            width = max(len(word) for word in counts)
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return [
                f"{word.ljust(width)} "
                f"{(Decimal(100 * count) / Decimal(total)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
                for word, count in ranked
            ]

        self.assertEqual(shares({"a": 1, "b": 1}), ["a 50.0%", "b 50.0%"])
        self.assertEqual(
            shares({"apple": 2, "b": 1}), ["apple 66.7%", "b     33.3%"]
        )
        self.assertEqual(
            shares({"a": 1, "b": 2, "c": 3}), ["c 50.0%", "b 33.3%", "a 16.7%"]
        )
        self.assertEqual(
            shares({"b": 1, "a": 1, "c": 1}),
            ["a 33.3%", "b 33.3%", "c 33.3%"],
        )
        self.assertEqual(shares({"a": 13, "b": 67}), ["b 83.8%", "a 16.3%"])
        self.assertEqual(shares({}), [])

    def test_the_rounding_criterion_is_a_real_trap(self):
        # The criterion exists because the obvious implementation gets it
        # wrong. If Python ever rounds this half away from zero, the ticket
        # stops being hard and this fixture stops testing anything.
        self.assertEqual(round(16.25, 1), 16.2)

    def test_it_writes_files_the_other_specs_do_not(self):
        spec, _how, _derived = ingest((SAMPLE / "SPEC.md").read_text(encoding="utf-8"))
        theirs = {path for ticket in spec for path in ticket.allowed_files}
        mine = {path for ticket in self.tickets for path in ticket.allowed_files}

        self.assertEqual(theirs & mine, set())
        for path in mine:
            self.assertFalse((SAMPLE / path).exists(), path)


class TestTheSeededDefectIsStillThere(unittest.TestCase):
    """`BUG.md` reports a real fault, and `forge bug` is only exercised while
    it stays one. Fixing it here would leave a bug report about behaviour the
    fixture no longer has — a reproduction that cannot fail, which is the one
    outcome the bug loop treats as its own failure."""

    def test_punctuation_is_still_counted_as_part_of_the_word(self):
        # Byte-code off for the same reason the subprocess runs have it off:
        # importing the fixture's code must not leave a `__pycache__` in the
        # tree that `TestTheFixtureIsOnlyTheFixture` is about to read.
        sys.path.insert(0, str(SAMPLE))
        written = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            from wordcount.counter import count_words
        finally:
            sys.dont_write_bytecode = written
            sys.path.pop(0)

        self.assertEqual(
            count_words("Hello, world! Hello again."),
            {"hello,": 1, "world!": 1, "hello": 1, "again.": 1},
        )


class TestTheSampleIsCopiedBeforeItIsRun(unittest.TestCase):
    """A run writes code, a database and an artifact tree. Run one in place and
    the next test reads the last run's output as the fixture's own state."""

    def test_a_copy_is_a_complete_runnable_project(self):
        import tempfile

        target = Path(tempfile.mkdtemp()) / "copy"
        copied = copy_sample(target)

        Config.load(copied).validate()
        self.assertTrue((copied / "SPEC.md").exists())
        self.assertTrue((copied / "plugin" / "histogram" / "bars.py").exists())

    def test_run_state_is_left_behind(self):
        # Staged in a copy rather than in the fixture: a test that writes run
        # state into the tree it exists to keep clean leaves it there the first
        # time it fails partway through.
        import tempfile

        used = copy_sample(Path(tempfile.mkdtemp()) / "used")
        (used / ".hybridforge" / "run.db").write_bytes(b"")
        (used / ".hybridforge" / "tickets").mkdir()
        (used / ".hybridforge" / "tickets" / "SP-001.md").write_text("x", encoding="utf-8")

        again = _copy_tree(used, Path(tempfile.mkdtemp()) / "again")

        self.assertFalse((again / ".hybridforge" / "run.db").exists())
        self.assertFalse((again / ".hybridforge" / "tickets").exists())
        self.assertTrue((again / "SPEC.md").exists())

    def test_copying_over_an_existing_directory_is_refused(self):
        # A merge into a tree a previous run dirtied is the state this script
        # exists to avoid, so it is an error rather than a silent overwrite.
        import tempfile

        existing = Path(tempfile.mkdtemp())

        with self.assertRaises(FileExistsError):
            copy_sample(existing)


if __name__ == "__main__":
    unittest.main()
