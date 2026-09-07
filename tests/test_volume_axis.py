"""The volume axis: how many kinds of failure a ticket has, and which are new.

§4.2 of docs/ADAPTIVE-TICKET-LOOP.md. `_convergence` answers whether a
ticket's failure set is *moving* and never how big it is, so a ticket failing
on 38 distinct classes and one failing on 7 are indistinguishable to every
brake in the loop. These tests pin the two counters that make the difference
visible, that review objections are counted at all, and that recording them
changes nothing about the class set the brakes read.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.config import Config, LoopSettings
from forge.failures import review_points
from forge.loop import Orchestrator
from forge.state import Store, Ticket
from forge.ui import server as ui_server


class TestSplittingARejectionIntoPoints(unittest.TestCase):
    """`failures.review_points`, which counts what one rejection objects to."""

    def test_a_bulleted_rejection_carries_one_point_per_bullet(self):
        reason = (
            "REJECT\n"
            "- No `def evidence(` exists in `forge/ui/server.py`.\n"
            "- The `snapshot` function omits the evidence key for `forge/a.py`.\n"
        )

        self.assertEqual(len(review_points(reason)), 2)

    def test_a_numbered_rejection_is_split_the_same_way(self):
        reason = (
            "REJECT\n"
            "1. `forge/state.py` never defines `seen_classes`.\n"
            "2. The test asserts on a `points` column and `forge/a.py` has none.\n"
        )

        self.assertEqual(len(review_points(reason)), 2)

    def test_a_point_carrying_no_citation_is_not_a_finding(self):
        # The reviewer's own contract: "an objection carrying neither is not a
        # finding". Counting uncited prose inflates every rejection to the
        # number of sentences in it.
        reason = (
            "REJECT\n"
            "- This does not feel like what the spec had in mind.\n"
            "- `forge/ui/server.py` never defines `evidence`.\n"
        )

        self.assertEqual(len(review_points(reason)), 1)

    def test_a_citation_on_a_continuation_line_still_counts(self):
        reason = (
            "REJECT\n"
            "- The criterion is unmet:\n"
            "  I searched for `allowRemoteWrites` and found nothing.\n"
        )

        self.assertEqual(len(review_points(reason)), 1)

    def test_prose_is_split_on_blank_lines_when_nothing_is_bulleted(self):
        reason = (
            "REJECT\n"
            "\n"
            "The tests never call the function: `test_evidence` asserts on the\n"
            "module source.\n"
            "\n"
            "I searched for `allowRemoteWrites` in `forge/config.py` and found\n"
            "nothing.\n"
        )

        self.assertEqual(len(review_points(reason)), 2)

    def test_the_verdict_line_is_not_an_objection(self):
        # It is a decision, and it reaches this function at the top of every
        # body `parse_verdict` hands back. It cites nothing, so it counts as
        # nothing without this function needing to know what a verdict is.
        self.assertEqual(review_points("REJECT\n"), [])

    def test_two_points_about_the_same_rule_in_the_same_file_are_one(self):
        reason = (
            "REJECT\n"
            "- Line 14 of `forge/state.py` is over the line-length limit.\n"
            "- Line 98 of `forge/state.py` is over the line-length limit.\n"
        )

        self.assertEqual(len(review_points(reason)), 1)

    def test_points_carry_the_file_they_cite(self):
        reason = "REJECT\n- `forge/ui/server.py` never defines `evidence`.\n"

        self.assertIn("forge/ui/server.py", review_points(reason)[0])

    def test_an_empty_body_is_no_points_rather_than_an_error(self):
        self.assertEqual(review_points(""), [])


class TestRecordingPointsOnTheStep(unittest.TestCase):
    """What `end_step` writes, and for which steps."""

    def setUp(self) -> None:
        self.store = Store(Path(tempfile.mkdtemp()).resolve() / "t.db")
        self.run_id = self.store.create_run("goal")
        self.store.add_tickets(self.run_id, [Ticket("T-1")])

    def tearDown(self) -> None:
        self.store.close()

    def _fail(self, name: str, detail: str) -> None:
        step_id = self.store.start_step(self.run_id, "T-1", name)
        self.store.end_step(step_id, "failed", detail)

    def test_a_failed_review_records_a_point_per_objection(self):
        self._fail(
            "review",
            "REJECT\n"
            "- `forge/a.py` never defines `evidence`.\n"
            "- `forge/b.py` never defines `snapshot`.\n",
        )

        self.assertEqual(len(self.store.ticket_points(self.run_id, "T-1")), 2)

    def test_a_rejection_still_classes_as_one_thing(self):
        # The counters are additional. Nothing about the class set changes, so
        # no brake reading it sees anything new.
        self._fail(
            "review",
            "REJECT\n"
            "- `forge/a.py` never defines `evidence`.\n"
            "- `forge/b.py` never defines `snapshot`.\n",
        )

        classes = self.store.ticket_classes(self.run_id, "T-1")
        self.assertEqual([entry["name"] for entry in classes], ["review reject"])

    def test_a_failed_lint_step_records_no_points(self):
        self._fail("lint", "a.py:1:101: E501 line too long (104 > 100 characters)")

        self.assertEqual(self.store.ticket_points(self.run_id, "T-1"), [])

    def test_a_passing_review_records_no_points(self):
        step_id = self.store.start_step(self.run_id, "T-1", "review")
        self.store.end_step(step_id, "ok", "ACCEPT\n\nEvery criterion is met.")

        self.assertEqual(self.store.ticket_points(self.run_id, "T-1"), [])

    def test_the_same_objection_twice_is_one_point_counted_twice(self):
        for _ in range(2):
            self._fail("review", "REJECT\n- `forge/a.py` never defines `evidence`.\n")

        points = self.store.ticket_points(self.run_id, "T-1")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["count"], 2)

    def test_points_after_a_mark_are_the_cycles_own(self):
        self._fail("review", "REJECT\n- `forge/a.py` never defines `evidence`.\n")
        mark = self.store.last_step_id(self.run_id, "T-1")
        self._fail("review", "REJECT\n- `forge/b.py` never defines `snapshot`.\n")

        points = self.store.ticket_points(self.run_id, "T-1", after=mark)
        self.assertEqual(len(points), 1)
        self.assertIn("forge/b.py", points[0]["name"])


class TestWhatWasSeenBeforeTheMark(unittest.TestCase):
    """`Store.seen_classes`, the half a forward read cannot answer."""

    def setUp(self) -> None:
        self.store = Store(Path(tempfile.mkdtemp()).resolve() / "t.db")
        self.run_id = self.store.create_run("goal")
        self.store.add_tickets(self.run_id, [Ticket("T-1")])

    def tearDown(self) -> None:
        self.store.close()

    def _fail(self, name: str, detail: str) -> None:
        step_id = self.store.start_step(self.run_id, "T-1", name)
        self.store.end_step(step_id, "failed", detail)

    def test_nothing_is_seen_before_the_first_cycle_ends(self):
        self._fail("lint", "a.py:1:1: E501 line too long")

        self.assertEqual(self.store.seen_classes(self.run_id, "T-1", until=0), set())

    def test_a_class_present_on_both_sides_of_the_mark_is_not_new(self):
        self._fail("lint", "a.py:1:1: E501 line too long")
        mark = self.store.last_step_id(self.run_id, "T-1")
        self._fail("lint", "a.py:9:1: E501 line too long")

        seen = self.store.seen_classes(self.run_id, "T-1", until=mark)
        current = {
            entry["name"]
            for entry in self.store.ticket_classes(self.run_id, "T-1", after=mark)
        }
        self.assertEqual(current - seen, set())

    def test_review_points_are_seen_alongside_tool_classes(self):
        self._fail("review", "REJECT\n- `forge/a.py` never defines `evidence`.\n")
        mark = self.store.last_step_id(self.run_id, "T-1")

        seen = self.store.seen_classes(self.run_id, "T-1", until=mark)
        self.assertTrue(any("forge/a.py" in name for name in seen))


class TestTheCountersOnTheDashboard(unittest.TestCase):
    """§4.2's "done when": the page can answer both of its questions."""

    def test_a_parked_ticket_carries_both_counters(self):
        ticket = Ticket(
            "T-1",
            status="blocked",
            distinct_classes=14,
            new_classes=["lint E501 in a.py"],
        )

        block = ui_server.evidence(ticket)

        self.assertEqual(block["distinct_classes"], 14)
        self.assertEqual(block["new_classes"], ["lint E501 in a.py"])

    def test_the_new_class_list_is_capped(self):
        ticket = Ticket(
            "T-1",
            status="blocked",
            new_classes=[f"lint E50{i} in a.py" for i in range(9)],
        )

        self.assertEqual(len(ui_server.evidence(ticket)["new_classes"]), 5)


