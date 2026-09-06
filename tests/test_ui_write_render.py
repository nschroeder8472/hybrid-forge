"""The dashboard renders the controls a parked ticket carries.

`apply_write` in `forge/ui/writes.py` serves the three writes — a note, a
criterion, a release — and `renderWriteControls` in `forge/ui/index.html` is
the half of the exchange that gives them a field to read from and a status
span to answer in. The page is a static asset with no Python interface, so
the contract is read straight off the file, the way
`test_handback_ui_render.py` reads the evidence contract: that the function
is defined and called after the evidence block, that the three handlers post
to the three endpoints with the run the page is showing, that the ids and
the button texts are there, and that the controls were added beside the
evidence and the note rather than in place of either.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "forge" / "ui" / "index.html"


class TestThePageCarriesTheWriteControls(unittest.TestCase):
    """The acceptance criteria, read straight off the file."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_it_defines_render_write_controls(self):
        self.assertIn("function renderWriteControls(", self.text)

    def test_it_calls_render_write_controls_after_the_evidence(self):
        self.assertIn("renderWriteControls(t)", self.text)
        self.assertGreater(
            self.text.index("renderWriteControls(t)"),
            self.text.index("renderEvidence(t.evidence)"),
        )

    def test_it_defines_the_three_write_handlers(self):
        names = (
            "async function writeNote(",
            "async function writeCriterion(",
            "async function releaseTicket(",
        )
        for name in names:
            self.assertIn(name, self.text)

    def test_it_posts_to_the_three_endpoints_with_an_encoded_id(self):
        for path in ("/note", "/criterion", "/route"):
            self.assertIn(path, self.text)
        self.assertIn("encodeURIComponent(", self.text)

    def test_the_endpoints_are_the_ticket_endpoints(self):
        self.assertIn('"/api/ticket/', self.text)

    def test_the_fields_and_the_status_span_carry_their_id_prefixes(self):
        for prefix in ("note-", "criterion-", "release-", "status-"):
            self.assertIn(prefix, self.text)

    def test_the_buttons_read_as_the_spec_says(self):
        for label in ("Add note", "Add criterion", "Release"):
            self.assertIn(label, self.text)

    def test_the_status_span_reads_the_outcome(self):
        messages = (
            "noted",
            "criterion added",
            "released",
            "already a criterion",
        )
        for message in messages:
            self.assertIn(message, self.text)

    def test_the_ticket_id_is_escaped_before_it_is_interpolated(self):
        self.assertIn("esc(t.id)", self.text)

    def test_the_write_names_the_run_the_page_is_showing(self):
        self.assertIn("state.run && state.run.id", self.text)

    def test_the_stylesheet_opens_a_rule_for_the_controls(self):
        self.assertIn(".writes {", self.text)
        self.assertIn(".write-status {", self.text)

    def test_the_controls_sit_beside_the_evidence_and_the_note(self):
        self.assertIn("renderEvidence(t.evidence)", self.text)
        self.assertIn("${esc(t.note)}", self.text)


if __name__ == "__main__":
    unittest.main()
