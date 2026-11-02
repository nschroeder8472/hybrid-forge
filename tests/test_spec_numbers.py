"""A revision may say the spec better; it may not change its numbers.

Twice running, a sign-off revision "corrected" measured geometry from general
knowledge: phase 7 of docs/CREATIVE-TOOLS.md rewrote `create_grid(size=2.0)`
as `size=4.0`, and the sprite-sheet run moved a keyhole from y -0.075 to
-0.06, then -0.065. Each broke the criteria the numbers were measured to meet.

What this module holds to:

1. A revision that drops or changes a number the human's spec states is
   refused, and the ticket keeps the text it had.
2. Reformatting a number (`0.10` as `0.1`) and adding numbers are not changes.
3. The anchor is the human's text, so a chain of revisions cannot walk a
   number away one step at a time.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.ratify import _apply  # noqa: E402
from forge.respec import _moved_numbers, spec_numbers  # noqa: E402
from forge.state import Store, Ticket  # noqa: E402

SPEC = (
    "Build the keyhole: a disc translated by (0, -0.075, 0.15) and a slot, "
    "`common.box(bm, (-0.012, -0.08, 0.07), (0.012, -0.065, 0.14), 2)`. "
    "The grid is `create_grid(bm, size=2.0)`, a 4 m square."
)


def ticket() -> Ticket:
    t = Ticket("SHT-002", title="lock", spec=SPEC, criteria=["c"],
               allowed_files=["art/scenes/lock.py"])
    t.original_spec = SPEC
    return t


class MovedNumbersTests(unittest.TestCase):
    def test_a_moved_coordinate_is_caught(self) -> None:
        revised = SPEC.replace("-0.075", "-0.06")
        self.assertEqual(_moved_numbers(ticket(), revised), ["-0.075"])

    def test_a_changed_size_is_caught(self) -> None:
        revised = SPEC.replace("size=2.0", "size=4.0")
        self.assertEqual(_moved_numbers(ticket(), revised), ["2.0"])

    def test_reformatting_is_not_a_change(self) -> None:
        revised = SPEC.replace("2.0", "2").replace("0.15", "0.150")
        self.assertEqual(_moved_numbers(ticket(), revised), [])

    def test_prose_may_change_around_the_numbers(self) -> None:
        revised = (
            "Make a dark disc and move it by (0, -0.075, 0.15); add the slot "
            "`common.box(bm, (-0.012, -0.08, 0.07), (0.012, -0.065, 0.14), 2)`. "
            "Use `create_grid(bm, size=2.0)` for the 4 m plane."
        )
        self.assertEqual(_moved_numbers(ticket(), revised), [])

    def test_added_numbers_are_not_counted_against_a_revision(self) -> None:
        revised = SPEC + " The disc's depth is 0.02, so it reaches y -0.085."
        self.assertEqual(_moved_numbers(ticket(), revised), [])

    def test_the_anchor_is_the_humans_text_not_the_last_revision(self) -> None:
        t = ticket()
        t.spec = SPEC.replace("-0.075", "-0.06")  # an earlier revision got through
        self.assertEqual(_moved_numbers(t, t.spec), ["-0.075"])

    def test_names_with_digits_are_not_numbers(self) -> None:
        self.assertEqual(spec_numbers("x2 attempt_3 3d v1.py"), {})


class RatifyRefusesTests(unittest.TestCase):
    def test_a_sign_off_revision_that_moves_a_number_keeps_the_old_spec(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.db")
        self.addCleanup(store.close_all)
        run_id = store.create_run("goal")
        t = ticket()
        revision = {"spec": SPEC.replace("-0.075", "-0.065")}
        _apply(store, run_id, t, revision, root=None)
        self.assertNotIn("spec", revision)
        messages = [row["message"] for row in store.events_after(0)]
        self.assertTrue(any("-0.075" in m and "refused" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
