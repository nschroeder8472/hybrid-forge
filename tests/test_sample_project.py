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
        ".flake8",
        ".hybridforge/.gitignore",
        ".hybridforge/config.json",
        "BUG.md",
        "README.md",
        "GRIND.md",
        "HARD.md",
        "OPAQUE.md",
        "SPEC.md",
        "STALL.md",
        "plugin/.flake8",
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
        # implementing the spec here and reading every criterion back — all
        # nine, so a revision of the spec that outruns this reference is a
        # failure here rather than a silently weaker guard.
        from decimal import ROUND_HALF_UP, Decimal

        def shares(counts, limit=0):
            if not counts:
                return []
            total = sum(counts.values())
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            shown = list(ranked)
            if 0 < limit < len(ranked):
                shown = ranked[:limit]
                shown.append(("other", sum(count for _label, count in ranked[limit:])))
            width = max(len(label) for label, _count in shown)
            rows = []
            for label, count in shown:
                share = (Decimal(100 * count) / Decimal(total)).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP
                )
                rows.append(f"{label.ljust(width)} " + f"{share}%".rjust(6))
            words = f"{len(counts)} word{'' if len(counts) == 1 else 's'}"
            occurrences = f"{total} occurrence{'' if total == 1 else 's'}"
            return rows + [f"{words}, {occurrences}"]

        self.assertEqual(
            shares({"a": 1, "b": 1}),
            ["a  50.0%", "b  50.0%", "2 words, 2 occurrences"],
        )
        self.assertEqual(
            shares({"apple": 2, "b": 1}),
            ["apple  66.7%", "b      33.3%", "2 words, 3 occurrences"],
        )
        self.assertEqual(
            shares({"a": 1, "b": 2, "c": 3}),
            ["c  50.0%", "b  33.3%", "a  16.7%", "3 words, 6 occurrences"],
        )
        self.assertEqual(
            shares({"b": 1, "a": 1, "c": 1}),
            ["a  33.3%", "b  33.3%", "c  33.3%", "3 words, 3 occurrences"],
        )
        self.assertEqual(
            shares({"a": 13, "b": 67}),
            ["b  83.8%", "a  16.3%", "2 words, 80 occurrences"],
        )
        self.assertEqual(
            shares({"a": 1}), ["a 100.0%", "1 word, 1 occurrence"]
        )
        self.assertEqual(
            shares({"a": 5, "b": 3, "c": 2, "d": 1}, 2),
            [
                "a      45.5%",
                "b      27.3%",
                "other  27.3%",
                "4 words, 11 occurrences",
            ],
        )
        self.assertEqual(
            shares({"a": 2, "b": 1}, 5),
            ["a  66.7%", "b  33.3%", "2 words, 3 occurrences"],
        )
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