class TestTheCountersOnACycle(unittest.TestCase):
    """`_measure_cycle` recording the axis, and acting on none of it."""

    def _orchestrator(self) -> tuple[Orchestrator, Store, int]:
        root = Path(tempfile.mkdtemp()).resolve()
        config = Config(
            root=root,
            models={
                "m": {
                    "kind": "openai",
                    "baseUrl": "http://127.0.0.1:1/v1",
                    "model": "stub",
                    "contextWindow": 8192,
                    "maxOutputTokens": 1024,
                }
            },
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": ""},
            loop=LoopSettings(**{"respec_on_retry": False, "preflight": False}),
        )
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1")])
        self.addCleanup(store.close)
        return Orchestrator(config, store), store, run_id

    def _fail(self, store: Store, run_id: int, name: str, detail: str) -> None:
        step_id = store.start_step(run_id, "T-1", name)
        store.end_step(step_id, "failed", detail)

    def test_a_first_cycle_counts_everything_it_produced_as_new(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = store.list_tickets(run_id)[0]
        self._fail(store, run_id, "lint", "a.py:1:1: E501 line too long")
        self._fail(
            store,
            run_id,
            "typecheck",
            "b.ts(4,1): error TS2532: Object is possibly undefined",
        )

        orchestrator._measure_cycle(run_id, ticket)

        self.assertEqual(ticket.distinct_classes, 2)
        self.assertEqual(len(ticket.new_classes), 2)

    def test_a_class_the_ticket_has_seen_before_is_not_new(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = store.list_tickets(run_id)[0]
        self._fail(store, run_id, "lint", "a.py:1:1: E501 line too long")
        orchestrator._measure_cycle(run_id, ticket)
        self._fail(store, run_id, "lint", "a.py:9:1: E501 line too long")
        self._fail(
            store,
            run_id,
            "typecheck",
            "b.ts(4,1): error TS2532: Object is possibly undefined",
        )

        orchestrator._measure_cycle(run_id, ticket)

        self.assertEqual(ticket.distinct_classes, 2)
        self.assertEqual(len(ticket.new_classes), 1)
        self.assertIn("TS2532", ticket.new_classes[0])

    def test_review_objections_count_towards_the_volume(self):
        # The case the axis exists for. Two rejections over four different
        # objections are one class — `review reject` — and four points.
        orchestrator, store, run_id = self._orchestrator()
        ticket = store.list_tickets(run_id)[0]
        self._fail(
            store,
            run_id,
            "review",
            "REJECT\n"
            "- `forge/a.py` never defines `evidence`.\n"
            "- `forge/b.py` never defines `snapshot`.\n",
        )
        self._fail(
            store,
            run_id,
            "review",
            "REJECT\n"
            "- `forge/c.py` never defines `advise`.\n"
            "- `forge/d.py` never defines `release`.\n",
        )

        orchestrator._measure_cycle(run_id, ticket)

        self.assertEqual(ticket.cycle_classes, ["review reject"])
        self.assertEqual(ticket.distinct_classes, 5)

    def test_the_counters_survive_a_reload(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = store.list_tickets(run_id)[0]
        self._fail(store, run_id, "lint", "a.py:1:1: E501 line too long")

        orchestrator._measure_cycle(run_id, ticket)

        reloaded = store.list_tickets(run_id)[0]
        self.assertEqual(reloaded.distinct_classes, ticket.distinct_classes)
        self.assertEqual(reloaded.new_classes, sorted(ticket.new_classes))

    def test_a_large_volume_parks_nothing_and_escalates_nothing(self):
        # §4.1: at the drafted threshold of eight, the two tickets that went on
        # to pass are decomposed and the one unsatisfiable ticket is left
        # alone. So the signal is recorded and the brake is not built.
        orchestrator, store, run_id = self._orchestrator()
        ticket = store.list_tickets(run_id)[0]
        for index in range(12):
            self._fail(
                store, run_id, "lint", f"a{index}.py:1:1: E50{index % 9} bad line"
            )

        orchestrator._measure_cycle(run_id, ticket)

        self.assertGreater(ticket.distinct_classes, 8)
        self.assertEqual(ticket.status, "pending")
        self.assertEqual(ticket.blocked_note, "")


if __name__ == "__main__":
    unittest.main()
