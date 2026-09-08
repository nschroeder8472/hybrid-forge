"""Sign-off may narrow a ticket's scope, but not out of its own test file.

`respec` has been held to this since run 1 of `HANDBACK-DASHBOARD.md`, where a
revision returned an `allowed_files` without the declared test path and the
rerun put three E501s in an invented file the executor could not open. The
sign-off pass was not, and it does the same thing one step earlier: TI-002 of
`TICKET-INSPECTOR.md` had `tests/test_inspector_render.py` trimmed out of its
scope before any code existed, so `_test_target` found no designated path,
nothing was ever asked to write tests, and thirteen criteria were settled by a
reviewer reading the diff.

Both callers now go through `patch.keep_test_paths`, so what these pin is the
rule at the sign-off end of it.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge import ratify
from forge.patch import keep_test_paths
from forge.providers import Completion, Usage
from forge.state import Store, Ticket


class TestTheSharedRule(unittest.TestCase):
    """`patch.keep_test_paths`, which both revisers ask."""

    def test_a_scope_that_keeps_no_test_file_gets_its_own_back(self):
        scope, kept = keep_test_paths(
            ["forge/ui/index.html", "tests/test_inspector_render.py"],
            ["forge/ui/index.html"],
        )

        self.assertIn("tests/test_inspector_render.py", scope)
        self.assertEqual(kept, ["tests/test_inspector_render.py"])

    def test_swapping_one_test_file_for_another_stands(self):
        # A rule about the count, not the path: a ticket that names a different
        # test file is still repairable through it.
        scope, kept = keep_test_paths(
            ["a.py", "tests/old_test.py"], ["a.py", "tests/new_test.py"]
        )

        self.assertEqual(kept, [])
        self.assertNotIn("tests/old_test.py", scope)

    def test_narrowing_anything_else_is_left_alone(self):
        scope, kept = keep_test_paths(
            ["a.py", "b.py", "tests/x_test.py"], ["a.py", "tests/x_test.py"]
        )

        self.assertEqual(kept, [])
        self.assertNotIn("b.py", scope)

    def test_a_ticket_that_never_had_one_is_unchanged(self):
        scope, kept = keep_test_paths(["a.py"], ["a.py", "b.py"])

        self.assertEqual((scope, kept), (["a.py", "b.py"], []))


class TestRatificationKeepsTheTestPath(unittest.TestCase):
    """The same rule where the loss is quietest: before any code exists."""

    def _ratify(self, proposed_scope: list[str]) -> tuple[Ticket, Store]:
        root = Path(tempfile.mkdtemp()).resolve()
        store = Store(root / "t.db")
        run_id = store.create_run("scope")
        store.add_tickets(
            run_id,
            [
                Ticket(
                    "TI-002",
                    spec="old",
                    criteria=["the page carries a panel"],
                    allowed_files=[
                        "forge/ui/index.html",
                        "tests/test_inspector_render.py",
                    ],
                )
            ],
        )
        ticket = store.list_tickets(run_id)[0]

        def call(role, _messages, _budget, **_options):
            # One role blocks, so the planner is asked for a revision at all;
            # the revision narrows the scope out of the test file.
            if role == "planner" and self.asked:
                return Completion(
                    text=json.dumps(
                        {"spec": "new", "allowed_files": proposed_scope}
                    ),
                    usage=Usage(),
                    finish_reason="stop",
                )
            self.asked = True
            blocking = "SIGNOFF: no\nBLOCKING:\n- the scope is wrong\nSUGGEST:\n- NONE"
            signing = "SIGNOFF: yes\nBLOCKING:\n- NONE\nSUGGEST:\n- NONE"
            reply = blocking if role == "tester" else signing
            return Completion(text=reply, usage=Usage(), finish_reason="stop")

        self.asked = False
        ratify.ratify(
            store,
            run_id,
            ticket,
            call=call,
            budget_for=lambda role: 4096,
            roles=("planner", "executor", "tester", "reviewer"),
            passes=2,
            root=root,
        )
        return store.list_tickets(run_id)[0], store

    def test_a_dropped_test_file_is_put_back(self):
        ticket, _ = self._ratify(["forge/ui/index.html"])

        self.assertIn("tests/test_inspector_render.py", ticket.allowed_files)

    def test_keeping_it_is_reported(self):
        _, store = self._ratify(["forge/ui/index.html"])

        logged = " ".join(row["message"] for row in store.events_after(0))
        self.assertIn("tests/test_inspector_render.py", logged)
        self.assertIn("writable scope", logged)

    def test_the_rest_of_the_revision_still_lands(self):
        # Kept, not refused whole: the narrowing may be right about everything
        # else it says.
        ticket, _ = self._ratify(["forge/ui/index.html"])

        self.assertIn("forge/ui/index.html", ticket.allowed_files)


if __name__ == "__main__":
    unittest.main()
