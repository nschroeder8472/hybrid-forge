"""The dashboard renders the evidence a parked ticket already has.

`evidence()` in `forge/ui/server.py` shapes the payload; `renderEvidence` in
`forge/ui/index.html` is the half of the exchange that turns it into markup.
The page is a static asset with no Python interface, so the contract is read
straight off the file: that the function is defined and called with the
ticket's evidence, that every part of the payload is read, that the route and
reason are escaped, that the stylesheet opens a rule for the block, and that
the block was added beside the note rather than in place of it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "forge" / "ui" / "index.html"


class TestThePageCarriesTheContract(unittest.TestCase):
    """The acceptance criteria, read straight off the file."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_it_defines_render_evidence(self):
        self.assertIn("function renderEvidence(", self.text)

    def test_it_calls_render_evidence_with_the_ticket_evidence(self):
        self.assertIn("renderEvidence(t.evidence)", self.text)

    def test_it_reads_every_part_of_the_payload(self):
        for name in (
            "e.route", "e.reason", "e.classes", "e.volume",
            "e.flat_cycles", "e.learned", "e.notes",
        ):
            self.assertIn(name, self.text)

    def test_it_escapes_the_route_and_the_reason(self):
        self.assertIn("esc(e.route)", self.text)
        self.assertIn("esc(e.reason)", self.text)

    def test_the_stylesheet_opens_a_rule_for_evidence(self):
        self.assertIn(".evidence {", self.text)

    def test_the_note_is_still_there(self):
        # The block was added beside the note, not in place of it.
        self.assertIn("renderTickets(", self.text)
        self.assertIn("${esc(t.note)}", self.text)


if __name__ == "__main__":
    unittest.main()
