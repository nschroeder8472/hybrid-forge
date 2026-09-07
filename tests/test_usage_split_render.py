"""The dashboard renders the usage split: input and output as their own columns.

US-002. `snapshot()` in `forge/ui/server.py` already carries `input_display`
and `output_display` beside `display` (US-001); the page is the half of the
exchange that turns them into markup. The page is a static asset with no
Python interface, so the contract is read straight off the file: that the
header names the five columns in order, that the row template reads the two
new keys through `esc` the way it reads `display`, and that the empty-state
row still spans the whole table.

    python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "forge" / "ui" / "index.html"


class TestThePageCarriesTheUsageSplit(unittest.TestCase):
    """The acceptance criteria, read straight off the file."""

    def setUp(self):
        self.text = INDEX.read_text("utf-8")

    def test_the_header_names_the_split_columns(self):
        self.assertIn("<th>In</th>", self.text)
        self.assertIn("<th>Out</th>", self.text)

    def test_the_row_reads_the_split_through_esc(self):
        self.assertIn("esc(u.input_display)", self.text)
        self.assertIn("esc(u.output_display)", self.text)

    def test_the_total_cell_still_reads_display(self):
        self.assertIn("esc(u.display)", self.text)

    def test_the_empty_row_spans_the_whole_table(self):
        self.assertIn('colspan="5"', self.text)
        self.assertNotIn('colspan="3"', self.text)

    def test_the_columns_appear_in_order(self):
        cells = (
            "<th>Model</th>",
            "<th>Calls</th>",
            "<th>In</th>",
            "<th>Out</th>",
            "<th>Total</th>",
        )
        positions = [self.text.index(cell) for cell in cells]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
