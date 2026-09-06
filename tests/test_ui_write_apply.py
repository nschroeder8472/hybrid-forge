"""The three per-ticket writes, applied from a decoded request.

HW-003. `apply_write` is the whole of the dashboard's write side: a note, a
criterion and a release, each of them one call into the store and one row in
the event log. These tests pin the decisions in the order the function makes
them — the gate before the path, the path before the run, the run before the
ticket, the text before the write — and the state each accepted write leaves
behind, read back out of the store rather than trusted from the return value.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge.config import Config
from forge.state import Store, Ticket
from forge.ui.writes import apply_write


def make_config(
    root: Path, *, host: str = "127.0.0.1", allow_remote_writes: bool = False
) -> Config:
    config = Config(
        root=root,
        models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
        roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
    )
    config.ui.host = host
    config.ui.allow_remote_writes = allow_remote_writes
    return config


def make_store(root: Path, ticket: Ticket | None = None) -> tuple[Store, int]:
    store = Store(root / "t.db")
    run_id = store.create_run("goal")
    if ticket is not None:
        store.add_tickets(run_id, [ticket])
    return store, run_id


def ticket(**fields) -> Ticket:
    return Ticket("T-1", **fields)


class TestANote(unittest.TestCase):
    """The write the endpoint exists for: a person says something to the loop."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.store, self.run_id = make_store(self.root, ticket(status="blocked"))
        self.config = make_config(self.root)

    def tearDown(self):
        self.store.close()

    def test_a_note_lands_on_the_ticket(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/note",
            {"text": "  the fixtures exist now  "},
            "127.0.0.1",
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["notes"], 1)
        notes = self.store.list_tickets(self.run_id)[0].human_note
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "the fixtures exist now")

    def test_a_refused_caller_writes_nothing(self):
        config = make_config(self.root, host="0.0.0.0")

        status, body = apply_write(
            self.store,
            config,
            "/api/ticket/T-1/note",
            {"text": "the fixtures exist now"},
            "10.0.0.4",
        )

        self.assertEqual(status, 403)
        self.assertIn("allowRemoteWrites", body["error"])
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_blank_text_is_refused_by_name(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/note", {"text": "   "}, "127.0.0.1"
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "a note needs text")
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_text_over_the_limit_is_refused_naming_the_limit(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/note", {"text": "x" * 2001}, "127.0.0.1"
        )

        self.assertEqual(status, 400)
        self.assertIn("2000", body["error"])
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_text_at_the_limit_is_accepted_whole(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/note", {"text": "x" * 2000}, "127.0.0.1"
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        notes = self.store.list_tickets(self.run_id)[0].human_note
        self.assertEqual(len(notes[0]["text"]), 2000)

    def test_the_event_log_carries_who_and_from_where(self):
        apply_write(self.store, self.config, "/api/ticket/T-1/note", {"text": "ok"}, "127.0.0.1")

        rows = [row for row in self.store.events_after(0) if row["kind"] == "ticket"]
        self.assertEqual(len(rows), 1)
        data = json.loads(rows[0]["data"])
        self.assertEqual(data["author"], "human")
        self.assertEqual(data["via"], "dashboard")
        self.assertEqual(data["peer"], "127.0.0.1")
        self.assertEqual(data["ticket"], "T-1")


class TestACriterion(unittest.TestCase):
    """A person adds to the standard the loop is judged against."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.store, self.run_id = make_store(self.root, ticket(status="blocked"))
        self.config = make_config(self.root)

    def tearDown(self):
        self.store.close()

    def test_a_new_criterion_is_adopted_into_both_lists(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/criterion",
            {"text": "capture returns a seed of 3130775471"},
            "127.0.0.1",
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["adopted"], ["capture returns a seed of 3130775471"])
        stored = self.store.list_tickets(self.run_id)[0]
        self.assertIn("capture returns a seed of 3130775471", stored.criteria)
        self.assertIn("capture returns a seed of 3130775471", stored.original_criteria)

    def test_the_same_criterion_twice_is_adopted_once(self):
        payload = {"text": "capture returns a seed of 3130775471"}
        first = apply_write(
            self.store, self.config, "/api/ticket/T-1/criterion", payload, "127.0.0.1"
        )
        second = apply_write(
            self.store, self.config, "/api/ticket/T-1/criterion", payload, "127.0.0.1"
        )

        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(second[1]["adopted"], [])
        stored = self.store.list_tickets(self.run_id)[0]
        self.assertEqual(stored.criteria.count("capture returns a seed of 3130775471"), 1)

    def test_blank_text_is_refused_by_name(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/criterion", {"text": ""}, "127.0.0.1"
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "a criterion needs text")
        self.assertEqual(self.store.list_tickets(self.run_id)[0].criteria, [])


class TestARoute(unittest.TestCase):
    """The release: a ticket a person withheld goes back in front of the executor."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.store, self.run_id = make_store(
            self.root, ticket(status="withheld", route="withheld:security")
        )
        self.config = make_config(self.root)

    def tearDown(self):
        self.store.close()

    def test_a_withheld_ticket_is_released(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/route",
            {"route": "delegate", "text": "the scope no longer touches auth"},
            "127.0.0.1",
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["released"])
        self.assertEqual(body["route"], "delegate")
        stored = self.store.list_tickets(self.run_id)[0]
        self.assertEqual(stored.route, "delegate")
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.attempts, 0)
        self.assertEqual(
            stored.human_note[-1]["text"],
            "Released from withheld: security: the scope no longer touches auth",
        )

    def test_a_ticket_already_delegated_is_not_touched(self):
        self.store, _ = make_store(self.root, ticket(status="blocked", route="delegate"))

        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/route",
            {"route": "delegate", "text": "the scope no longer touches auth"},
            "127.0.0.1",
        )

        self.assertEqual(status, 200)
        self.assertFalse(body["released"])
        self.assertEqual(body["route"], "delegate")
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_only_delegate_is_accepted(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/route",
            {"route": "skip", "text": "why not"},
            "127.0.0.1",
        )

        self.assertEqual(status, 400)
        self.assertIn("delegate", body["error"])
        self.assertEqual(self.store.list_tickets(self.run_id)[0].route, "withheld:security")

    def test_a_release_without_a_reason_is_refused(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/route",
            {"route": "delegate", "text": ""},
            "127.0.0.1",
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "releasing a withheld ticket needs a reason")
        self.assertEqual(self.store.list_tickets(self.run_id)[0].route, "withheld:security")

    def test_the_release_is_logged_with_what_it_was(self):
        apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/route",
            {"route": "delegate", "text": "the scope no longer touches auth"},
            "127.0.0.1",
        )

        rows = [row for row in self.store.events_after(0) if row["kind"] == "ticket"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "warn")
        data = json.loads(rows[0]["data"])
        self.assertEqual(data["was"], "withheld:security")
        self.assertEqual(data["author"], "human")
        self.assertEqual(data["via"], "dashboard")
        self.assertEqual(data["peer"], "127.0.0.1")
        self.assertEqual(data["ticket"], "T-1")


