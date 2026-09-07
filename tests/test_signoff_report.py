"""The two counters over ratification, and the audit of a drifted contract.

§8.2 and §8.3 of docs/ADAPTIVE-TICKET-LOOP.md are report-only measurements
over data the loop already records: what each role does in a sign-off pass, and
how much of what went wrong afterwards named a file the pass had in front of
it. §8.1 is the third: a ticket whose criteria moved is checked against the
ones it was given, and the result is a note rather than a status.

What these tests mostly pin is the absence — that none of it changes what the
loop does.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge import signoff
from forge.config import Config, LoopSettings
from forge.loop import Orchestrator
from forge.prompts import parse_criteria_audit
from forge.providers import Completion, Usage
from forge.state import Store, Ticket


def _note(role: str, *, signed: bool, blocking=(), suggestions=()) -> dict:
    return {
        "pass": 1,
        "role": role,
        "signed": signed,
        "blocking": list(blocking),
        "suggestions": list(suggestions),
        "response": "",
    }


class TestParticipation(unittest.TestCase):
    """§8.2: what each role did, counted rather than read."""

    def test_a_role_is_counted_once_per_vote_and_once_per_ticket(self):
        ticket = Ticket(
            "T-1",
            ratify_status="signed",
            ratify_notes=[
                _note("reviewer", signed=True),
                _note("reviewer", signed=False, blocking=["the criterion is unmet"]),
            ],
        )

        [entry] = signoff.participation([ticket])

        self.assertEqual(entry.tickets, 1)
        self.assertEqual(entry.votes, 2)
        self.assertEqual(entry.signed, 1)
        self.assertEqual(entry.blocking, 1)

    def test_a_role_saying_none_has_raised_no_points(self):
        # Both spellings appear in recorded runs. Counting them would make
        # every role look engaged, which is the thing this counter exists to
        # be able to disprove.
        ticket = Ticket(
            "T-1",
            ratify_status="signed",
            ratify_notes=[_note("tester", signed=True, blocking=["NONE"],
                                suggestions=["none."])],
        )

        [entry] = signoff.participation([ticket])

        self.assertEqual(entry.points, 0)
        self.assertEqual(entry.blocking, 0)

    def test_blocking_and_suggestions_both_count_as_points(self):
        ticket = Ticket(
            "T-1",
            ratify_status="signed",
            ratify_notes=[
                _note(
                    "tester",
                    signed=False,
                    blocking=["criterion 3 cannot be tested"],
                    suggestions=["name the file", "quote the value"],
                )
            ],
        )

        [entry] = signoff.participation([ticket])

        self.assertEqual(entry.points, 3)

    def test_a_role_that_never_blocks_is_named_only_past_the_window(self):
        signer = [_note("planner", signed=True)]
        few = signoff.Report(
            participation=signoff.participation(
                [Ticket("T-1", ratify_status="signed", ratify_notes=signer)]
            ),
            ratified=3,
            window=20,
        )
        self.assertEqual(few.rubber_stamps, [])

        many = signoff.Report(
            participation=few.participation, ratified=20, window=20
        )
        self.assertEqual([entry.role for entry in many.rubber_stamps], ["planner"])


class TestEfficacy(unittest.TestCase):
    """§8.3: how much of what failed was visible when the roles signed."""

    def setUp(self) -> None:
        self.store = Store(Path(tempfile.mkdtemp()).resolve() / "t.db")
        self.run_id = self.store.create_run("goal")

    def tearDown(self) -> None:
        self.store.close()

    def _ticket(self, **fields) -> Ticket:
        ticket = Ticket("T-1", ratify_status="signed", **fields)
        self.store.add_tickets(self.run_id, [ticket])
        return ticket

    def _fail(self, name: str, detail: str) -> None:
        step_id = self.store.start_step(self.run_id, "T-1", name)
        self.store.end_step(step_id, "failed", detail)

    def test_a_failure_in_a_file_the_ticket_owned_was_visible(self):
        ticket = self._ticket(allowed_files=["forge/a.py"])
        self._fail("lint", "forge/a.py:1:1: E501 line too long")

        [row] = signoff.efficacy(self.store, self.run_id, [ticket])

        self.assertEqual((row.total, row.visible), (1, 1))

    def test_a_failure_in_a_file_nobody_listed_was_not(self):
        ticket = self._ticket(allowed_files=["forge/a.py"])
        self._fail("lint", "vendor/b.py:1:1: E501 line too long")

        [row] = signoff.efficacy(self.store, self.run_id, [ticket])

        self.assertEqual((row.total, row.visible), (1, 0))

    def test_a_path_a_tool_printed_with_a_prefix_is_the_same_file(self):
        # `flake8` prints `./forge/a.py` and the plan writes `forge/a.py`.
        ticket = self._ticket(allowed_files=["forge/a.py"])
        self._fail("lint", "./forge/a.py:1:1: E501 line too long")

        [row] = signoff.efficacy(self.store, self.run_id, [ticket])

        self.assertEqual(row.visible, 1)

    def test_a_reference_file_counts_as_scope(self):
        ticket = self._ticket(allowed_files=[], reference_files=["forge/a.py"])
        self._fail("lint", "forge/a.py:1:1: E501 line too long")

        [row] = signoff.efficacy(self.store, self.run_id, [ticket])

        self.assertEqual(row.visible, 1)

    def test_review_objections_are_counted_beside_tool_failures(self):
        ticket = self._ticket(allowed_files=["forge/a.py"])
        self._fail("review", "REJECT\n- `forge/a.py` never defines `evidence`.\n")

        [row] = signoff.efficacy(self.store, self.run_id, [ticket])

        self.assertEqual(row.points, 1)
        self.assertEqual(row.points_in_scope, 1)

    def test_a_ticket_that_never_went_through_signoff_is_not_reported(self):
        ticket = Ticket("T-2")
        self.store.add_tickets(self.run_id, [ticket])

        self.assertEqual(signoff.efficacy(self.store, self.run_id, [ticket]), [])


class TestTheRenderedReport(unittest.TestCase):
    """What `forge signoff` prints."""

    def test_a_run_with_no_ratified_ticket_says_so(self):
        text = signoff.render(signoff.Report())

        self.assertIn("has been through a sign-off pass", text)

    def test_the_report_names_every_role_that_voted(self):
        tickets = [
            Ticket(
                "T-1",
                ratify_status="signed",
                ratify_notes=[_note("planner", signed=True), _note("tester", signed=True)],
            )
        ]
        text = signoff.render(
            signoff.Report(
                participation=signoff.participation(tickets), ratified=1, window=20
            )
        )

        self.assertIn("planner", text)
        self.assertIn("tester", text)


class TestReadingAnAudit(unittest.TestCase):
    """§8.1's parser, which fails open where `parse_verdict` fails closed."""

    def test_a_covered_verdict_reads_as_covered(self):
        self.assertEqual(parse_criteria_audit("COVERED"), (True, ""))

    def test_a_reduction_carries_the_note(self):
        covered, note = parse_criteria_audit(
            "SCOPE-REDUCED\n- \"handles every case\": the diff handles the common one"
        )

        self.assertFalse(covered)
        self.assertIn("handles every case", note)

    def test_an_unreadable_reply_is_not_a_reduction(self):
        # The opposite of `parse_verdict`, and deliberate: this check cannot
        # change a ticket's status, so an unreadable reply must not manufacture
        # a report about a ticket nobody has evidence against.
        covered, _ = parse_criteria_audit("I am not sure what you are asking.")

        self.assertTrue(covered)

    def test_an_empty_reply_is_not_a_reduction(self):
        self.assertEqual(parse_criteria_audit(""), (True, ""))


