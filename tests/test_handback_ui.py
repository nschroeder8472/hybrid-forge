"""The dashboard's read side for a parked ticket: the evidence block.

HD-001. `snapshot()` already loads every value a person needs to decide what
to do with a parked ticket — its withheld reason, its `learned` entries, its
repeated failure classes — and throws it away. These tests pin the block that
hands it back, and that only a parked ticket carries it.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.config import Config
from forge.state import Store, Ticket
from forge.ui import server as ui_server


class TestEvidenceForAParkedTicket(unittest.TestCase):
    """The block itself, built from a ticket rather than a run."""

    def _ticket(self, **fields) -> Ticket:
        return Ticket("T-1", **fields)

    def test_a_blocked_ticket_with_a_security_route_reads_as_the_glossary_says(self):
        ticket = self._ticket(status="blocked", route="withheld:security")

        block = ui_server.evidence(ticket)

        self.assertEqual(block["route"], "withheld: security")
        self.assertEqual(
            block["reason"], "authentication, authorization, session handling, secrets"
        )

    def test_a_blocked_ticket_that_was_delegated_carries_no_reason(self):
        ticket = self._ticket(status="blocked", route="delegate")

        block = ui_server.evidence(ticket)

        self.assertEqual(block["route"], "delegate")
        self.assertEqual(block["reason"], "")

    def test_the_legacy_route_reads_as_unspecified(self):
        ticket = self._ticket(status="withheld", route="claude-only")

        block = ui_server.evidence(ticket)

        self.assertEqual(block["route"], "withheld: unspecified")
        self.assertEqual(block["reason"], "no reason was recorded")

    def test_every_parked_status_carries_evidence_and_done_does_not(self):
        for status in Store.RETRYABLE:
            self.assertIsInstance(ui_server.evidence(self._ticket(status=status)), dict)
        self.assertIsNone(ui_server.evidence(self._ticket(status="done")))

    def test_learned_is_capped_at_five_and_keeps_stored_order(self):
        learned = [{"text": f"fact {i}", "count": i + 1} for i in range(7)]
        ticket = self._ticket(status="blocked", learned=learned)

        block = ui_server.evidence(ticket)

        self.assertEqual(len(block["learned"]), 5)
        self.assertEqual(block["learned"][0]["text"], learned[0]["text"])

    def test_a_long_learning_is_cut_to_400_characters_with_no_marker(self):
        ticket = self._ticket(status="blocked", learned=[{"text": "x" * 900, "count": 2}])

        block = ui_server.evidence(ticket)

        self.assertEqual(block["learned"][0]["text"], "x" * 400)
        self.assertEqual(block["learned"][0]["count"], 2)

    def test_notes_are_the_last_five_in_stored_order(self):
        notes = [{"text": f"n{i}", "at": f"t{i}"} for i in range(7)]
        ticket = self._ticket(status="blocked", human_note=notes)

        block = ui_server.evidence(ticket)

        texts = [entry["text"] for entry in block["notes"]]
        self.assertEqual(texts, ["n2", "n3", "n4", "n5", "n6"])

    def test_cycle_evidence_is_copied_not_aliased(self):
        ticket = self._ticket(
            status="blocked",
            cycle_classes=["lint", "tests"],
            cycle_volume=3,
            flat_cycles=2,
        )

        block = ui_server.evidence(ticket)

        self.assertEqual(block["classes"], ["lint", "tests"])
        self.assertEqual(block["volume"], 3)
        self.assertEqual(block["flat_cycles"], 2)
        block["classes"].append("more")
        self.assertEqual(ticket.cycle_classes, ["lint", "tests"])


class TestTheSnapshotCarriesEvidenceOnlyForParkedTickets(unittest.TestCase):
    """The block in the payload: present for the parked ticket, absent for the
    done one, and the entry's existing keys untouched."""

    def _config(self, root):
        return Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
        )

    def test_parked_ticket_carries_evidence_and_done_does_not(self):
        root = Path(tempfile.mkdtemp())
        store = Store(root / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            [
                Ticket(
                    "T-1",
                    title="the parked one",
                    route="withheld:security",
                    status="blocked",
                    attempts=2,
                    allowed_files=["src/a.py"],
                    criteria=["c1"],
                    blocked_note="needs a human",
                ),
                Ticket("T-2", title="the done one", status="done"),
            ],
        )

        state = ui_server.snapshot(store, self._config(root))
        by_id = {entry["id"]: entry for entry in state["tickets"]}

        parked = by_id["T-1"]
        self.assertIsInstance(parked["evidence"], dict)
        self.assertNotIn("evidence", by_id["T-2"])

        for key in ("id", "title", "route", "status", "attempts", "files", "criteria", "note"):
            self.assertIn(key, parked)
        self.assertEqual(parked["note"], "needs a human")
