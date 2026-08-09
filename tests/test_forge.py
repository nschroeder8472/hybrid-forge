"""Tests for the parts where a silent wrong answer is expensive.

Scope enforcement, reset-time parsing, and plan parsing are all places where a
bug does not raise — it just lets the loop do the wrong thing for hours. Those
get tests; the HTTP adapters do not, since exercising them needs a live model.

    python -m unittest discover tests
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from forge.budget import BudgetGate, RateLimitPolicy
from forge.config import Config, UISettings
from forge.ingest import looks_like_plan, parse_plan, tickets_from_json
from forge.patch import enforce_scope, is_safe_path, matches_any, parse_output
from forge.providers.claude_cli import parse_reset_time
from forge.state import Store, Ticket
from forge.ui.server import exposure_warning, is_exposed


class TestPatchParsing(unittest.TestCase):
    def test_extracts_path_and_body(self):
        parsed = parse_output("slug.py\n```python\nx = 1\n```")
        self.assertEqual(len(parsed.edits), 1)
        self.assertEqual(parsed.edits[0].path, "slug.py")
        self.assertEqual(parsed.edits[0].content, "x = 1\n")

    def test_accepts_labelled_paths(self):
        for header in ("File: a/b.py", "`a/b.py`", "a/b.py:"):
            parsed = parse_output(f"{header}\n```py\nz = 0\n```")
            self.assertEqual(parsed.edits[0].path, "a/b.py", header)

    def test_blocked_short_circuits(self):
        parsed = parse_output("BLOCKED: which error type should this raise?")
        self.assertTrue(parsed.is_blocked)
        self.assertFalse(parsed.edits)

    def test_blocked_after_preamble_still_detected(self):
        parsed = parse_output("I read the spec.\nBLOCKED: two criteria conflict.")
        self.assertTrue(parsed.is_blocked)


class TestScopeEnforcement(unittest.TestCase):
    def test_rejects_paths_outside_allowed_list(self):
        parsed = parse_output("secrets/creds.txt\n```\nnope\n```")
        scoped = enforce_scope(parsed, ["slug.py"], [])
        self.assertFalse(scoped.edits)
        self.assertEqual(len(scoped.rejected), 1)

    def test_never_delegate_beats_an_allowed_list(self):
        # A ticket may not authorize its way past a project-level prohibition.
        parsed = parse_output("src/auth/login.py\n```\nx\n```")
        scoped = enforce_scope(parsed, ["src/auth/login.py"], ["src/auth/**"])
        self.assertFalse(scoped.edits)

    def test_empty_allowed_list_rejects_everything(self):
        parsed = parse_output("anything.py\n```\nx\n```")
        self.assertFalse(enforce_scope(parsed, [], []).edits)

    def test_double_star_matches_nested_paths(self):
        self.assertTrue(matches_any("src/auth/deep/x.py", ["src/auth/**"]))
        self.assertFalse(matches_any("src/authz/x.py", ["src/auth/**"]))

    def test_path_traversal_is_refused(self):
        root = Path.cwd()
        self.assertTrue(is_safe_path(root, "forge/loop.py"))
        self.assertFalse(is_safe_path(root, "../../.ssh/authorized_keys"))


class TestResetTimeParsing(unittest.TestCase):
    """The difference between parking for an hour and dying at 2am."""

    def test_epoch_seconds(self):
        self.assertEqual(parse_reset_time("limit will reset at 1799999999"), 1799999999.0)

    def test_iso_instant(self):
        self.assertIsNotNone(parse_reset_time("resets at 2026-08-09T03:30:00"))

    def test_clock_time_rolls_to_tomorrow_when_already_past(self):
        # 14:00 local; "reset at 9am" must mean tomorrow, not 5 hours ago.
        now = time.mktime(time.struct_time((2026, 8, 8, 14, 0, 0, 5, 220, -1)))
        reset = parse_reset_time("limit will reset at 9am", now=now)
        self.assertIsNotNone(reset)
        self.assertGreater(reset, now)

    def test_no_time_returns_none(self):
        # None is meaningful: the caller waits conservatively instead of
        # guessing a window length.
        self.assertIsNone(parse_reset_time("usage limit reached, try later"))


class TestBudgetGate(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "t.db")

    def test_proactive_window_limit(self):
        gate = BudgetGate(
            self.store, {"m": RateLimitPolicy(tokens_per_window=1000, window_seconds=18000)}
        )
        self.assertIsNone(gate.check_rate_limit("m"))
        gate.record("m", 900, 200)
        self.assertIsNotNone(gate.check_rate_limit("m"))

    def test_park_and_clear(self):
        gate = BudgetGate(self.store, {})
        gate.park("m", time.time() + 60)
        self.assertIsNotNone(gate.check_rate_limit("m"))
        # A successful call proves the window reopened.
        gate.record("m", 1, 1)
        self.assertIsNone(gate.check_rate_limit("m"))

    def test_unconfigured_model_is_never_gated(self):
        self.assertIsNone(BudgetGate(self.store, {}).check_rate_limit("anything"))


class TestIngest(unittest.TestCase):
    PLAN = """# Feature