class TestAuditingAPassedTicket(unittest.TestCase):
    """§8.1 in the loop: when it runs, and what it is allowed to do."""

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
        self.addCleanup(store.close)
        return Orchestrator(config, store), store, run_id

    def test_a_ticket_whose_criteria_never_moved_is_not_audited(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = Ticket(
            "T-1",
            status="done",
            criteria=["holds every case"],
            original_criteria=["holds every case"],
        )
        store.add_tickets(run_id, [ticket])
        asked = []
        orchestrator._converse = lambda *a, **k: asked.append(a) or Completion(
            text="COVERED", usage=Usage()
        )

        orchestrator._audit_criteria(run_id, ticket)

        self.assertEqual(asked, [])

    def test_a_reduction_is_recorded_and_the_ticket_stays_done(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = Ticket(
            "T-1",
            status="done",
            criteria=["holds the common case"],
            original_criteria=["holds every case"],
        )
        store.add_tickets(run_id, [ticket])
        orchestrator._converse = lambda *a, **k: Completion(
            text='SCOPE-REDUCED\n- "holds every case": only the common one is met',
            usage=Usage(),
        )

        orchestrator._audit_criteria(run_id, ticket)

        self.assertEqual(ticket.status, "done")
        self.assertIn("holds every case", ticket.scope_note)
        self.assertIn("holds every case", store.list_tickets(run_id)[0].scope_note)

    def test_a_covered_audit_leaves_no_note(self):
        orchestrator, store, run_id = self._orchestrator()
        ticket = Ticket(
            "T-1",
            status="done",
            criteria=["holds every case, exactly"],
            original_criteria=["holds every case"],
        )
        store.add_tickets(run_id, [ticket])
        orchestrator._converse = lambda *a, **k: Completion(
            text="COVERED", usage=Usage()
        )

        orchestrator._audit_criteria(run_id, ticket)

        self.assertEqual(ticket.scope_note, "")

    def test_the_audit_step_cannot_add_a_failure_class(self):
        # It runs after the ticket has passed. A step that could class a
        # failure would put one on a ticket nothing is going to retry.
        orchestrator, store, run_id = self._orchestrator()
        ticket = Ticket(
            "T-1",
            status="done",
            criteria=["holds the common case"],
            original_criteria=["holds every case"],
        )
        store.add_tickets(run_id, [ticket])
        orchestrator._converse = lambda *a, **k: Completion(
            text="SCOPE-REDUCED\n- \"holds every case\": not met", usage=Usage()
        )

        orchestrator._audit_criteria(run_id, ticket)

        self.assertEqual(store.ticket_classes(run_id, "T-1"), [])


if __name__ == "__main__":
    unittest.main()