class TestTheShapeOfTheRequest(unittest.TestCase):
    """Path, run and ticket: what a request may name, and what it may not."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.store, self.run_id = make_store(self.root, ticket(status="blocked"))
        self.config = make_config(self.root)

    def tearDown(self):
        self.store.close()

    def test_a_ticket_the_run_does_not_hold_is_not_found(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-9/note", {"text": "ok"}, "127.0.0.1"
        )

        self.assertEqual(status, 404)
        self.assertIn("T-9", body["error"])
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_an_unknown_action_is_not_found(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/frobnicate", {"text": "ok"}, "127.0.0.1"
        )

        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_a_path_with_too_few_segments_is_not_found(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1", {"text": "ok"}, "127.0.0.1"
        )

        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_a_path_that_is_not_a_ticket_write_is_not_found(self):
        status, body = apply_write(
            self.store, self.config, "/api/state", {"text": "ok"}, "127.0.0.1"
        )

        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_a_run_the_request_does_not_match_is_a_conflict(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/note", {"text": "ok", "run": 999}, "127.0.0.1"
        )

        self.assertEqual(status, 409)
        self.assertIn(str(self.run_id), body["error"])
        self.assertIn("999", body["error"])
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_a_run_the_request_matches_is_served(self):
        status, body = apply_write(
            self.store,
            self.config,
            "/api/ticket/T-1/note",
            {"text": "ok", "run": self.run_id},
            "127.0.0.1",
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_a_run_that_is_not_an_integer_is_a_conflict(self):
        status, body = apply_write(
            self.store, self.config, "/api/ticket/T-1/note", {"text": "ok", "run": "1"}, "127.0.0.1"
        )

        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_tickets(self.run_id)[0].human_note, [])

    def test_a_store_with_no_runs_says_so(self):
        store = Store(self.root / "empty.db")
        try:
            status, body = apply_write(
                store, self.config, "/api/ticket/T-1/note", {"text": "ok"}, "127.0.0.1"
            )
        finally:
            store.close()

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "no runs yet")


if __name__ == "__main__":
    unittest.main()