class TestTheGrindBacklogReachesTheMiddleOfTheLoop(unittest.TestCase):
    """`GRIND.md` was written for the middle of `docs/CONVERGENCE.md` — a
    failure set with somewhere to descend from — and its one run did not reach
    it: both tickets done, one attempt each, nothing failed.

    `GR-001` landed first try, so it is a control case beside `HARD.md`.
    `GR-002` was written to be jointly impossible and is not: its criteria hold
    together under an asymmetric correction, which is proved here rather than
    argued, because arguing it is what got it wrong the first time. What was
    defective was the ticket's stated *rule*, and the sign-off pass refused the
    ticket over exactly that."""

    def setUp(self):
        self.tickets, self.how, _derived = ingest(
            (SAMPLE / "GRIND.md").read_text(encoding="utf-8")
        )
        self.grind = {ticket.ticket_id: ticket for ticket in self.tickets}

    def test_it_is_two_parsed_tickets(self):
        self.assertEqual(self.how, "parsed")
        self.assertEqual([t.ticket_id for t in self.tickets], ["GR-001", "GR-002"])

    def test_neither_ticket_waits_on_the_other(self):
        # A dependency would park GR-002 behind GR-001 rather than running it,
        # and the whole point is that the two halves fail independently.
        for ticket in self.tickets:
            self.assertEqual(ticket.needs, [], ticket.ticket_id)

    def test_the_satisfiable_ticket_is_satisfiable(self):
        # The property that separates GR-001 from `STALL.md`. Checked by
        # implementing its spec here and reading all nine criteria back.
        class Stream:
            def __init__(self, window):
                if window < 1:
                    raise ValueError("window must be positive")
                self.window = window
                self.texts = []
                self.counts = {}

            def add(self, text):
                if len(self.texts) >= self.window:
                    for word in self.texts.pop(0):
                        self.counts[word] -= 1
                        if self.counts[word] == 0:
                            del self.counts[word]
                words = [word.lower() for word in text.split()]
                self.texts.append(words)
                for word in words:
                    self.counts[word] = self.counts.get(word, 0) + 1

            def top(self, n):
                ranked = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
                return ranked[:n]

            def distinct(self):
                return len(self.counts)

        with self.assertRaises(ValueError) as raised:
            Stream(0)
        self.assertEqual(str(raised.exception), "window must be positive")

        stream = Stream(2)
        stream.add("a b")
        self.assertEqual(stream.top(5), [("a", 1), ("b", 1)])
        self.assertEqual(stream.distinct(), 2)

        stream = Stream(2)
        for text in ("a", "b", "c"):
            stream.add(text)
        self.assertEqual(stream.top(5), [("b", 1), ("c", 1)])
        self.assertEqual(stream.distinct(), 2)

        stream = Stream(2)
        stream.add("a")
        stream.add("a")
        self.assertEqual(stream.top(5), [("a", 2)])
        stream.add("b")
        self.assertEqual(stream.top(5), [("a", 1), ("b", 1)])

        stream = Stream(2)
        stream.add("a")
        handed_out = stream.top(5)
        self.assertEqual(handed_out, [("a", 1)])
        stream.add("a")
        self.assertEqual(handed_out, [("a", 1)])
        self.assertEqual(stream.top(5), [("a", 2)])

        stream = Stream(1)
        stream.add("b a a c")
        self.assertEqual(stream.top(5), [("a", 2), ("b", 1), ("c", 1)])
        self.assertEqual(stream.top(2), [("a", 2), ("b", 1)])

        stream = Stream(1)
        stream.add("A a")
        self.assertEqual(stream.top(5), [("a", 2)])

        stream = Stream(3)
        self.assertEqual(stream.top(5), [])
        self.assertEqual(stream.distinct(), 0)

        stream = Stream(1)
        stream.add("a")
        self.assertEqual(stream.top(0), [])

    def test_the_second_tickets_criteria_hold_together_asymmetrically(self):
        # GR-002 was written to be jointly impossible and is not, which the run
        # of 2026-09-01 established and this pins. All seven criteria hold at
        # once under a correction that runs in one direction only — the rule
        # the sign-off pass wrote into the spec, reproduced here.
        from decimal import ROUND_HALF_UP, Decimal

        def rounded(counts):
            total = sum(counts.values())
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return ranked, [
                int(
                    (Decimal(100 * count) / Decimal(total)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                for _word, count in ranked
            ]

        def table(counts):
            if not counts:
                return []
            ranked, percents = rounded(counts)
            shortfall = 100 - sum(percents)
            if shortfall > 0:
                first = min(counts)
                percents[[word for word, _count in ranked].index(first)] += shortfall
            width = max(len(word) for word in counts)
            return [
                f"{word.ljust(width)} {percent}%"
                for (word, _count), percent in zip(ranked, percents)
            ]

        def shown_total(rows):
            return sum(int(row.split()[-1].rstrip("%")) for row in rows)

        self.assertEqual(table({"a": 1, "b": 1}), ["a 50%", "b 50%"])
        self.assertEqual(table({"apple": 3, "b": 1}), ["apple 75%", "b     25%"])
        self.assertEqual(table({"b": 1, "a": 1}), ["a 50%", "b 50%"])
        self.assertEqual(table({"a": 1, "b": 1, "c": 4}), ["c 67%", "a 17%", "b 17%"])
        self.assertEqual(table({"a": 1, "b": 7}), ["b 88%", "a 13%"])
        self.assertEqual(shown_total(table({"a": 1, "b": 1, "c": 1})), 100)
        self.assertEqual(table({}), [])
        self.assertIn("100", self.grind["GR-002"].criteria[5])

    def test_the_symmetric_reading_of_the_second_ticket_really_does_fail(self):
        # And why the ticket reads as impossible: the rule its own spec states
        # yields 99 where a criterion asks for 100, and the two criteria that
        # pin rows summing to 101 block every correction that runs in both
        # directions, largest-remainder included. That is a defect in the
        # ticket's rule rather than in what it promises, which is why revising
        # the spec repaired it and the criteria ratchet never had to move.
        from decimal import ROUND_HALF_UP, Decimal

        def shares(counts):
            total = sum(counts.values())
            return [
                int(
                    (Decimal(100 * count) / Decimal(total)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                for count in counts.values()
            ]

        self.assertEqual(sum(shares({"a": 1, "b": 1, "c": 1})), 99)
        self.assertEqual(sum(shares({"a": 1, "b": 1, "c": 4})), 101)
        self.assertEqual(sum(shares({"a": 1, "b": 7})), 101)

    def test_the_rounding_criterion_is_a_real_trap(self):
        # Same reason `HARD.md` pins its own: the criterion exists because the
        # obvious implementation gets it wrong.
        self.assertEqual(round(12.5), 12)

    def test_it_writes_files_the_other_specs_do_not(self):
        mine = {path for ticket in self.tickets for path in ticket.allowed_files}
        for name in ("SPEC.md", "HARD.md", "STALL.md"):
            other, _how, _derived = ingest((SAMPLE / name).read_text(encoding="utf-8"))
            theirs = {path for ticket in other for path in ticket.allowed_files}
            self.assertEqual(theirs & mine, set(), name)
        for path in mine:
            self.assertFalse((SAMPLE / path).exists(), path)

    def test_every_ticket_names_its_own_test_file(self):
        # Without one the tester writes outside the ticket's scope and the
        # executor is refused every time it tries to repair what it wrote —
        # which would park both tickets for a reason this backlog is not about.
        for ticket in self.tickets:
            self.assertTrue(
                any("test" in path for path in ticket.allowed_files),
                f"{ticket.ticket_id} has no test file in its allowed files",
            )


class TestTheOpaqueBacklogWithholdsTheRule(unittest.TestCase):
    """Eleven runs of this fixture have landed and none has asked the
    escalation ladder a question. The reading in `docs/ROADMAP.md` is that
    every mechanism below the ladder absorbs the failure it exists to escalate,
    and that what none of them can absorb is a failure whose *text does not
    describe its cause* — `TS2532 object is possibly undefined` against
    `E501 line too long (52 > 50 characters)`.

    `OPAQUE.md` is written to produce failures of the first kind: the rule that
    decides a bar's length lives in a file the ticket may not read or call, and
    what a failing attempt is shown is one character of difference in an
    assertion."""

    def setUp(self):
        self.tickets, self.how, _derived = ingest(
            (SAMPLE / "OPAQUE.md").read_text(encoding="utf-8")
        )
        self.ticket = self.tickets[0]

    def _bars(self):
        sys.path.insert(0, str(SAMPLE / "plugin"))
        written = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            from histogram.bars import bars
        finally:
            sys.dont_write_bytecode = written
            sys.path.pop(0)
        return bars

    def test_it_is_one_parsed_ticket(self):
        self.assertEqual(self.how, "parsed")
        self.assertEqual([t.ticket_id for t in self.tickets], ["OP-001"])

    def test_it_runs_in_the_second_build(self):
        config = Config.load(SAMPLE)
        builds = {
            config.workspace_for(path).root for path in self.ticket.allowed_files
        }

        self.assertEqual(builds, {"plugin"})

    def test_the_file_that_decides_the_answer_is_not_in_the_prompt(self):
        # The whole mechanism. `bars.py` carries `count * width // tallest`,
        # and a ticket that could read it would be a ticket whose failures
        # name their own cause.
        scope = self.ticket.allowed_files + self.ticket.reference_files

        self.assertNotIn("plugin/histogram/bars.py", scope)

    def test_but_the_ordering_rule_is(self):
        # The ticket is not a guessing game: everything except the scaling
        # arithmetic is in the prompt, and `bars_test.py` is where the ordering
        # and full-width rules come from.
        self.assertIn("plugin/tests/bars_test.py", self.ticket.reference_files)

    def test_the_criteria_name_what_decides_the_answer(self):
        # Satisfiable, and mechanically so: the tester can import `bars` and
        # compare. A criterion nobody can check is what parks `STALL.md`, and
        # this backlog is meant to end done.
        comparison = [c for c in self.ticket.criteria if "bars(" in c]

        self.assertEqual(len(comparison), 1)
        self.assertIn("may not", comparison[0])

    def test_the_mappings_it_names_really_do_separate_the_rules(self):
        # The guard against quiet rot, and the same one `STALL.md` has: if
        # somebody changes `bars` to round, this backlog stops testing anything
        # and every run of it lands on the first attempt for the wrong reason.
        bars = self._bars()
        for counts in ({"a": 3, "b": 2}, {"a": 5, "b": 3, "c": 1}, {"a": 3, "b": 1, "c": 2}):
            tallest = max(counts.values())
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            rounded = [
                f"{word} {'#' * round(count / tallest * 4)}" for word, count in ranked
            ]

            self.assertNotEqual(bars(counts, width=4), rounded, counts)

    def test_one_of_them_catches_a_bar_clamped_to_a_single_character(self):
        # The repair a wrong implementation reaches for first: a word with a
        # count "should" show something. The rule says it shows nothing.
        bars = self._bars()

        self.assertEqual(bars({"a": 5, "b": 3, "c": 1}, width=4)[-1].strip(), "c")

    def test_the_criteria_it_states_outright_are_what_the_rule_produces(self):
        # Every stated criterion has to be true of the code being compared
        # against, or the ticket is unsatisfiable and this is `STALL.md` again.
        bars = self._bars()

        self.assertEqual(bars({"a": 2}, width=4), ["a ####"])
        self.assertEqual(bars({"b": 1, "a": 1}, width=1), ["a #", "b #"])
        self.assertEqual(bars({"a": 4, "b": 2}, width=4), ["a ####", "b ##"])

    def test_it_writes_files_the_other_specs_do_not(self):
        mine = set(self.ticket.allowed_files)
        for name in ("SPEC.md", "HARD.md", "STALL.md", "GRIND.md"):
            other, _how, _derived = ingest((SAMPLE / name).read_text(encoding="utf-8"))
            theirs = {path for ticket in other for path in ticket.allowed_files}
            self.assertEqual(theirs & mine, set(), name)
        for path in mine:
            self.assertFalse((SAMPLE / path).exists(), path)

    def test_it_names_its_own_test_file(self):
        self.assertTrue(any("test" in path for path in self.ticket.allowed_files))


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
