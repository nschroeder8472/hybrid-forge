"""The blind-grading experiment builds two arms that differ in one thing.

`docs/BLIND-GRADING.md` is the write-up. What matters here is that the scaffold
keeps the properties the comparison rests on: one variable between the arms, a
green baseline in both, a lint rule strict enough to actually bite, and the
ticket lifted from `GRIND.md` rather than copied so it cannot drift from the
work the earlier runs landed.

None of this spends a token or needs a model.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import blind_grading  # noqa: E402

SAMPLE = ROOT / "examples" / "sample-project"


class TestTheTicketIsTheOneTheEarlierRunsUsed(unittest.TestCase):
    def test_it_is_lifted_from_the_committed_spec(self):
        # Copied into the script instead, it would drift from `GRIND.md` and
        # the comparison against three earlier runs would quietly stop being a
        # comparison.
        spec = (SAMPLE / "GRIND.md").read_text(encoding="utf-8")
        ticket = blind_grading._ticket(spec, "GR-001")

        self.assertTrue(ticket.startswith("## GR-001:"))
        self.assertIn("window must be positive", ticket)
        self.assertNotIn("GR-002", ticket)

    def test_a_missing_ticket_is_an_error_rather_than_an_empty_arm(self):
        with self.assertRaises(SystemExit):
            blind_grading._ticket("# nothing here\n", "GR-001")


class TestTheRuleIsStrictEnoughToBite(unittest.TestCase):
    """A limit the executor satisfies by accident measures nothing. Run 3 of
    `GRIND.md` established that this one writes clean Python at 79 columns with
    no config in front of it."""

    def test_the_limit_is_below_what_a_default_satisfies(self):
        self.assertLess(blind_grading.COLUMNS, 79)

    def test_the_committed_fixture_files_are_grandfathered(self):
        # Every root-build file that is longer than the limit has to be listed,
        # or the baseline ships red and the run reports the fixture's own debt
        # as the ticket's.
        too_long = set()
        for path in sorted(SAMPLE.rglob("*.py")):
            relative = path.relative_to(SAMPLE).as_posix()
            if relative.startswith("plugin/"):
                continue
            widest = max(
                (len(line) for line in path.read_text(encoding="utf-8").splitlines()),
                default=0,
            )
            if widest > blind_grading.COLUMNS:
                too_long.add(relative)

        self.assertEqual(too_long, set(blind_grading.GRANDFATHERED))

    def test_the_budget_can_reach_the_first_rung_of_the_ladder(self):
        # `_measure_cycle` runs over tickets eligible for a retry *cycle*, and
        # `reviewWhenStuck` fires on the second flat one. The fixture's own
        # `retryCycles: 1` cannot reach it however many attempts are burned
        # inside the first cycle — which is why run 3's second attempt
        # measured nothing.
        from forge.config import Config

        rung = Config.load(SAMPLE).loop.review_when_stuck
        self.assertGreater(blind_grading.RETRY_CYCLES, rung)


class TestTheArmsDifferInOneThing(unittest.TestCase):
    # Built once. Nothing below writes to the arms, and building them per test
    # method copied the fixture eight times and ran flake8 sixteen times — it
    # took the whole suite from 105 seconds to 349.
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="forge-blind-")) / "experiment"
        spec = (SAMPLE / "GRIND.md").read_text(encoding="utf-8")
        ticket = blind_grading._ticket(spec, "GR-001")
        cls.arms = {}
        for arm, toolchain_context in blind_grading.ARMS.items():
            root = cls.base / arm
            blind_grading.copy_sample(root)
            blind_grading._write_arm(root, toolchain_context, ticket)
            cls.arms[arm] = root

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.base.parent, ignore_errors=True)

    def test_only_the_toolchain_setting_differs(self):
        left, right = (
            json.loads((root / ".hybridforge" / "config.json").read_text("utf-8"))
            for root in (self.arms["arm-blind"], self.arms["arm-shown"])
        )
        differing = {
            key
            for key in set(left["loop"]) | set(right["loop"])
            if left["loop"].get(key) != right["loop"].get(key)
        }

        self.assertEqual(differing, {"toolchainContext"})
        self.assertFalse(left["loop"]["toolchainContext"])
        self.assertTrue(right["loop"]["toolchainContext"])

    def test_every_other_file_is_identical(self):
        import filecmp

        # `.git` because each arm gets its own `git init` — the copies need one
        # or quarantine cannot revert a failed ticket's files — and two
        # independent repositories over identical content still differ in
        # commit time, and so in every object and ref that hashes it. What the
        # arms have to share is the tree git is tracking, which is the rest of
        # this comparison.
        comparison = filecmp.dircmp(
            self.arms["arm-blind"],
            self.arms["arm-shown"],
            ignore=["config.json", "__pycache__", ".git"],
        )

        def differences(node, prefix=""):
            found = [prefix + name for name in node.diff_files]
            for name, child in node.subdirs.items():
                found += differences(child, f"{prefix}{name}/")
            return found

        self.assertEqual(differences(comparison), [])

    def test_both_arms_lint_clean_before_a_run_starts(self):
        # A red baseline is a confound in both directions: it charges the
        # ticket for the fixture's debt, and it is excused for the wrong
        # reasons when quarantine takes the ticket's own work back out.
        for arm, root in self.arms.items():
            for build in (".", "plugin"):
                result = subprocess.run(
                    [sys.executable, "-m", "flake8", "."],
                    cwd=root / build,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0, f"{arm}/{build}:\n{result.stdout}"
                )


if __name__ == "__main__":
    unittest.main()