## AB-001: Do the thing

**Route:** delegate

### Spec

Implement it.

### Allowed files

- `a.py`

### Acceptance criteria

- returns 1 for input 0

## AB-002: Rotate keys

**Route:** claude-only

### Spec

Rotate them.

### Allowed files

- `secrets.py`

### Acceptance criteria

- old keys stop validating
"""

    def test_recognizes_a_plan(self):
        self.assertTrue(looks_like_plan(self.PLAN))
        self.assertFalse(looks_like_plan("Please add PNG export at some point."))

    def test_parses_verbatim_without_a_model(self):
        tickets = parse_plan(self.PLAN)
        self.assertEqual([t.ticket_id for t in tickets], ["AB-001", "AB-002"])
        self.assertEqual(tickets[0].allowed_files, ["a.py"])
        self.assertEqual(tickets[0].criteria, ["returns 1 for input 0"])
        self.assertEqual(tickets[1].route, "claude-only")

    def test_planner_json_tolerates_a_code_fence(self):
        reply = '```json\n{"tickets":[{"id":"X-1","title":"t","spec":"s",' \
                '"allowed_files":["a"],"criteria":["c"]}]}\n```'
        tickets = tickets_from_json(reply)
        self.assertEqual(tickets[0].ticket_id, "X-1")

    def test_planner_garbage_raises(self):
        with self.assertRaises(ValueError):
            tickets_from_json("I could not plan this.")


class TestStoreResume(unittest.TestCase):
    def test_stopped_run_with_work_left_is_resumable(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1"), Ticket("T-2")])
        store.set_run_status(run_id, "stopped")

        # A stopped run is not permanently terminal — an interrupted overnight
        # run must be continuable, not only re-ingestible.
        self.assertIsNone(store.active_run())
        self.assertIsNotNone(store.resumable_run())

        for ticket in store.list_tickets(run_id):
            ticket.status = "done"
            store.update_ticket(run_id, ticket)
        self.assertIsNone(store.resumable_run())

    def test_next_ticket_picks_up_one_left_running_by_a_crash(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", status="running", position=0)])
        self.assertEqual(store.next_ticket(run_id).ticket_id, "T-1")


class TestDashboardExposure(unittest.TestCase):
    """The dashboard has no auth and its stop button ends a run, so a bind
    address that reaches beyond this machine must say so out loud."""

    def _config(self, host: str) -> Config:
        config = Config(root=Path("."))
        config.ui = UISettings(host=host, port=8799)
        return config

    def test_loopback_binds_are_silent(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertFalse(is_exposed(host), host)
            self.assertEqual(exposure_warning(self._config(host)), "")

    def test_wildcard_bind_warns_about_every_network(self):
        warning = exposure_warning(self._config("0.0.0.0"))
        self.assertIn("NO authentication", warning)
        self.assertIn("every network this machine is on", warning)

    def test_specific_non_loopback_bind_names_the_address(self):
        warning = exposure_warning(self._config("192.168.1.10"))
        self.assertIn("192.168.1.10:8799", warning)
        self.assertIn("NO authentication", warning)


if __name__ == "__main__":
    unittest.main()
