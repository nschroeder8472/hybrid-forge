"""The dashboard opens a parked ticket in front of the page.

TI-002. `ticket_detail()` in `forge/ui/server.py` shapes the payload;
`openInspector` and `renderInspector` in `forge/ui/index.html` are the half of
the exchange that turns it into markup. The page is a static asset with no
Python interface, so the contract is read straight off the file.

Written by hand rather than by the loop: the sign-off pass trimmed this ticket's
`allowed_files` to the page alone, so the ticket had no designated test path and
its tester was never asked for one. Its criteria were settled by a reviewer's
reading and by the pre-existing suite — which is exactly the state this file
exists to end.

What it pins is the shape the ticket landed with, including the one criterion
the sign-off pass corrected: `renderWriteControls` stays, and moves inside the
panel. The original criterion demanded deleting it, which would have scattered
the fields it builds; keeping the helper and moving its only call site is what
makes each field id exist once on the page.

    python -m unittest discover tests
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "forge" / "ui" / "index.html"


class TestThePageOpensAParkedTicket(unittest.TestCase):
    """The panel itself, read off the file."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_the_page_carries_a_container_for_the_panel(self):
        self.assertIn('id="inspector"', self.text)

    def test_it_defines_the_three_functions(self):
        for name in ("openInspector(", "renderInspector(", "closeInspector("):
            self.assertIn(f"function {name}", self.text)

    def test_it_fetches_the_per_ticket_endpoint_with_the_id_encoded(self):
        self.assertIn("/api/ticket/", self.text)
        self.assertIn("encodeURIComponent", self.text)

    def test_a_ticket_row_opens_it(self):
        self.assertIn('onclick="openInspector(', self.text)

    def test_the_stylesheet_opens_a_rule_for_the_panel(self):
        self.assertIn("#inspector {", self.text)
        self.assertIn(".inspector-panel {", self.text)


class TestThePanelReadsTheWholePayload(unittest.TestCase):
    """Every part of what `ticket_detail` returns reaches the page."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_it_reads_the_tickets_own_text(self):
        for name in ("d.spec", "d.criteria", "d.files"):
            self.assertIn(name, self.text)

    def test_it_reads_the_failures(self):
        self.assertIn("d.failures", self.text)

    def test_failure_text_keeps_its_line_breaks(self):
        # The complaint this ticket was written from: the row showed a failure
        # *class*, capped at 60 characters by `failures._message_of` for
        # counting, and the text that explains the failure was nowhere. It is
        # here, and it is in a `<pre>` so it reads as the tool wrote it.
        after = self.text.split("d.failures", 1)[1][:400]
        self.assertIn("<pre", after)

    def test_the_route_and_the_status_are_escaped(self):
        self.assertIn("esc(d.route)", self.text)
        self.assertIn("esc(d.status)", self.text)


class TestTheTwoWritesAreEquals(unittest.TestCase):
    """The second complaint: a criterion box that read as an afterthought."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_the_note_and_the_criterion_are_both_textareas(self):
        self.assertEqual(self.text.count("<textarea"), 2)

    def test_neither_field_is_a_single_line_input(self):
        # The shape that made one write look smaller than the other.
        self.assertNotIn('<input id="criterion-', self.text)
        self.assertNotIn('<input id="note-', self.text)

    def test_each_field_id_exists_once_on_the_page(self):
        # Two copies would break every write: `getElementById` answers with
        # the first, which would be whichever copy rendered first. Counted as
        # rendered `id=` attributes rather than as bare text — every one of
        # these names appears a second time as the lookup in `writeNote`,
        # `writeCriterion` and `releaseTicket`.
        for field in ("note", "criterion", "status", "release"):
            self.assertEqual(
                self.text.count(f'id="{field}-${{id}}"'), 1, field
            )

    def test_the_controls_are_built_inside_the_panel(self):
        # The criterion the sign-off pass corrected. The helper stays; what
        # moved is its only call site, from the backlog row into the panel.
        self.assertIn("function renderWriteControls(", self.text)
        panel = self.text.split("function renderInspector(", 1)[1].split(
            "function closeInspector(", 1
        )[0]
        self.assertIn("renderWriteControls(", panel)

    def test_the_backlog_row_carries_no_write_controls(self):
        rows = self.text.split("function renderTickets(", 1)[1].split(
            "function renderEvidence(", 1
        )[0]
        self.assertNotIn("renderWriteControls(", rows)
        self.assertNotIn("<textarea", rows)

    def test_the_three_writes_still_exist(self):
        for name in ("writeNote(", "writeCriterion(", "releaseTicket("):
            self.assertIn(f"function {name}", self.text)


class TestThePanelCloses(unittest.TestCase):
    """Two ways out, because a panel with one is a panel someone gets stuck in."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_the_escape_key_closes_it(self):
        self.assertIn("Escape", self.text)
        self.assertIn("closeInspector()", self.text)

    def test_a_click_on_the_backdrop_closes_it(self):
        # On the backdrop itself rather than anywhere inside, or a click in a
        # text field would dismiss the panel it is typing into.
        self.assertRegex(
            self.text, re.compile(r"e\.target\s*===\s*\$\(\"inspector\"\)")
        )


class TestTheRowKeptWhatItHad(unittest.TestCase):
    """The panel was added beside the evidence lines, not in place of them."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_the_evidence_block_is_still_rendered_on_the_row(self):
        self.assertIn("function renderEvidence(", self.text)
        self.assertIn("renderEvidence(t.evidence)", self.text)

    def test_the_row_still_shows_the_blocked_note(self):
        self.assertIn("${esc(t.note)}", self.text)


if __name__ == "__main__":
    unittest.main()
