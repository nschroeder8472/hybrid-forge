"""Tests for the parts where a silent wrong answer is expensive.

Scope enforcement, reset-time parsing, and plan parsing are all places where a
bug does not raise — it just lets the loop do the wrong thing for hours. Those
get tests; the HTTP adapters do not, since exercising them needs a live model.

    python -m unittest discover tests
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from forge import cli, respec
from forge.artifacts import Artifacts
from forge.budget import BudgetGate, RateLimitPolicy
from forge.config import Config, ConfigError, LoopSettings, UISettings
from forge.ingest import (
    derive_needs,
    graph_problems,
    looks_like_plan,
    parse_plan,
    plan_with_model,
    render_ticket,
    shared_file_conflicts,
    tickets_from_json,
)
from forge.ingest import ingest as ingest_document
from forge.respec import _merge_criteria
from forge.loop import Orchestrator, StepResult
from forge.patch import (
    duplicate_paths,
    enforce_scope,
    is_safe_path,
    matches_any,
    normalize_path,
    parse_output,
)
from forge.failures import distill, signatures
from forge.prompts import (
    build_prompt,
    parse_respec,
    parse_verdict,
    respec_prompt,
    review_prompt,
    tests_prompt,
)
from forge.providers.base import (
    Capabilities,
    Completion,
    Message,
    Provider,
    ProviderBadResponse,
    ProviderUnreachable,
    Usage,
)
from forge.providers.openai_compat import OpenAICompatProvider
from forge.providers.claude_cli import (
    _LIMIT_PATTERN,
    _SPEND_LIMIT_PATTERN,
    parse_reset_time,
)
from forge.state import (
    TICKET_DONE,
    TICKET_PENDING,
    TICKET_FAILED,
    TICKET_SKIPPED,
    Store,
    Ticket,
)
from forge.ui import server as ui_server
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

    def test_doubled_fence_does_not_leak_into_the_file(self):
        # A real run wrote ```rust as line 2 of three .rs files, which broke
        # `cargo clippy --all-targets` for every later ticket in the repo.
        fence = "`" * 3
        body = f"tests/x.rs\n{fence}\n{fence}rust\nuse std::fs;\n{fence}\n{fence}\n"
        content = parse_output(body).edits[0].content
        self.assertNotIn(fence, content)
        self.assertEqual(content.strip(), "use std::fs;")

    def test_a_normal_single_fence_is_untouched(self):
        fence = "`" * 3
        content = parse_output(f"a.rs\n{fence}rust\nfn main() {{}}\n{fence}\n").edits[0].content
        self.assertEqual(content, "fn main() {}\n")

    def test_a_longer_fence_carries_a_file_that_contains_fences(self):
        # A README wrapped in three backticks ends at the first fence inside
        # the README. The file is written truncated and its remaining prose is
        # re-parsed as more files — which is how a working build.sh was
        # silently replaced by a markdown fragment.
        inner, outer = "`" * 3, "`" * 4
        readme = f"# Title\n\n{inner}sh\n./build.sh\n{inner}\n\n## More\n\ndone\n"
        parsed = parse_output(f"README.md\n{outer}md\n{readme}{outer}\n")

        self.assertEqual([e.path for e in parsed.edits], ["README.md"])
        self.assertEqual(parsed.edits[0].content, readme)

    def test_a_short_fence_around_fenced_content_is_caught_as_a_duplicate(self):
        # Verbatim shape of the response that broke TT-006: the README closes
        # its own fence early, and its remaining prose — a path on its own line
        # ahead of a fence — parses as one more file. The parse cannot be
        # salvaged, but it must not reach disk: the spurious block is last, so
        # apply_edits lets it win over the real build.sh.
        f = "`" * 3
        body = (
            f"build.sh\n{f}sh\ncargo build --release\n{f}\n\n"
            f"README.md\n{f}\n"
            "# Tetris\n\n"
            f"{f}sh\nrustup target add wasm32-unknown-unknown\n{f}\n\n"
            "Then build with one of the provided scripts:\n\n"
            "### POSIX shell\n\n"
            f"{f}sh\n./build.sh\n{f}\n\n"
            "### PowerShell\n\n"
            f"{f}powershell\n.\\build.ps1\n{f}\n"
            f"{f}\n"
        )
        parsed = parse_output(body)

        self.assertEqual([e.path for e in parsed.edits], ["build.sh", "README.md", "./build.sh"])
        self.assertEqual(duplicate_paths(parsed), ["build.sh"])
        # The corrupting block is the later one, which is why last-write-wins
        # turned a working script into a fragment of markdown.
        self.assertIn("### PowerShell", parsed.edits[-1].content)

    def test_distinct_paths_are_not_duplicates(self):
        fence = "`" * 3
        parsed = parse_output(f"a.rs\n{fence}\nx\n{fence}\n\nb.rs\n{fence}\ny\n{fence}\n")
        self.assertEqual(duplicate_paths(parsed), [])

    def test_duplicate_detection_sees_through_path_spelling(self):
        fence = "`" * 3
        parsed = parse_output(f"build.sh\n{fence}\nx\n{fence}\n\n./build.sh\n{fence}\ny\n{fence}\n")
        self.assertEqual(duplicate_paths(parsed), ["build.sh"])


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

    def test_dot_slash_prefix_is_not_out_of_scope(self):
        # A real ticket listed `build.sh` and had its own `./build.sh` edit
        # rejected on every attempt, so it could never finish.
        self.assertTrue(matches_any("./build.sh", ["build.sh"]))
        self.assertTrue(matches_any(".\\build.ps1", ["build.ps1"]))
        self.assertTrue(matches_any("./src/game.rs", ["src/game.rs"]))

    def test_normalizing_does_not_widen_scope(self):
        # Stripping a leading slash would let /etc/passwd match `etc/*`.
        self.assertEqual(normalize_path("/etc/passwd"), "/etc/passwd")
        self.assertFalse(matches_any("/etc/passwd", ["etc/*"]))
        self.assertFalse(matches_any("./src/other.rs", ["src/game.rs"]))

    def test_never_delegate_still_catches_the_dot_slash_form(self):
        parsed = parse_output("./src/auth/login.py\n```\nx\n```")
        scoped = enforce_scope(parsed, ["./src/auth/login.py"], ["src/auth/**"])
        self.assertFalse(scoped.edits)
        self.assertIn("neverDelegate", scoped.rejected[0])

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


class TestLimitDetection(unittest.TestCase):
    """A limit the parser misses costs the ticket its whole retry budget."""

    # The exact sentence the CLI emitted when a real run stalled.
    SPEND = (
        "You've hit your monthly spend limit "
        "· raise it at claude.ai/settings/usage?from=cc_cli_limit_message"
    )

    def test_monthly_spend_limit_is_a_limit(self):
        self.assertTrue(_LIMIT_PATTERN.search(self.SPEND))

    def test_monthly_spend_limit_is_classified_as_spend(self):
        # Spend limits do not lift on a clock, so they must not share the
        # short retry cadence used for rolling usage windows.
        self.assertTrue(_SPEND_LIMIT_PATTERN.search(self.SPEND))

    def test_rolling_usage_limit_is_not_a_spend_limit(self):
        text = "Claude usage limit reached. Your limit will reset at 9am."
        self.assertTrue(_LIMIT_PATTERN.search(text))
        self.assertIsNone(_SPEND_LIMIT_PATTERN.search(text))

    def test_ordinary_failure_is_not_a_limit(self):
        self.assertIsNone(_LIMIT_PATTERN.search("error: file not found"))


class TestClaudeCliUsage(unittest.TestCase):
    """Cache counters carry nearly all the input on a CLI-backed call."""

    def test_cache_tokens_and_cost_are_recorded(self):
        usage = Usage(
            prompt_tokens=2,
            completion_tokens=4,
            cache_creation_tokens=26326,
            cache_read_tokens=100_000,
            cost_usd=0.26337,
        )
        # Reading prompt_tokens alone would report 2 tokens for a call that
        # actually moved six figures.
        self.assertEqual(usage.input_tokens, 126_328)
        self.assertEqual(usage.total_tokens, 126_332)
        self.assertAlmostEqual(usage.cost_usd, 0.26337)


class TestBudgetGate(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "t.db")

    def test_proactive_window_limit(self):
        gate = BudgetGate(
            self.store, {"m": RateLimitPolicy(tokens_per_window=1000, window_seconds=18000)}
        )
        self.assertIsNone(gate.check_rate_limit("m"))
        gate.record("m", Usage(prompt_tokens=900, completion_tokens=200))
        self.assertIsNotNone(gate.check_rate_limit("m"))

    def test_window_limit_counts_cache_tokens(self):
        # A cache-heavy call reports almost nothing as `prompt_tokens`. If the
        # window only summed that, the gate would never fire on the traffic
        # that actually consumes the allowance.
        gate = BudgetGate(
            self.store, {"m": RateLimitPolicy(tokens_per_window=1000, window_seconds=18000)}
        )
        gate.record(
            "m",
            Usage(prompt_tokens=2, completion_tokens=4, cache_read_tokens=1500),
        )
        self.assertIsNotNone(gate.check_rate_limit("m"))

    def test_cost_window_limit(self):
        # The spend limit that stalled a real run was a dollar cap, so the
        # gate has to be able to park on dollars rather than only on tokens.
        gate = BudgetGate(
            self.store, {"m": RateLimitPolicy(cost_per_window=1.0, window_seconds=18000)}
        )
        self.assertIsNone(gate.check_rate_limit("m"))
        gate.record("m", Usage(completion_tokens=4, cost_usd=0.75))
        self.assertIsNone(gate.check_rate_limit("m"))
        gate.record("m", Usage(completion_tokens=4, cost_usd=0.30))
        self.assertIsNotNone(gate.check_rate_limit("m"))

    def test_park_and_clear(self):
        gate = BudgetGate(self.store, {})
        gate.park("m", time.time() + 60)
        self.assertIsNotNone(gate.check_rate_limit("m"))
        # A successful call proves the window reopened.
        gate.record("m", Usage(prompt_tokens=1, completion_tokens=1))
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


class TestRetry(unittest.TestCase):
    """A blocked run must be continuable, not only re-ingestible."""

    def _store_with_exhausted_run(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            [
                Ticket("T-1", status="done", attempts=1, position=0),
                Ticket("T-2", status="failed", attempts=3, position=1),
                Ticket("T-3", status="blocked", attempts=2, position=2),
            ],
        )
        return store, run_id

    def test_exhausted_run_has_no_next_ticket_before_a_retry(self):
        # The dead end this command exists to open: the run is still selectable
        # but the loop finds nothing to do and re-declares it exhausted.
        store, run_id = self._store_with_exhausted_run()
        self.assertIsNone(store.next_ticket(run_id))

    def test_retry_requeues_only_unfinished_work(self):
        store, run_id = self._store_with_exhausted_run()
        reset = store.reset_tickets(run_id)

        self.assertEqual([t.ticket_id for t in reset], ["T-2", "T-3"])
        by_id = {t.ticket_id: t for t in store.list_tickets(run_id)}
        self.assertEqual(by_id["T-1"].status, "done")
        self.assertEqual(by_id["T-2"].status, "pending")
        self.assertEqual(store.next_ticket(run_id).ticket_id, "T-2")

    def test_retry_restores_a_full_attempt_budget(self):
        store, run_id = self._store_with_exhausted_run()
        store.reset_tickets(run_id)
        # Without this the loop's `attempts < max_attempts` guard is already
        # false and the retried ticket fails again without a single call.
        self.assertEqual(store.list_tickets(run_id)[1].attempts, 0)

    def test_retry_does_not_overwrite_the_failed_attempt_artifacts(self):
        store, run_id = self._store_with_exhausted_run()
        store.reset_tickets(run_id)
        ticket = {t.ticket_id: t for t in store.list_tickets(run_id)}["T-2"]

        # T-2 already wrote attempt-1..3; the next cycle must land on attempt-4
        # or the evidence explaining the failure is destroyed by the retry.
        self.assertEqual(ticket.attempt_base, 3)
        ticket.attempts = 1
        self.assertEqual(ticket.attempt_number, 4)

    def test_retry_clears_the_stale_blocked_note(self):
        store, run_id = self._store_with_exhausted_run()
        ticket = store.list_tickets(run_id)[1]
        ticket.blocked_note = "exhausted 3 attempts"
        store.update_ticket(run_id, ticket)

        store.reset_tickets(run_id)
        self.assertEqual(store.list_tickets(run_id)[1].blocked_note, "")

    def test_named_ticket_is_retried_even_when_it_succeeded(self):
        store, run_id = self._store_with_exhausted_run()
        reset = store.reset_tickets(run_id, ticket_ids=["T-1"])
        self.assertEqual([t.ticket_id for t in reset], ["T-1"])
        self.assertEqual(store.list_tickets(run_id)[0].status, "pending")

    def test_retry_is_a_no_op_when_nothing_is_exhausted(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", status="done", attempts=1)])
        self.assertEqual(store.reset_tickets(run_id), [])

    def test_attempt_base_survives_repeated_retries(self):
        store, run_id = self._store_with_exhausted_run()
        for spent in (3, 2):
            store.reset_tickets(run_id, ticket_ids=["T-2"])
            ticket = {t.ticket_id: t for t in store.list_tickets(run_id)}["T-2"]
            ticket.attempts = spent
            ticket.status = "failed"
            store.update_ticket(run_id, ticket)

        store.reset_tickets(run_id, ticket_ids=["T-2"])
        # 3 from the original run, then 3 and 2 from the two retry cycles.
        self.assertEqual(
            {t.ticket_id: t for t in store.list_tickets(run_id)}["T-2"].attempt_base, 8
        )


class TestRespec(unittest.TestCase):
    """A retry that re-runs the spec that already failed is just a slower failure."""

    def _store_with_a_rejected_ticket(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", status="failed", attempts=3)])
        for detail in ("REJECT first", "REJECT second", "REJECT third"):
            step = store.start_step(run_id, "T-1", "review")
            store.end_step(step, "failed", detail)
        return store, run_id

    def test_failures_are_recoverable_after_the_ticket_is_given_up_on(self):
        store, run_id = self._store_with_a_rejected_ticket()
        failures = store.ticket_failures(run_id, "T-1")
        # Oldest first: a rejection that recurs is the signal that the spec,
        # not the implementation, is what needs changing.
        self.assertEqual([f["detail"] for f in failures], ["REJECT first", "REJECT second", "REJECT third"])

    def test_failures_survive_the_requeue(self):
        store, run_id = self._store_with_a_rejected_ticket()
        store.reset_tickets(run_id)
        self.assertEqual(len(store.ticket_failures(run_id, "T-1")), 3)

    def test_only_failed_steps_count_as_evidence(self):
        store, run_id = self._store_with_a_rejected_ticket()
        step = store.start_step(run_id, "T-1", "build")
        store.end_step(step, "ok", "this went fine")
        details = [f["detail"] for f in store.ticket_failures(run_id, "T-1")]
        self.assertNotIn("this went fine", details)

    def test_prompt_carries_the_spec_and_every_failure(self):
        ticket = Ticket("T-1", title="Shell", spec="Build a shell", criteria=["works"])
        failures = [
            {"name": "review", "detail": "REJECT wrong key code"},
            {"name": "verify-test", "detail": "2 tests failed"},
        ]
        body = respec_prompt(ticket, failures)[1].content
        self.assertIn("Build a shell", body)
        self.assertIn("REJECT wrong key code", body)
        self.assertIn("2 tests failed", body)

    def test_parses_a_fenced_reply(self):
        revision = parse_respec(
            '```json\n{"rationale": "scope too narrow", "spec": "new spec",\n'
            ' "criteria": ["a"], "allowed_files": ["web/main.js"]}\n```'
        )
        self.assertEqual(revision["spec"], "new spec")
        self.assertEqual(revision["allowed_files"], ["web/main.js"])
        self.assertEqual(revision["rationale"], "scope too narrow")

    def test_omitted_fields_are_absent_rather_than_blank(self):
        # A reply that drops allowed_files must leave the existing scope
        # alone; treating it as [] would narrow the ticket to nothing.
        revision = parse_respec('{"spec": "only the spec changed"}')
        self.assertEqual(set(revision), {"spec"})

    def test_empty_list_is_treated_as_a_truncated_reply(self):
        revision = parse_respec('{"spec": "s", "criteria": [], "allowed_files": []}')
        self.assertNotIn("criteria", revision)
        self.assertNotIn("allowed_files", revision)

    def test_a_reply_with_no_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_respec('{"rationale": "looks fine to me"}')

    def test_unparseable_reply_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_respec("I think the ticket is fine, actually.")


class TestAutomaticRetryCycles(unittest.TestCase):
    """A backlog that ends blocked at 2am does nothing until somebody wakes up.

    `loop.retryCycles` is `forge retry --respec` run by the loop itself. The
    risk it carries is the opposite of the one it fixes — a run that never
    stops — so what is tested here is mostly the brakes: a persisted count, and
    a cycle with nothing to requeue ending the run instead of spinning.
    """

    def _orchestrator(self, tickets=None, **loop_settings):
        root = Path(tempfile.mkdtemp())
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
            # Off unless a test asks for it: respec is a model call, and most of
            # these tests are about the cycle counting around it.
            loop=LoopSettings(**{"respec_on_retry": False, **loop_settings}),
        )
        # The real location, so a test can hand the same run to the CLI.
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            tickets or [Ticket("T-1", position=0), Ticket("T-2", position=1)],
        )
        return Orchestrator(config, store), store, run_id

    @staticmethod
    def _script(orchestrator, passes_after: int = 99) -> list[str]:
        """Stand in for the whole per-ticket state machine.

        Returns the log of tickets worked, so a test can count how many times
        the backlog was run rather than how many cycles were recorded.
        """
        worked: list[str] = []

        def work(run_id: int, ticket: Ticket) -> None:
            worked.append(ticket.ticket_id)
            seen = worked.count(ticket.ticket_id)
            ticket.status = "done" if seen > passes_after else "failed"
            ticket.blocked_note = "" if ticket.status == "done" else "exhausted 3 attempts"
            orchestrator.store.update_ticket(run_id, ticket)

        orchestrator._work_ticket = work
        return worked

    def test_a_blocked_backlog_is_left_for_a_human_by_default(self):
        orchestrator, store, run_id = self._orchestrator()
        worked = self._script(orchestrator)

        self.assertEqual(orchestrator.run(run_id), "blocked")
        self.assertEqual(worked, ["T-1", "T-2"])
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "0")

    def test_the_backlog_is_requeued_the_configured_number_of_times(self):
        orchestrator, store, run_id = self._orchestrator(retry_cycles=2)
        worked = self._script(orchestrator)

        self.assertEqual(orchestrator.run(run_id), "blocked")
        # The first pass, then two retries: every ticket seen three times.
        self.assertEqual(len(worked), 6)
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "2")

    def test_minus_one_keeps_going_until_the_backlog_is_clean(self):
        orchestrator, store, run_id = self._orchestrator(retry_cycles=-1)
        worked = self._script(orchestrator, passes_after=2)

        self.assertEqual(orchestrator.run(run_id), "done")
        self.assertEqual(len(worked), 6)
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "2")

    def test_a_retry_restores_the_attempt_budget_it_spent(self):
        orchestrator, store, run_id = self._orchestrator(retry_cycles=1)
        self._script(orchestrator)
        orchestrator.run(run_id)

        # Requeued through the same path `forge retry` uses, so the next cycle
        # starts with a full budget and lands its artifacts in fresh
        # directories rather than on top of the failed ones.
        ticket = store.list_tickets(run_id)[0]
        self.assertEqual(ticket.attempts, 0)

    def test_a_failure_no_ticket_owns_ends_the_run_instead_of_spinning(self):
        # Every ticket landed and the run is still not done: the final verify
        # step is failing on something outside this backlog. A retry would
        # requeue nothing and arrive straight back here, forever.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="done")], retry_cycles=-1
        )
        orchestrator._shell = lambda _run, _name, _cmd: StepResult(ok=False, detail="boom")

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "0")

    def test_claude_only_tickets_are_not_requeued_forever(self):
        # Triage is a hard gate: a requeued claude-only ticket is skipped
        # again, so under -1 it is a cycle that repeats forever while doing
        # nothing but spending a planner call on each pass.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", route="claude-only", status="skipped")],
            retry_cycles=-1,
        )
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)
        self.assertEqual(store.list_tickets(run_id)[0].status, "skipped")

    def test_a_cycle_that_repeats_the_last_one_exactly_ends_the_run(self):
        # The brake a count cannot provide. One ticket rewriting identical code
        # and collecting an identical rejection ran 37 attempts across a dozen
        # cycles before a human noticed.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="failed", attempts=3)], retry_cycles=-1
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the implementation is missing")

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

        # The cycle runs and fails in precisely the same way.
        ticket = store.list_tickets(run_id)[0]
        ticket.status = "failed"
        store.update_ticket(run_id, ticket)
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the implementation is missing")

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)

    def test_a_cycle_that_fails_in_a_new_way_keeps_going(self):
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="failed", attempts=3)], retry_cycles=-1
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the implementation is missing")
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

        ticket = store.list_tickets(run_id)[0]
        ticket.status = "failed"
        store.update_ticket(run_id, ticket)
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the RNG seed handling is wrong")

        # Different objection: the respec has something new to work from, so
        # stopping here would end a run that is still making progress.
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

    def test_a_respec_that_changed_nothing_ends_the_run(self):
        # "planner kept the ticket as written", cycle after cycle: the next
        # cycle hands the executor the identical ticket that has already
        # failed, and the only thing left varying is model sampling.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", spec="s", status="failed", attempts=3)],
            retry_cycles=-1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the reviewer disagrees with the executor")
        orchestrator._call = lambda *_a, **_k: Completion(
            text=json.dumps({"spec": "s", "rationale": "the ticket is already right"}),
            usage=Usage(),
        )

        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)
        # Not requeued: the ticket is left exactly as the run ended it.
        self.assertEqual(store.list_tickets(run_id)[0].status, "failed")

    def test_the_planner_rationale_reaches_the_log(self):
        # Without it the operator reads "kept the ticket as written" and cannot
        # tell "the spec is fine, the executor did not finish" from "I could
        # not work out what to change".
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", spec="s", status="failed", attempts=3)],
            retry_cycles=1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        orchestrator._call = lambda *_a, **_k: Completion(
            text=json.dumps(
                {"spec": "s", "rationale": "the failures show unfinished work, not a bad spec"}
            ),
            usage=Usage(),
        )

        orchestrator._retry_cycle(run_id, "blocked")

        messages = [r["message"] for r in store.events_after(0)]
        self.assertTrue(
            any("unfinished work, not a bad spec" in message for message in messages)
        )

    def test_an_unreachable_planner_does_not_become_a_plain_retry_loop(self):
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="failed", attempts=3)],
            retry_cycles=-1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        orchestrator._output_budget = unittest.mock.Mock(
            side_effect=ConfigError("no planner")
        )

        # respecOnRetry was asked for and cannot happen; requeueing anyway
        # would spin the backlog with nothing changing between cycles.
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)

    def test_the_spent_cycles_survive_a_restart(self):
        # In memory, a killed daemon would hand the resumed run a fresh budget
        # every time — which is how retryCycles: 2 becomes unbounded.
        orchestrator, store, run_id = self._orchestrator(retry_cycles=2)
        self._script(orchestrator)
        orchestrator.run(run_id)

        resumed = Orchestrator(orchestrator.config, store)
        self._script(resumed)
        self.assertIs(resumed._retry_cycle(run_id, "blocked"), False)

    def test_a_manual_retry_restores_the_automatic_budget(self):
        orchestrator, store, run_id = self._orchestrator(retry_cycles=1)
        self._script(orchestrator)
        orchestrator.run(run_id)
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "1")

        orchestrator.config.write()
        cli.cmd_retry(
            argparse.Namespace(
                root=str(orchestrator.config.root),
                run=run_id,
                ticket=[],
                all=False,
                respec=False,
                go=False,
                no_ui=True,
                retries=None,
            )
        )
        # The human just replaced the situation the automatic cycles gave up
        # on; the next `forge go` gets its full budget against the new one.
        self.assertEqual(store.get_control(f"retries:{run_id}", "0"), "0")

    def test_each_requeued_ticket_is_respecced_before_the_next_cycle(self):
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="failed", spec="old spec", attempts=3)],
            retry_cycles=1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the spec never said which file")

        asked: list[str] = []

        def call(_run_id, role, _messages, **_kwargs):
            asked.append(role)
            return Completion(
                text=json.dumps({"spec": "new spec", "rationale": "named the file"}),
                usage=Usage(),
            )

        orchestrator._call = call
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

        self.assertEqual(asked, ["planner"])
        self.assertEqual(store.list_tickets(run_id)[0].spec, "new spec")
        # The tickets on disk are what a human reads; a revision that lives
        # only in the database makes those files lie.
        written = (orchestrator.config.tickets_dir / "T-1.md").read_text(encoding="utf-8")
        self.assertIn("new spec", written)

    def test_only_the_revised_tickets_are_rewritten(self):
        # Rewriting the whole backlog reported "6 ticket file(s)" for a single
        # revision, which reads as respec having touched work it never looked
        # at. The count has to be the revisions.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[
                Ticket("T-1", status="failed", spec="old spec", attempts=3),
                Ticket("T-2", status="failed", spec="fine as written", attempts=3),
            ],
            retry_cycles=1,
            respec_on_retry=True,
        )
        for ticket_id in ("T-1", "T-2"):
            step = store.start_step(run_id, ticket_id, "review")
            store.end_step(step, "failed", "REJECT: something")

        def call(_run_id, _role, messages, **_kwargs):
            asked = "\n".join(m.content for m in messages)
            # The planner is told to say so when the spec was not the problem.
            spec = "new spec" if "T-1" in asked else "fine as written"
            return Completion(text=json.dumps({"spec": spec}), usage=Usage())

        orchestrator._call = call
        orchestrator._retry_cycle(run_id, "blocked")

        written = sorted(p.name for p in orchestrator.config.tickets_dir.glob("*.md"))
        self.assertEqual(written, ["T-1.md"])

    def test_a_respec_that_could_not_run_stops_rather_than_retrying_blind(self):
        # respecOnRetry was asked for and did not happen, so the cycle would be
        # a plain re-run of a ticket that already failed. The run stops with
        # the ticket exactly as it was, for a human to look at.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[Ticket("T-1", status="failed", spec="old spec", attempts=3)],
            retry_cycles=1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")

        def unreachable(*_args, **_kwargs):
            raise ProviderUnreachable("connection refused")

        orchestrator._call = unreachable
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)

        ticket = store.list_tickets(run_id)[0]
        self.assertEqual(ticket.status, "failed")
        self.assertEqual(ticket.spec, "old spec")


class TestRetryCycleConfig(unittest.TestCase):
    """The knob is read from config, and a typo in it must not run forever."""

    def _load(self, loop_block: dict) -> Config:
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"m": {"kind": "openai", "model": "x"}},
                    "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                    "loop": loop_block,
                }
            ),
            encoding="utf-8",
        )
        return Config.load(root)

    def test_the_default_hands_blocked_work_back_to_a_human(self):
        config = self._load({})
        self.assertEqual(config.loop.retry_cycles, 0)
        self.assertTrue(config.loop.respec_on_retry)

    def test_both_knobs_are_read(self):
        config = self._load({"retryCycles": -1, "respecOnRetry": False})
        self.assertEqual(config.loop.retry_cycles, -1)
        self.assertFalse(config.loop.respec_on_retry)

    def test_a_negative_number_that_is_not_minus_one_is_rejected(self):
        # Guessing which one it meant either burns tokens forever or silently
        # does nothing, and neither is recoverable from the config file.
        with self.assertRaises(ConfigError):
            self._load({"retryCycles": -2})

    def test_the_setting_survives_a_write(self):
        config = self._load({"retryCycles": 3, "respecOnRetry": False})
        config.write()
        reloaded = Config.load(config.root)
        self.assertEqual(reloaded.loop.retry_cycles, 3)
        self.assertFalse(reloaded.loop.respec_on_retry)


class TestRespecHasGroundTruth(unittest.TestCase):
    """Respec used to rewrite a spec knowing only the ticket and the failures.

    So it wrote "SoftDrop decrements y" about an implementation that increments
    it, invented an acceptance criterion no derivation supported, and — because
    every revision was derived from the previous revision — drifted until the
    ticket asserted the opposite of what its author had written. Three holes:
    it could not see the code, could not see the original, and could rewrite
    the standard it was being judged against.
    """

    def _ticket(self, **kwargs) -> Ticket:
        base = dict(
            ticket_id="TT-003",
            title="Game rules",
            spec="Implement Game::tick",
            criteria=["tick(2000) moves the piece down at least two rows"],
            allowed_files=["src/game.rs"],
        )
        return Ticket(**{**base, **kwargs})

    FAILURES = [{"name": "review", "detail": "REJECT: the implementation is missing"}]

    def test_the_planner_is_shown_the_code_it_is_writing_about(self):
        body = respec_prompt(
            self._ticket(),
            self.FAILURES,
            sources={"src/game.rs": "pub fn tick(&mut self) { self.y += 1; }"},
        )[-1].content

        self.assertIn("self.y += 1", body)
        self.assertIn("must be checked against them", body)

    def test_the_original_is_shown_once_the_ticket_has_drifted(self):
        body = respec_prompt(
            self._ticket(
                spec="revision number nine",
                original_spec="what the human actually wrote",
                original_criteria=["the criterion the plan stated"],
            ),
            self.FAILURES,
        )[-1].content

        self.assertIn("what the human actually wrote", body)
        self.assertIn("the criterion the plan stated", body)
        self.assertIn("drift you", body)

    def test_an_undrifted_ticket_carries_no_original_section(self):
        # First respec of a ticket: the current text *is* the original, and
        # printing it twice spends budget to say nothing.
        body = respec_prompt(
            self._ticket(original_spec="Implement Game::tick"), self.FAILURES
        )[-1].content
        self.assertNotIn("when the plan was ingested", body)

    def test_each_criterion_is_marked_with_who_wrote_it(self):
        body = respec_prompt(
            self._ticket(
                criteria=["the plan's bar", "invented by an earlier respec"],
                original_criteria=["the plan's bar"],
            ),
            self.FAILURES,
        )[-1].content

        section = body[body.index("What you may do to the acceptance criteria") :]
        plan_line = section.index("the plan's bar")
        added_line = section.index("invented by an earlier respec")
        self.assertIn("you may not change this", section[plan_line:added_line])
        self.assertIn("you may revise or retire it", section[added_line:])

    def test_the_impossible_escape_route_is_offered(self):
        body = respec_prompt(self._ticket(), self.FAILURES)[-1].content
        self.assertIn("cannot be satisfied at all", body)
        self.assertIn("impossible", body)

    def test_unlocking_the_criteria_removes_the_rules(self):
        body = respec_prompt(self._ticket(), self.FAILURES, criteria_locked=False)[
            -1
        ].content
        self.assertNotIn("What you may do to the acceptance criteria", body)


class TestCriteriaAreScopedByProvenance(unittest.TestCase):
    """Who wrote a criterion decides who may change it.

    A blanket freeze made a machine-invented criterion as immutable as a
    human's, so the loop could mint one no implementation could satisfy and
    then never retire it — and rewrote the spec around it instead. No freeze at
    all let the failing party edit the standard until it asserted the opposite
    of what the plan said. Both failures happened, in that order.
    """

    def _store(self, criteria=("the plan's bar",), added=()):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec="old", criteria=list(criteria), status="failed")]
        )
        if added:
            # As if an earlier revision had added them.
            ticket = store.list_tickets(run_id)[0]
            ticket.criteria = list(criteria) + list(added)
            store.update_ticket(run_id, ticket)
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _reply(self, **payload):
        def call(_messages, _budget):
            return Completion(text=json.dumps(payload), usage=Usage())

        return call

    def test_a_plan_criterion_the_planner_dropped_is_put_back(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        result = respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="new", criteria=["a bar this ticket can clear"]),
            budget=1024,
        )

        self.assertIn("the plan's bar", store.list_tickets(run_id)[0].criteria)
        self.assertEqual(result.refused_criteria, ["the plan's bar"])

    def test_the_restoration_is_surfaced_rather_than_silent(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="new", criteria=["something easier"]),
            budget=1024,
        )

        logged = [r["message"] for r in store.events_after(0) if "put back" in r["message"]]
        self.assertTrue(logged, "the restoration must reach the run log")

    def test_a_new_criterion_is_accepted(self):
        # The genuine gap: a plan that specifies scoring in the spec and states
        # no criterion for it. Adding one cannot lower the bar.
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(
                spec="new", criteria=["the plan's bar", "clearing one line scores 100"]
            ),
            budget=1024,
        )

        self.assertEqual(
            store.list_tickets(run_id)[0].criteria,
            ["the plan's bar", "clearing one line scores 100"],
        )

    def test_a_criterion_an_earlier_revision_added_can_be_retired(self):
        # The trap the blanket freeze created: `[6, 3, 5, 7, 4]` was invented by
        # a respec, was impossible, and could never be taken back.
        store, run_id = self._store(added=["Game::new(1) yields [6, 3, 5, 7, 4]"])
        ticket = store.list_tickets(run_id)[0]

        result = respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="new", criteria=["the plan's bar"]),
            budget=1024,
        )

        self.assertEqual(store.list_tickets(run_id)[0].criteria, ["the plan's bar"])
        self.assertEqual(result.refused_criteria, [])

    def test_a_plan_criterion_a_human_already_removed_is_not_resurrected(self):
        # Protecting the contract must not mean overruling the human who
        # edited it. The anchor holds a criterion the ticket no longer has.
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        ticket.criteria = ["a criterion the human wrote by hand"]
        store.update_ticket(run_id, ticket)

        result = respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(
                spec="new",
                criteria=["a criterion the human wrote by hand", "and one addition"],
            ),
            budget=1024,
        )

        self.assertEqual(
            store.list_tickets(run_id)[0].criteria,
            ["a criterion the human wrote by hand", "and one addition"],
        )
        self.assertEqual(result.refused_criteria, [])

    def test_a_run_with_no_anchor_treats_every_criterion_as_the_plans(self):
        # Ingested before originals were recorded: provenance is unknown, so
        # leaving a human's contract alone is the safe direction to be wrong in.
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        ticket.original_criteria = []

        respec.revise(
            store, run_id, ticket, call=self._reply(spec="new", criteria=["easier"]), budget=1024
        )

        self.assertIn("the plan's bar", store.list_tickets(run_id)[0].criteria)

    def test_unlocking_lets_the_planner_replace_them_outright(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="new", criteria=["a deliberate replacement"]),
            budget=1024,
            criteria_locked=False,
        )

        self.assertEqual(
            store.list_tickets(run_id)[0].criteria, ["a deliberate replacement"]
        )

    def test_omitting_criteria_leaves_them_alone(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        result = respec.revise(
            store, run_id, ticket, call=self._reply(spec="new"), budget=1024
        )

        self.assertEqual(store.list_tickets(run_id)[0].criteria, ["the plan's bar"])
        self.assertEqual(result.changed, ["spec"])


class TestAnImpossibleTicketParksInsteadOfRetrying(unittest.TestCase):
    """The planner found `[6, 3, 5, 7, 4]` unreachable, wrote that discovery
    into `context` — which the executor reads as fact — and changed an xorshift
    constant to chase it anyway. Being unable to say "this cannot be done" is
    what made rewriting the spec the only available move."""

    def _store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec="old", criteria=["yields [6,3,5,7,4]"])]
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: sequence mismatch")
        return store, run_id

    IMPOSSIBLE = "No xorshift32 with these shifts yields that sequence; seed 1 gives [2, ...]"

    def _reply(self, **payload):
        def call(_messages, _budget):
            return Completion(text=json.dumps(payload), usage=Usage())

        return call

    def test_a_report_of_impossibility_is_a_complete_reply(self):
        # No revised spec, and that is the point: there is no spec that
        # satisfies a contradiction.
        revision = parse_respec(json.dumps({"impossible": self.IMPOSSIBLE}))
        self.assertEqual(revision["impossible"], self.IMPOSSIBLE)

    def test_nothing_is_applied_when_the_ticket_cannot_be_satisfied(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        result = respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="a spec bent around the contradiction",
                             impossible=self.IMPOSSIBLE),
            budget=1024,
        )

        self.assertEqual(result.impossible, self.IMPOSSIBLE)
        self.assertFalse(result.revised)
        self.assertEqual(store.list_tickets(run_id)[0].spec, "old")

    def test_the_loop_parks_the_ticket_rather_than_retrying_it(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        ticket.status = "failed"
        store.update_ticket(run_id, ticket)

        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": ""},
            loop=LoopSettings(retry_cycles=1, respec_on_retry=True),
        )
        orchestrator = Orchestrator(config, store)
        orchestrator._call = lambda *_a, **_k: Completion(
            text=json.dumps({"impossible": self.IMPOSSIBLE}), usage=Usage()
        )

        orchestrator._retry_cycle(run_id, "blocked")

        parked = store.list_tickets(run_id)[0]
        self.assertEqual(parked.status, "blocked")
        self.assertIn("xorshift32", parked.blocked_note)
        self.assertIsNone(store.next_ticket(run_id))


class TestTheOriginalTicketIsAnAnchor(unittest.TestCase):
    """Every revision is derived from the last one. Without the ingested text
    kept somewhere no caller can write, the tenth revision has no relationship
    left to what a human asked for."""

    def _store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec="as ingested", criteria=["as ingested too"])]
        )
        return store, run_id

    def test_ingest_captures_the_original(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        self.assertEqual(ticket.original_spec, "as ingested")
        self.assertEqual(ticket.original_criteria, ["as ingested too"])

    def test_a_revision_cannot_move_the_anchor(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        ticket.spec = "revision one"
        ticket.criteria = ["revised"]
        ticket.original_spec = "a rewritten history"
        store.update_ticket(run_id, ticket)

        stored = store.list_tickets(run_id)[0]
        self.assertEqual(stored.spec, "revision one")
        self.assertEqual(stored.original_spec, "as ingested")

    def test_drift_is_detectable(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        self.assertFalse(ticket.drifted)

        ticket.spec = "revision one"
        self.assertTrue(ticket.drifted)

    def test_a_database_from_before_the_column_still_opens(self):
        # Older runs must keep working: the column is added by migration, and
        # tickets ingested before it have no anchor to report.
        path = Path(tempfile.mkdtemp()) / "old.db"
        store = Store(path)
        store._connection.execute("ALTER TABLE tickets DROP COLUMN original_spec")
        store._connection.commit()
        store.close()

        reopened = Store(path)
        run_id = reopened.create_run("goal")
        reopened.add_tickets(run_id, [Ticket("T-1", spec="s")])
        self.assertEqual(reopened.list_tickets(run_id)[0].original_spec, "s")


class TestTheDashboardOutlivesTheRun(unittest.TestCase):
    """The dashboard dies with the process, and the run worth reading is the
    one that just ended. Exiting the moment the loop stops takes the event
    stream away at exactly the moment it becomes interesting."""

    def test_a_watched_run_holds_the_dashboard_open(self):
        with unittest.mock.patch("forge.wizard.interactive", return_value=True):
            self.assertTrue(cli._should_wait(argparse.Namespace(wait=None)))

    def test_an_unwatched_run_still_exits(self):
        # A scheduled task or a CI step must return on its own; a daemon that
        # silently never exits is worse than a dashboard you restart.
        with unittest.mock.patch("forge.wizard.interactive", return_value=False):
            self.assertFalse(cli._should_wait(argparse.Namespace(wait=None)))

    def test_the_flags_beat_the_guess_in_both_directions(self):
        with unittest.mock.patch("forge.wizard.interactive", return_value=False):
            self.assertTrue(cli._should_wait(argparse.Namespace(wait=True)))
        with unittest.mock.patch("forge.wizard.interactive", return_value=True):
            self.assertFalse(cli._should_wait(argparse.Namespace(wait=False)))

    def test_a_namespace_without_the_flag_falls_back_to_the_guess(self):
        # `forge retry --go` builds its own namespace.
        with unittest.mock.patch("forge.wizard.interactive", return_value=True):
            self.assertTrue(cli._should_wait(argparse.Namespace()))


class TestAClosedTabIsNotAnError(unittest.TestCase):
    """Closing or refreshing the dashboard tears the socket down under whatever
    write is in flight. `socketserver` answers that by printing a traceback
    into the middle of the run's output, where it reads as the loop crashing."""

    def _handler(self, raises: Exception | None):
        handler = ui_server.Handler.__new__(ui_server.Handler)
        handler.close_connection = False
        with unittest.mock.patch.object(
            BaseHTTPRequestHandler,
            "handle_one_request",
            side_effect=raises or (lambda: None),
        ):
            handler.handle_one_request()
        return handler

    def test_every_way_a_client_can_vanish_is_swallowed(self):
        # ConnectionAbortedError is the Windows one (WinError 10053) that the
        # original two-name except clause let through.
        for error in (
            BrokenPipeError(),
            ConnectionResetError(),
            ConnectionAbortedError(10053, "aborted by the host machine"),
        ):
            with self.subTest(error=type(error).__name__):
                handler = self._handler(error)
                self.assertTrue(handler.close_connection)

    def test_a_real_error_still_propagates(self):
        # A bug in the dashboard must not be silently swallowed alongside them.
        with self.assertRaises(ValueError):
            self._handler(ValueError("a genuine bug"))


class TestFailureDistillation(unittest.TestCase):
    """The next attempt only sees what survives this. Noise here is fatal there."""

    CARGO = """    Checking tetris v0.1.0 (D:\\proj)
warning: field `cells` is never read
 --> src\\board.rs:2:5
  |
1 | pub struct Board {
  |            ----- field in this struct

error[E0616]: field `board` of struct `tetris::game::Game` is private
  --> tests\\game_test.rs:77:14
   |
77 |             .board
   |              ^^^^^ private field

warning: unused variable: `x`
 --> src\\game.rs:9:9

warning: 12 warnings emitted
error: could not compile `tetris` due to 1 previous error
"""

    def test_keeps_the_error_and_drops_the_warnings(self):
        out = distill(self.CARGO, limit=400)
        self.assertIn("E0616", out)
        self.assertNotIn("field `cells` is never read", out)

    def test_keeps_the_span_that_names_the_offending_line(self):
        # The `77 | .board` line starts at column zero, so a naive
        # "unindented means new block" rule drops the only evidence.
        out = distill(self.CARGO, limit=400)
        self.assertIn(".board", out)
        self.assertIn("private field", out)

    def test_reports_what_it_suppressed(self):
        self.assertIn("warning(s) suppressed", distill(self.CARGO, limit=400))

    def test_short_output_is_returned_untouched(self):
        self.assertEqual(distill("boom", limit=400), "boom")

    def test_never_cuts_inside_a_line(self):
        # Tail-slicing produced a failure note naming `s::game::Game`, a symbol
        # that appears nowhere in the source, because the cut landed mid-token.
        text = "\n".join(f"error: number {i} of something long" for i in range(400))
        for line in distill(text, limit=500).splitlines():
            self.assertTrue(
                line.startswith(("error:", "[")) or not line.strip(),
                f"line was cut mid-token: {line!r}",
            )

    def test_unrecognized_output_keeps_the_head_not_the_tail(self):
        # Compilers lead with the complaint; the tail is the summary.
        text = "FIRST LINE MATTERS\n" + "\n".join(f"filler {i}" for i in range(500))
        self.assertIn("FIRST LINE MATTERS", distill(text, limit=300))


class TestExecutorSeesSource(unittest.TestCase):
    """The executor has no filesystem — unshown files get invented."""

    def test_reference_files_are_pasted_read_only(self):
        ticket = Ticket("T-1", spec="s", allowed_files=["web/main.js"],
                        reference_files=["src/wasm.rs"])
        body = build_prompt(
            ticket, sources={"src/wasm.rs": "pub fn game_tick() {}"}
        )[-1].content
        self.assertIn("pub fn game_tick() {}", body)
        self.assertIn("do not return these files", body)

    def test_writable_files_are_shown_as_current_contents(self):
        ticket = Ticket("T-1", spec="s", allowed_files=["web/main.js"])
        body = build_prompt(ticket, sources={"web/main.js": "let x = 1;"})[-1].content
        self.assertIn("let x = 1;", body)
        self.assertIn("files you may write", body)

    def test_a_ticket_with_no_sources_is_unchanged(self):
        ticket = Ticket("T-1", spec="s", allowed_files=["a.py"])
        self.assertNotIn("Reference —", build_prompt(ticket)[-1].content)

    def test_reference_files_round_trip_through_the_store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", reference_files=["src/wasm.rs"])])
        self.assertEqual(store.list_tickets(run_id)[0].reference_files, ["src/wasm.rs"])

    def test_tester_is_shown_the_code_it_asserts_against(self):
        # A tester that guesses the API writes `game.over` for a method and
        # `game.board` for a private field. That does not fail a test — it
        # fails to compile, and every later ticket's verify step dies on a
        # file unrelated to it.
        ticket = Ticket("T-1", spec="s", criteria=["c"])
        body = tests_prompt(
            ticket,
            ["src/game.rs"],
            test_path="tests/t_1_test.rs",
            sources={"src/game.rs": "pub fn over(&self) -> bool"},
        )[-1].content
        self.assertIn("pub fn over(&self) -> bool", body)
        self.assertIn("code under test", body)

    def test_tester_prompt_without_sources_is_unchanged(self):
        ticket = Ticket("T-1", spec="s", criteria=["c"])
        self.assertNotIn(
            "code under test",
            tests_prompt(ticket, ["a.rs"], test_path="tests/t_1_test.rs")[-1].content,
        )

    def test_respec_can_add_reference_files(self):
        revision = parse_respec(
            '{"spec": "s", "reference_files": ["src/wasm.rs", "src/game.rs"]}'
        )
        self.assertEqual(revision["reference_files"], ["src/wasm.rs", "src/game.rs"])


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


class TestProviderWorkingDirectory(unittest.TestCase):
    """Adapters that shell out must run in the project, not wherever the daemon
    was started. A planner in the wrong directory does not fail — it reads
    another repository and writes confident tickets about it."""

    def _config(self, root: Path, block: dict) -> Config:
        return Config(
            root=root,
            models={"m": block},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
        )

    def test_project_root_becomes_the_default_cwd(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root, {"kind": "claude-cli", "model": "opus"})

        self.assertEqual(config.model_block("m")["cwd"], str(root))
        self.assertEqual(config.provider_for("planner").cwd, str(root))

    def test_an_explicit_cwd_is_not_overridden(self):
        # A deliberate override — pointing the planner at a sibling checkout —
        # has to survive the default.
        config = self._config(
            Path(tempfile.mkdtemp()),
            {"kind": "claude-cli", "model": "opus", "cwd": "/elsewhere"},
        )
        self.assertEqual(config.model_block("m")["cwd"], "/elsewhere")

    def test_the_command_adapter_gets_it_too(self):
        root = Path(tempfile.mkdtemp())
        config = self._config(root, {"kind": "command", "command": ["echo", "hi"]})
        self.assertEqual(config.provider_for("executor").cwd, str(root))

    def test_model_block_does_not_mutate_the_stored_config(self):
        config = self._config(Path(tempfile.mkdtemp()), {"kind": "claude-cli", "model": "opus"})
        config.model_block("m")
        self.assertNotIn("cwd", config.models["m"])


class TestTesterEvidence(unittest.TestCase):
    """The tester never sees the repo, so the two things that decide whether
    its output is collectable — the runner and an example — must be handed to
    it. A pytest file under `unittest discover` collects zero tests."""

    def _orchestrator(self, test_command: str = "python -m unittest discover tests"):
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": test_command},
        )
        return Orchestrator(config, Store(root / "t.db")), root

    def test_finds_an_existing_test_to_imitate(self):
        orch, root = self._orchestrator()
        (root / "tests").mkdir()
        (root / "tests" / "test_thing.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
            encoding="utf-8",
        )

        found = orch._example_test([])

        self.assertIsNotNone(found)
        self.assertEqual(found[0], "tests/test_thing.py")
        self.assertIn("unittest.TestCase", found[1])

    def test_skips_files_this_ticket_just_wrote(self):
        # Handing back the tester's own previous attempt would launder a wrong
        # framework guess into "the repo convention".
        orch, root = self._orchestrator()
        (root / "tests").mkdir()
        (root / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n", "utf-8")

        self.assertIsNone(orch._example_test(["tests/test_new.py"]))

    def test_no_tests_yet_is_not_an_error(self):
        orch, _ = self._orchestrator()
        self.assertIsNone(orch._example_test([]))

    def test_prompt_carries_the_runner_and_the_example(self):
        messages = tests_prompt(
            Ticket("T-1", criteria=["x is 1"]),
            ["app.py"],
            test_path="tests/t_1_test.py",
            test_command="python -m unittest discover tests",
            example_test=("tests/test_thing.py", "import unittest\n"),
        )
        body = messages[-1].content
        self.assertIn("python -m unittest discover tests", body)
        self.assertIn("tests/test_thing.py", body)
        self.assertIn("import unittest", body)

    def test_prompt_without_an_example_still_asks_for_repo_conventions(self):
        body = tests_prompt(Ticket("T-1"), ["app.py"], test_path="tests/t_1_test.py")[-1].content
        self.assertIn("conventions already used in this repository", body)

    def test_failure_context_reaches_the_tester(self):
        body = tests_prompt(
            Ticket("T-1", criteria=["x is 1"]),
            ["app.py"],
            test_path="tests/t_1_test.py",
            failure_context="AssertionError: '\"HI!\"' not found in source",
        )[-1].content
        self.assertIn("not found in source", body)

    def test_failure_context_forbids_weakening_a_real_failure(self):
        # The dangerous reading of "your tests failed" is "make them pass".
        # A tester that deletes an assertion turns a caught defect into a green
        # suite over broken code.
        body = tests_prompt(
            Ticket("T-1"), ["app.py"], test_path="tests/t_1_test.py", failure_context="boom"
        )[-1].content
        self.assertIn("not yours to correct", body)
        self.assertIn("keep the assertion as written", body)

    def test_a_clean_first_attempt_carries_no_failure_section(self):
        body = tests_prompt(Ticket("T-1"), ["app.py"], test_path="tests/t_1_test.py")[-1].content
        self.assertNotIn("did not pass verification", body)


class TestTicketScopedDiff(unittest.TestCase):
    """`autoCommit` is off by default, so a verified ticket's work stays in the
    working tree. A whole-tree diff therefore shows ticket N's reviewer
    everything tickets 1..N-1 wrote, and it rejects them as out of scope —
    blaming the executor for work it did not do."""

    def _repo(self) -> Orchestrator:
        root = Path(tempfile.mkdtemp())
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t.local"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        (root / "first.py").write_text("original = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-qm", "initial"], cwd=root, capture_output=True, check=False
        )
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
        )
        return Orchestrator(config, Store(root / "t.db"))

    def test_earlier_uncommitted_work_is_excluded(self):
        orch = self._repo()
        root = orch.config.root
        # Ticket one lands and is never committed.
        (root / "first.py").write_text("original = 2\n", encoding="utf-8")

        baseline = orch._snapshot()  # ticket two starts here
        (root / "second.py").write_text("added = True\n", encoding="utf-8")

        diff = orch._diff(baseline)

        self.assertIn("second.py", diff)
        self.assertNotIn("first.py", diff)

    def test_new_files_still_appear(self):
        orch = self._repo()
        baseline = orch._snapshot()
        (orch.config.root / "brand_new.py").write_text("x = 1\n", encoding="utf-8")

        self.assertIn("brand_new.py", orch._diff(baseline))

    def test_snapshot_leaves_the_users_index_alone(self):
        orch = self._repo()
        root = orch.config.root
        (root / "untracked.py").write_text("x = 1\n", encoding="utf-8")

        orch._snapshot()

        status = subprocess.run(
            ["git", "status", "--short"], cwd=root, capture_output=True, text=True, check=False
        ).stdout
        # Still untracked: the snapshot staged nothing in the real index.
        self.assertIn("?? untracked.py", status)

    def test_gitignored_files_never_reach_the_reviewer(self):
        orch = self._repo()
        root = orch.config.root
        (root / ".gitignore").write_text("junk/\n", encoding="utf-8")
        baseline = orch._snapshot()
        (root / "junk").mkdir()
        (root / "junk" / "build.log").write_text("noise\n", encoding="utf-8")
        (root / "real.py").write_text("x = 1\n", encoding="utf-8")

        diff = orch._diff(baseline)

        self.assertIn("real.py", diff)
        self.assertNotIn("build.log", diff)

    def test_no_baseline_falls_back_to_the_whole_tree(self):
        # A snapshot that failed must not mean reviewing nothing.
        orch = self._repo()
        (orch.config.root / "first.py").write_text("changed = 1\n", encoding="utf-8")

        self.assertIn("first.py", orch._diff(""))


class TestWorkAlreadyOnDiskIsStillShown(unittest.TestCase):
    """A retry starts with the previous cycle's implementation still on disk.

    The executor rewrites it byte for byte, git reports no change, and the only
    thing left in the diff is the test file — which `_discard_tests` deleted, so
    it reappears as new. The reviewer reads that as "the implementation is
    missing" and rejects. Correctly, on the evidence it was given. Every
    attempt, every cycle: 37 attempts on one ticket before a human noticed.
    """

    def _repo(self) -> Orchestrator:
        root = Path(tempfile.mkdtemp())
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t.local"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        (root / "src").mkdir()
        (root / "src" / "game.rs").write_text("pub fn tick() {}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-qm", "the previous cycle's work"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
        )
        return Orchestrator(config, Store(root / "t.db"))

    def test_a_file_rewritten_identically_is_reported_as_absent_from_the_diff(self):
        orchestrator = self._repo()
        baseline = orchestrator._snapshot()
        # What the executor "wrote" this attempt: the same bytes.
        diff = orchestrator._diff(baseline)
        self.assertNotIn("src/game.rs", diff)

        unchanged = orchestrator._written_but_unchanged(["src/game.rs"], diff)

        self.assertEqual(list(unchanged), ["src/game.rs"])
        self.assertIn("pub fn tick() {}", unchanged["src/game.rs"])

    def test_a_file_the_attempt_really_changed_is_left_to_the_diff(self):
        orchestrator = self._repo()
        baseline = orchestrator._snapshot()
        (orchestrator.config.root / "src" / "game.rs").write_text(
            "pub fn tick() { todo!() }\n", encoding="utf-8"
        )

        diff = orchestrator._diff(baseline)

        self.assertEqual(orchestrator._written_but_unchanged(["src/game.rs"], diff), {})

    def test_the_reviewer_is_told_the_files_are_not_missing(self):
        body = review_prompt(
            Ticket("TT-003", spec="s", criteria=["Game::tick advances"]),
            "diff --git a/tests/tt_003_test.rs b/tests/tt_003_test.rs\n+#[test]\n",
            unchanged={"src/game.rs": "pub fn tick() {}"},
        )[-1].content

        self.assertIn("pub fn tick() {}", body)
        self.assertIn("not** missing", body)

    def test_nothing_is_added_when_every_written_file_shows_up(self):
        body = review_prompt(Ticket("T-1", spec="s"), "diff --git a/a.py b/a.py\n")[-1].content
        self.assertNotIn("identical to what was already on disk", body)


class TestSamplingOverride(unittest.TestCase):
    """The loop asks for a low temperature per role. Some model families ship
    an official sampling recipe that disagrees, and following it is the point
    of the override."""

    def _provider(self, block: dict):
        config = Config(
            root=Path(tempfile.mkdtemp()),
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192, **block}},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
        )
        return config.provider_for("executor")

    def test_role_default_is_used_when_config_is_silent(self):
        self.assertEqual(self._provider({}).temperature(0.2), 0.2)

    def test_config_overrides_the_role_default(self):
        self.assertEqual(self._provider({"temperature": 0.6}).temperature(0.2), 0.6)

    def test_zero_is_an_override_not_an_absence(self):
        # `if configured:` would silently drop a deliberate 0.0.
        self.assertEqual(self._provider({"temperature": 0}).temperature(0.7), 0.0)


class TestArtifacts(unittest.TestCase):
    """The record has to survive the things that make you want it: a run that
    failed unattended, on a disk that may be full, in a tree whose contents the
    reviewer is about to be shown."""

    def _artifacts(self) -> tuple[Artifacts, Path]:
        config_dir = Path(tempfile.mkdtemp()) / ".hybridforge"
        return Artifacts(config_dir, 1), config_dir

    def test_writes_envelope_and_raw_side_by_side(self):
        artifacts, config_dir = self._artifacts()

        artifacts.record("SL-001", 2, "build", {"status": "ok", "role": "executor"}, raw="hello")

        attempt = config_dir / "artifacts" / "run-1" / "SL-001" / "attempt-2"
        document = json.loads((attempt / "01-build.json").read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["attempt"], 2)
        self.assertEqual((attempt / "01-build.md").read_text(encoding="utf-8"), "hello")

    def test_steps_are_numbered_in_order_within_an_attempt(self):
        artifacts, config_dir = self._artifacts()

        for name in ("build", "apply", "review"):
            artifacts.record("SL-001", 1, name, {"status": "ok"})

        attempt = config_dir / "artifacts" / "run-1" / "SL-001" / "attempt-1"
        self.assertEqual(
            sorted(p.name for p in attempt.glob("*.json")),
            ["01-build.json", "02-apply.json", "03-review.json"],
        )

    def test_manifest_gets_one_line_per_step(self):
        artifacts, config_dir = self._artifacts()

        artifacts.record("SL-001", 1, "build", {"status": "ok"})
        artifacts.record("SL-002", 1, "review", {"status": "failed", "approved": False})

        lines = (config_dir / "artifacts" / "run-1" / "steps.jsonl").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertFalse(json.loads(lines[1])["approved"])

    def test_the_directory_is_gitignored_before_anything_is_written(self):
        # _diff() builds the reviewer's changeset with `git add -N .`, so an
        # unignored artifact directory would put the reviewer's own previous
        # verdict into the diff it is asked to approve.
        _, config_dir = self._artifacts()
        ignored = (config_dir / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("artifacts/", ignored)

    def test_an_older_gitignore_is_repaired_not_replaced(self):
        config_dir = Path(tempfile.mkdtemp()) / ".hybridforge"
        config_dir.mkdir(parents=True)
        (config_dir / ".gitignore").write_text("run.db\nrun.db-wal\nrun.db-shm\n", "utf-8")

        Artifacts(config_dir, 1)

        content = (config_dir / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("artifacts/", content)
        self.assertIn("run.db-wal", content)

    def test_a_ticket_id_cannot_escape_its_directory(self):
        # Ticket ids are text a planner model chose, not an identifier this
        # project controls.
        artifacts, config_dir = self._artifacts()

        artifacts.record("../../etc/passwd", 1, "build", {"status": "ok"})

        base = config_dir / "artifacts" / "run-1"
        written = [p for p in base.rglob("*.json")]
        self.assertEqual(len(written), 1)
        self.assertIn(base.resolve(), written[0].resolve().parents)

    def test_an_unwritable_location_never_raises(self):
        artifacts, config_dir = self._artifacts()
        # Stand a file where the run directory needs to be.
        run_dir = config_dir / "artifacts" / "run-1" / "SL-001"
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        run_dir.write_text("not a directory", encoding="utf-8")

        artifacts.record("SL-001", 1, "build", {"status": "ok"})

        self.assertTrue(artifacts.failure)

    def test_disabled_artifacts_write_nothing(self):
        config_dir = Path(tempfile.mkdtemp()) / ".hybridforge"
        artifacts = Artifacts(config_dir, 1, enabled=False)

        artifacts.record("SL-001", 1, "build", {"status": "ok"})

        self.assertFalse(config_dir.exists())


class TestReviewVerdict(unittest.TestCase):
    """Approval is inferred from text a model wrote freely, so every ambiguous
    reading has to fall to REJECT. A wrongly-rejected ticket costs an attempt;
    a wrongly-approved one defeats the review step entirely."""

    def test_plain_verdicts(self):
        self.assertTrue(parse_verdict("ACCEPT\n\nAll criteria met.")[0])
        self.assertFalse(parse_verdict("REJECT\n\nCriterion 3 unmet.")[0])

    def test_echoed_instruction_does_not_launder_a_rejection(self):
        # Observed in a real run: the model repeated its own instruction line,
        # so a startswith("REJECT") check read the rejection as approval and
        # the ticket was marked done over refused work.
        reply = "ACCEPT or REJECT:\n\nREJECT\n\n**Spec forbade editing that file.**"
        approved, _ = parse_verdict(reply)
        self.assertFalse(approved)

    def test_echoed_instruction_before_an_approval_still_approves(self):
        approved, _ = parse_verdict("ACCEPT or REJECT:\n\nACCEPT\n\nLooks right.")
        self.assertTrue(approved)

    def test_decorated_verdicts(self):
        for reply in ("**REJECT**", "# REJECT", "REJECT.", "`REJECT`", "  reject  "):
            self.assertFalse(parse_verdict(reply)[0], reply)
        for reply in ("**ACCEPT**", "## ACCEPT", "ACCEPT:", "accept"):
            self.assertTrue(parse_verdict(reply)[0], reply)

    def test_unreadable_verdict_is_a_rejection(self):
        approved, reason = parse_verdict("I had trouble reading this diff.")
        self.assertFalse(approved)
        self.assertIn("no readable ACCEPT or REJECT", reason)

    def test_empty_reply_is_a_rejection(self):
        self.assertFalse(parse_verdict("")[0])

    def test_prose_mentioning_rejection_does_not_flip_an_approval(self):
        reply = "ACCEPT\n\nI considered whether to reject this over naming, but it is fine."
        self.assertTrue(parse_verdict(reply)[0])


class TestTruncatedResponses(unittest.TestCase):
    """A response cut off at the output limit still parses. Every role has to
    refuse it explicitly, because none of the downstream checks can tell a
    half-written file from a deliberate one."""

    def _orchestrator(self) -> tuple[Orchestrator, Path, int]:
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={
                "local": {
                    "kind": "openai",
                    "baseUrl": "http://127.0.0.1:1/v1",
                    "model": "stub",
                    # Both set so capabilities() never reaches for discovery.
                    "contextWindow": 8192,
                    "maxOutputTokens": 1024,
                }
            },
            roles={role: "local" for role in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": ""},
        )
        store = Store(root / "t.db")
        run_id = store.create_run("goal")
        return Orchestrator(config, store), root, run_id

    @staticmethod
    def _completion(text: str, finish_reason: str) -> Completion:
        return Completion(text=text, usage=Usage(), finish_reason=finish_reason)

    def test_truncated_build_writes_nothing_and_spends_the_attempt(self):
        orch, root, run_id = self._orchestrator()
        orch._call = lambda *a, **k: self._completion(
            "app.py\n```python\ndef half(\n```", "length"
        )

        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["app.py"]), "")

        self.assertFalse(result.ok)
        # Retryable, not blocked: a shorter response may still succeed.
        self.assertFalse(result.blocked)
        self.assertIn("cut off at the output limit", result.detail)
        self.assertFalse((root / "app.py").exists())

    def test_untruncated_build_still_applies(self):
        orch, root, run_id = self._orchestrator()
        orch._call = lambda *a, **k: self._completion(
            "app.py\n```python\nx = 1\n```", "stop"
        )

        orch._attempt(run_id, Ticket("T-1", allowed_files=["app.py"]), "")

        self.assertEqual((root / "app.py").read_text(encoding="utf-8").strip(), "x = 1")

    def test_truncated_tests_are_discarded_without_failing_the_ticket(self):
        orch, root, run_id = self._orchestrator()

        def fake_call(_run_id, role, *a, **k):
            if role == "tester":
                return self._completion(
                    "test_app.py\n```python\ndef test_half(\n```", "length"
                )
            if role == "reviewer":
                return self._completion("ACCEPT", "stop")
            return self._completion("app.py\n```python\nx = 1\n```", "stop")

        orch._call = fake_call
        result = orch._attempt(
            run_id, Ticket("T-1", allowed_files=["app.py"], criteria=["x is 1"]), ""
        )

        # A missing test is a weaker result, not a failed ticket — but the
        # half-written file must not reach disk.
        self.assertTrue(result.ok)
        self.assertFalse((root / "test_app.py").exists())

    def test_truncated_review_is_not_read_as_approval(self):
        orch, root, run_id = self._orchestrator()
        calls: list[str] = []

        def fake_call(_run_id, role, *a, **k):
            calls.append(role)
            if role == "reviewer":
                # No REJECT in it — approval would otherwise be inferred.
                return self._completion("The diff looks correct so far, and", "length")
            return self._completion("app.py\n```python\nx = 1\n```", "stop")

        orch._call = fake_call
        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["app.py"]), "")

        self.assertIn("reviewer", calls)
        self.assertFalse(result.ok)
        self.assertIn("not treated as approval", result.detail)


class TestOneTestFilePerTicket(unittest.TestCase):
    """A tester free to name its own file renames it on every retry. The
    abandoned files stay on disk and keep running, verification is
    whole-project, and no other ticket has them in scope to delete — which is
    how one run reached 17 test files for 6 tickets and blocked all of them."""

    def _orchestrator(self, test_command: str = "cargo test"):
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={role: "m" for role in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": test_command},
        )
        return Orchestrator(config, Store(root / "t.db")), root

    def test_path_is_derived_from_the_ticket_and_does_not_move(self):
        orch, _ = self._orchestrator()
        ticket = Ticket("TT-004", allowed_files=["src/wasm.rs"])

        first, _ = orch._test_target(ticket, ["src/wasm.rs"], None)
        second, _ = orch._test_target(ticket, ["src/wasm.rs", "src/lib.rs"], None)

        self.assertEqual(first, "tests/tt_004_test.rs")
        self.assertEqual(first, second)

    def test_an_example_test_decides_the_directory_and_extension(self):
        orch, _ = self._orchestrator()
        example = ("test/unit/thing_test.py", "import unittest\n")

        path, _ = orch._test_target(Ticket("T-1"), ["app.py"], example)

        self.assertEqual(path, "test/unit/t_1_test.py")

    def test_a_test_file_the_planner_named_is_honoured(self):
        orch, _ = self._orchestrator()
        ticket = Ticket("T-1", allowed_files=["src/board.rs", "tests/board_test.rs"])

        path, reason = orch._test_target(ticket, ["src/board.rs"], None)

        self.assertEqual(path, "tests/board_test.rs")
        self.assertEqual(reason, "")

    def test_a_build_artifact_is_never_mistaken_for_the_test_convention(self):
        # `**/*_test.*` matches inside target/, and cargo fills
        # target/debug/.fingerprint with `...-integration-test-game_test.json`,
        # which sorts first. Taken as the example it concludes the project's
        # tests are JSON and skips test authoring for the whole run.
        orch, root = self._orchestrator()
        artifact = root / "target" / "debug" / ".fingerprint" / "tetris-0a5b"
        artifact.mkdir(parents=True)
        (artifact / "test-integration-test-game_test.json").write_text("{}", "utf-8")

        self.assertIsNone(orch._example_test([]))

        path, _ = orch._test_target(Ticket("TT-001"), ["src/piece.rs"], None)
        self.assertEqual(path, "tests/tt_001_test.rs")

    def test_docs_only_ticket_gets_no_tests(self):
        # TT-006 wrote build.sh, build.ps1 and README.md, and the tester
        # answered with a cargo target that string-matched the README.
        orch, _ = self._orchestrator()

        path, reason = orch._test_target(
            Ticket("TT-006"), ["build.sh", "build.ps1", "README.md"], None
        )

        self.assertEqual(path, "")
        self.assertIn("no source file", reason)

    def test_cross_language_ticket_gets_no_tests(self):
        # A ticket that wrote only HTML and JS must not acquire a Rust
        # integration test asserting on the text of index.html.
        orch, _ = self._orchestrator()
        example = ("tests/board_test.rs", "#[test]\nfn x() {}\n")

        path, reason = orch._test_target(
            Ticket("TT-005"), ["web/index.html", "web/main.js"], example
        )

        self.assertEqual(path, "")
        self.assertIn(".rs", reason)

    def test_tester_output_outside_the_one_path_is_dropped(self):
        orch, root, run_id = _stub_orchestrator()
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```",
            "tests/tt_001_test.rs\n```rust\n#[test]\nfn a() {}\n```\n"
            "tests/extra_wasm.rs\n```rust\n#[test]\nfn b() {}\n```",
            "ACCEPT\nfine",
        )

        orch._attempt(
            run_id,
            Ticket("TT-001", allowed_files=["src/game.rs"], criteria=["go() exists"]),
            "",
        )

        self.assertTrue((root / "tests" / "tt_001_test.rs").exists())
        self.assertFalse((root / "tests" / "extra_wasm.rs").exists())

    def test_unverified_tests_are_removed_when_the_ticket_gives_up(self):
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```",
            "tests/tt_001_test.rs\n```rust\n#[test]\nfn a() { panic!() }\n```",
            "REJECT\nnot what the spec asked for",
        )

        orch._work_ticket(
            run_id,
            Ticket("TT-001", allowed_files=["src/game.rs"], criteria=["go() exists"]),
        )

        self.assertFalse((root / "tests" / "tt_001_test.rs").exists())
        # The implementation stays: it is the ticket's own scope, and a human
        # reading the blocked note needs to see what was attempted.
        self.assertTrue((root / "src" / "game.rs").exists())

    def test_a_retry_is_not_shown_its_own_previous_attempt_as_the_convention(self):
        orch, root, run_id = _stub_orchestrator()
        (root / "tests").mkdir()
        (root / "tests" / "tt_001_test.rs").write_text(
            "#[test]\nfn wrong_guess_from_last_time() {}\n", "utf-8"
        )
        seen: list[str] = []

        def call(_run, role, messages, **_kw):
            if role == "tester":
                seen.append(messages[-1].content)
            return Completion(
                text="src/game.rs\n```rust\npub fn go() {}\n```", usage=Usage(),
                finish_reason="stop",
            )

        orch._call = call
        orch._attempt(
            run_id,
            Ticket("TT-001", allowed_files=["src/game.rs"], criteria=["go() exists"]),
            "",
        )

        self.assertEqual(len(seen), 1)
        self.assertNotIn("wrong_guess_from_last_time", seen[0])

    def test_a_plan_designated_test_file_that_predates_the_ticket_is_never_deleted(self):
        """A path the *plan* named may be a hand-written suite the ticket was
        asked to extend. Authorship still governs those: a failed ticket does
        not earn the right to delete a human's file."""
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1
        (root / "tests").mkdir()
        (root / "tests" / "legacy_suite.rs").write_text("#[test]\nfn old() {}\n", "utf-8")
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```",
            "tests/legacy_suite.rs\n```rust\n#[test]\nfn a() {}\n```",
            "REJECT\nno",
        )

        orch._work_ticket(
            run_id,
            Ticket(
                "TT-001",
                allowed_files=["src/game.rs", "tests/legacy_suite.rs"],
                criteria=["go() exists"],
            ),
        )

        self.assertTrue((root / "tests" / "legacy_suite.rs").exists())

    def test_the_generated_test_file_is_reclaimed_even_when_it_predates_the_run(self):
        """The id-derived name is this loop's own, so no run inherits it as
        somebody else's. Judging it by authorship is what let one orphan
        survive five retry cycles: once a file outlives a single run, every
        run after it records `created=False` and none can ever reclaim it."""
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1
        (root / "tests").mkdir()
        (root / "tests" / "tt_001_test.rs").write_text("#[test]\nfn stale() {}\n", "utf-8")
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```",
            "tests/tt_001_test.rs\n```rust\n#[test]\nfn a() {}\n```",
            "REJECT\nno",
        )

        orch._work_ticket(
            run_id,
            Ticket("TT-001", allowed_files=["src/game.rs"], criteria=["go() exists"]),
        )

        self.assertFalse((root / "tests" / "tt_001_test.rs").exists())


class TestPreExistingBreakageIsNotThisTicketsFault(unittest.TestCase):
    """Verification is whole-project, so it reports every ticket's breakage to
    whichever ticket runs next. Without attribution the executor is told to fix
    an error in a file its ticket cannot open, burns all three attempts, and
    respec then rewrites the spec around somebody else's bug."""

    # The real thing: an abandoned test file from an earlier ticket, whose
    # `extern` block never links. TT-001 through TT-006 all died on this.
    _ORPHAN = (
        "error[E0432]: unresolved import `tetris::wasm`\n"
        " --> tests/wasm_layer.rs:1:5\n"
        "  |\n"
        "1 | use tetris::wasm;\n"
        "\n"
        "error: could not compile `tetris` (test \"wasm_layer\")\n"
    )

    def test_a_failure_that_predates_the_ticket_does_not_fail_it(self):
        orch, _, run_id = _stub_orchestrator(
            commands={"lint": "", "typecheck": "", "test": "cargo test"}
        )
        orch._shell = _failing_shell(self._ORPHAN)
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```", "ACCEPT\nfine"
        )

        ticket = Ticket("TT-002", allowed_files=["src/game.rs"])
        result = orch._attempt(
            run_id, ticket, "", pre_existing={"test": signatures(self._ORPHAN)}
        )

        self.assertTrue(result.ok)

    def test_a_new_failure_alongside_a_pre_existing_one_still_fails(self):
        orch, _, run_id = _stub_orchestrator(
            commands={"lint": "", "typecheck": "", "test": "cargo test"}
        )
        mine = self._ORPHAN + "\nerror[E0425]: cannot find value `nope`\n --> src/game.rs:2:5\n"
        orch._shell = _failing_shell(mine)
        orch._call = _replies("src/game.rs\n```rust\npub fn go() {}\n```")

        result = orch._attempt(
            run_id,
            Ticket("TT-002", allowed_files=["src/game.rs"]),
            "",
            pre_existing={"test": signatures(self._ORPHAN)},
        )

        self.assertFalse(result.ok)
        # The executor is told which half is its problem, or it tries to fix
        # the orphan it has no scope for.
        self.assertIn("not yours to fix", result.detail)
        self.assertIn("e0425", result.detail.lower())

    def test_signatures_survive_a_rebuild(self):
        # cargo renames the target hash and rust stamps a pid into every panic
        # header. Comparing raw text would call every pre-existing failure new.
        first = (
            "error: linking with `link.exe` failed\n"
            " --> tests/wasm_layer.rs:1:5\n"
            "thread 'x' (64464) panicked at tests/board.rs:145:5:\n"
        )
        second = first.replace("64464", "12987")

        self.assertEqual(signatures(first), signatures(second))
        self.assertTrue(signatures(first))

    def test_a_completed_backlog_over_a_red_build_is_not_reported_done(self):
        # Nobody introduced the orphan, so no ticket was blamed for it and no
        # ticket had it in scope. The run must not call that success.
        orch, _, run_id = _stub_orchestrator(
            commands={"lint": "", "typecheck": "", "test": "cargo test"}
        )
        orch._shell = _failing_shell(self._ORPHAN)

        self.assertEqual(orch._finish(run_id), "blocked")

    def test_a_completed_backlog_over_a_green_build_is_done(self):
        orch, _, run_id = _stub_orchestrator(
            commands={"lint": "", "typecheck": "", "test": "cargo test"}
        )
        orch._shell = lambda _run, name, cmd: StepResult(ok=True, detail="")

        self.assertEqual(orch._finish(run_id), "done")

    def test_unparseable_output_yields_no_signatures(self):
        # An empty set must not be read as "no errors": a set difference
        # against it would forgive a failure the ticket really did cause.
        self.assertEqual(signatures("build died\n"), set())


class TestDependencyGraph(unittest.TestCase):
    """A ticket is a testable unit, not a file lease. Two tickets may both write
    `src/lib.rs`; what they need from the backlog is an order, not exclusive
    ownership of the file."""

    def _tickets(self, *specs):
        return [
            Ticket(tid, position=i, allowed_files=list(files), needs=list(needs))
            for i, (tid, files, needs) in enumerate(specs)
        ]

    def test_a_shared_file_orders_the_pair_by_position(self):
        tickets = self._tickets(
            ("TT-003", ["src/game.rs", "src/lib.rs"], []),
            ("TT-004", ["src/wasm.rs", "src/lib.rs"], []),
        )
        derived = derive_needs(tickets)

        self.assertEqual(tickets[1].needs, ["TT-003"])
        self.assertEqual(tickets[0].needs, [])
        self.assertEqual(derived, [("TT-004", "TT-003", "src/lib.rs")])

    def test_tickets_that_share_nothing_stay_independent(self):
        tickets = self._tickets(
            ("TT-001", ["src/a.rs"], []),
            ("TT-002", ["src/b.rs"], []),
        )
        self.assertEqual(derive_needs(tickets), [])
        self.assertEqual([t.needs for t in tickets], [[], []])

    def test_a_declared_edge_is_never_reversed_by_derivation(self):
        """The plan may deliberately order a pair against reading order."""
        tickets = self._tickets(
            ("TT-003", ["src/lib.rs"], ["TT-004"]),
            ("TT-004", ["src/lib.rs"], []),
        )
        derive_needs(tickets)

        self.assertEqual(tickets[0].needs, ["TT-004"])
        self.assertEqual(tickets[1].needs, [])

    def test_three_writers_chain_rather_than_fan_in(self):
        tickets = self._tickets(
            ("TT-001", ["src/lib.rs"], []),
            ("TT-002", ["src/lib.rs"], []),
            ("TT-003", ["src/lib.rs"], []),
        )
        derive_needs(tickets)

        self.assertEqual([t.needs for t in tickets], [[], ["TT-001"], ["TT-002"]])

    def test_derivation_never_introduces_a_cycle(self):
        tickets = self._tickets(
            ("TT-001", ["a.rs", "b.rs"], []),
            ("TT-002", ["a.rs", "b.rs"], []),
        )
        derive_needs(tickets)

        self.assertEqual(graph_problems(tickets), [])

    def test_a_dangling_dependency_is_reported(self):
        tickets = self._tickets(("TT-001", [], ["TT-999"]))
        self.assertIn("not in this backlog", graph_problems(tickets)[0])

    def test_a_self_dependency_is_reported(self):
        tickets = self._tickets(("TT-001", [], ["TT-001"]))
        self.assertIn("needs itself", graph_problems(tickets)[0])

    def test_a_cycle_is_reported_with_its_path(self):
        tickets = self._tickets(
            ("TT-001", [], ["TT-002"]),
            ("TT-002", [], ["TT-001"]),
        )
        problems = graph_problems(tickets)

        self.assertTrue(any("cycle" in p for p in problems), problems)

    def test_needs_survives_the_plan_round_trip(self):
        ticket = Ticket("TT-004", title="W", spec="s", criteria=["c"], needs=["TT-003"])
        reparsed = parse_plan(render_ticket(ticket))

        self.assertEqual(reparsed[0].needs, ["TT-003"])

    def test_ingest_refuses_a_backlog_whose_graph_does_not_resolve(self):
        plan = (
            "## TT-001: One\n\n**Needs:** TT-002\n\n### Spec\n\ndo a\n"
            "## TT-002: Two\n\n**Needs:** TT-001\n\n### Spec\n\ndo b\n"
        )
        with self.assertRaises(ValueError) as caught:
            ingest_document(plan)

        self.assertIn("cycle", str(caught.exception))


class TestRespecCannotPinASharedFile(unittest.TestCase):
    """Ingest refuses a whole-file claim outright, because there a human can
    restate it for free. Mid-run there is nobody to ask, so the offending
    criterion is dropped and the rest of the revision stands."""

    def _store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            [
                Ticket("T-1", status="failed", attempts=3, position=0,
                       allowed_files=["src/game.rs", "src/lib.rs"],
                       criteria=["game_score() returns 0"],
                       original_criteria=["game_score() returns 0"]),
                Ticket("T-2", position=1, allowed_files=["src/wasm.rs", "src/lib.rs"]),
            ],
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _revise(self, store, run_id, criteria):
        def call(_messages, _budget):
            return Completion(
                text=json.dumps({"spec": "revised spec", "criteria": criteria}),
                usage=Usage(),
            )

        ticket = store.list_tickets(run_id)[0]
        return respec.revise(store, run_id, ticket, call=call, budget=1024)

    def test_a_minted_whole_file_claim_is_dropped(self):
        store, run_id = self._store()
        self._revise(
            store,
            run_id,
            ["game_score() returns 0", "src/lib.rs contains exactly three lines"],
        )

        kept = store.list_tickets(run_id)[0].criteria
        self.assertNotIn("src/lib.rs contains exactly three lines", kept)
        self.assertIn("game_score() returns 0", kept)

    def test_the_rest_of_the_revision_still_lands(self):
        store, run_id = self._store()
        self._revise(
            store,
            run_id,
            ["game_score() returns 0", "src/lib.rs contains exactly three lines"],
        )

        self.assertEqual(store.list_tickets(run_id)[0].spec, "revised spec")

    def test_the_drop_is_reported_not_silent(self):
        store, run_id = self._store()
        self._revise(
            store,
            run_id,
            ["game_score() returns 0", "src/lib.rs contains exactly three lines"],
        )

        messages = " ".join(e["message"] for e in store.events_after(0))
        self.assertIn("pinning all of", messages)

    def test_a_superset_satisfiable_criterion_is_kept(self):
        store, run_id = self._store()
        self._revise(
            store,
            run_id,
            ["game_score() returns 0", "src/lib.rs declares module wasm"],
        )

        self.assertIn(
            "src/lib.rs declares module wasm", store.list_tickets(run_id)[0].criteria
        )


class TestMonotoneCriteriaOnSharedFiles(unittest.TestCase):
    """Ordering a shared file is not enough on its own. A ticket that pins the
    whole of `src/lib.rs` passes, the next ticket adds its module, and the
    first ticket's claim is false forever — verification is whole-project and
    permanent, so its own test then fails everything that follows."""

    def _pair(self, first_criterion: str, files=("src/lib.rs",)):
        return [
            Ticket("TT-003", position=0, allowed_files=list(files),
                   criteria=[first_criterion]),
            Ticket("TT-004", position=1, allowed_files=list(files),
                   criteria=["game_score() returns 0"]),
        ]

    def test_a_whole_file_claim_on_a_shared_file_is_refused(self):
        tickets = self._pair("src/lib.rs contains exactly three lines")
        problems = shared_file_conflicts(tickets)

        self.assertEqual(len(problems), 1)
        self.assertIn("TT-003", problems[0])
        self.assertIn("TT-004", problems[0])

    def test_the_same_claim_about_a_file_only_one_ticket_writes_is_fine(self):
        """A sole owner may pin its file as tightly as it likes — nothing is
        coming to contradict it."""
        tickets = self._pair("src/solo.rs contains exactly three lines")
        tickets[0].allowed_files = ["src/solo.rs"]

        self.assertEqual(shared_file_conflicts(tickets), [])

    def test_a_superset_satisfiable_claim_is_accepted(self):
        tickets = self._pair("src/lib.rs declares modules board, game and piece")
        self.assertEqual(shared_file_conflicts(tickets), [])

    def test_exactly_about_something_other_than_a_shared_file_is_not_flagged(self):
        """`render` writes exactly 200 bytes' is a real criterion from a real
        plan. The word alone means nothing without a shared path beside it."""
        tickets = self._pair("render writes exactly 200 bytes on a fresh game")
        self.assertEqual(shared_file_conflicts(tickets), [])

    def test_the_claim_is_caught_in_spec_prose_not_only_in_criteria(self):
        """Where it actually lives. A plan states the pin in its spec, and the
        tester turns that into the assertion downstream — checking only the
        criteria bullets misses the sentence they were derived from."""
        tickets = self._pair("game_score() returns 0")
        tickets[0].criteria = ["game_score() returns 0"]
        tickets[0].spec = (
            "Create the rules layer.\n"
            "`src/lib.rs` must end up containing exactly these three lines:\n"
        )
        problems = shared_file_conflicts(tickets)

        self.assertEqual(len(problems), 1)
        self.assertIn("spec", problems[0])

    def test_a_phrase_and_a_path_in_different_sentences_do_not_combine(self):
        tickets = self._pair("game_score() returns 0")
        tickets[0].spec = (
            "The renderer must emit exactly one frame per tick.\n"
            "It reads state through `src/lib.rs` as the crate root.\n"
        )
        self.assertEqual(shared_file_conflicts(tickets), [])

    def test_ingest_refuses_the_plan_that_deadlocked(self):
        plan = (
            "## TT-003: Rules\n\n### Spec\n\n"
            "`src/lib.rs` must end up containing exactly these three lines:\n\n"
            "### Allowed files\n\n- `src/game.rs`\n- `src/lib.rs`\n"
            "## TT-004: Wasm\n\n### Spec\n\n"
            "`src/lib.rs` must end up containing exactly these four lines:\n\n"
            "### Allowed files\n\n- `src/wasm.rs`\n- `src/lib.rs`\n"
        )
        with self.assertRaises(ValueError) as caught:
            ingest_document(plan)

        message = str(caught.exception)
        self.assertIn("cannot all hold at once", message)
        self.assertIn("TT-003", message)
        self.assertIn("TT-004", message)


class TestCriteriaAreMatchedByWhatTheyAssert(unittest.TestCase):
    """A planner that rewords a criterion has not raised a new one. Comparing
    exact strings restores the plan's wording *and* keeps the rewording, so the
    contract doubles and the executor reads every demand twice."""

    def _ticket(self):
        return Ticket(
            "TT-003",
            criteria=["`Game::new(0)` does not panic", "`tick(0)` leaves `y` unchanged"],
            original_criteria=[
                "`Game::new(0)` does not panic",
                "`tick(0)` leaves `y` unchanged",
            ],
        )

    def test_a_reworded_criterion_replaces_its_original(self):
        ticket = self._ticket()
        merged, refused = _merge_criteria(
            ticket,
            ["Game::new(0) does not panic", "tick(0) leaves y unchanged"],
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual(refused, [])

    def test_the_plans_wording_is_the_one_that_survives(self):
        ticket = self._ticket()
        merged, _ = _merge_criteria(ticket, ["Game::new(0) does not panic"])

        self.assertIn("`Game::new(0)` does not panic", merged)

    def test_a_genuinely_dropped_criterion_is_still_restored_and_reported(self):
        ticket = self._ticket()
        merged, refused = _merge_criteria(ticket, ["`Game::new(0)` does not panic"])

        self.assertEqual(len(merged), 2)
        self.assertEqual(refused, ["`tick(0)` leaves `y` unchanged"])

    def test_a_genuinely_new_criterion_is_still_added(self):
        ticket = self._ticket()
        merged, _ = _merge_criteria(
            ticket, [*ticket.criteria, "`level` starts at 1"]
        )

        self.assertEqual(len(merged), 3)
        self.assertIn("`level` starts at 1", merged)

    def test_thirteen_criteria_reworded_stay_thirteen(self):
        """The observed regression: a plan stating 13 reached 27 in one pass."""
        plan = [f"`f{i}()` returns {i}" for i in range(13)]
        ticket = Ticket("TT-003", criteria=list(plan), original_criteria=list(plan))
        merged, refused = _merge_criteria(
            ticket, [c.replace("`", "") for c in plan]
        )

        self.assertEqual(len(merged), 13)
        self.assertEqual(refused, [])


class TestDependencyScheduling(unittest.TestCase):
    """Ordering already existed via `position`; what was missing is what happens
    when a dependency does not land. Running the dependent anyway files failures
    about a ticket whose only problem is that something else has not happened."""

    def _run(self, *specs):
        orch, root, run_id = _stub_orchestrator()
        orch.store.add_tickets(
            run_id,
            [
                Ticket(tid, position=i, status=status, needs=list(needs))
                for i, (tid, status, needs) in enumerate(specs)
            ],
        )
        return orch, run_id

    def test_a_ticket_waits_until_its_dependency_is_done(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_PENDING, []),
            ("TT-002", TICKET_PENDING, ["TT-001"]),
        )
        self.assertEqual(orch.store.next_ticket(run_id).ticket_id, "TT-001")

    def test_the_dependent_becomes_eligible_once_it_lands(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, []),
            ("TT-002", TICKET_PENDING, ["TT-001"]),
        )
        self.assertEqual(orch.store.next_ticket(run_id).ticket_id, "TT-002")

    def test_position_still_breaks_ties_between_eligible_tickets(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_PENDING, []),
            ("TT-002", TICKET_PENDING, []),
        )
        self.assertEqual(orch.store.next_ticket(run_id).ticket_id, "TT-001")

    def test_a_later_ticket_runs_while_an_earlier_one_is_stuck(self):
        """Blocking is per-edge, not per-position: an independent ticket behind
        a failed one is not implicated by it."""
        orch, run_id = self._run(
            ("TT-001", TICKET_FAILED, []),
            ("TT-002", TICKET_PENDING, ["TT-001"]),
            ("TT-003", TICKET_PENDING, []),
        )
        self.assertEqual(orch.store.next_ticket(run_id).ticket_id, "TT-003")

    def test_a_ticket_whose_dependency_failed_is_skipped_not_attempted(self):
        orch, run_id = self._run(
            ("TT-003", TICKET_FAILED, []),
            ("TT-004", TICKET_PENDING, ["TT-003"]),
        )
        parked = orch._park_unreachable(run_id)

        after = {t.ticket_id: t for t in orch.store.list_tickets(run_id)}
        self.assertEqual(parked, 1)
        self.assertEqual(after["TT-004"].status, TICKET_SKIPPED)
        self.assertIn("TT-003", after["TT-004"].blocked_note)
        self.assertEqual(after["TT-004"].attempts, 0)

    def test_parking_leaves_a_runnable_ticket_alone(self):
        orch, run_id = self._run(("TT-001", TICKET_PENDING, []))
        self.assertEqual(orch._park_unreachable(run_id), 0)
        self.assertEqual(
            orch.store.list_tickets(run_id)[0].status, TICKET_PENDING
        )

    def test_a_backlog_with_no_edges_schedules_exactly_as_before(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_PENDING, []),
            ("TT-002", TICKET_PENDING, []),
            ("TT-003", TICKET_PENDING, []),
        )
        order = []
        while (ticket := orch.store.next_ticket(run_id)) is not None:
            order.append(ticket.ticket_id)
            ticket.status = TICKET_DONE
            orch.store.update_ticket(run_id, ticket)

        self.assertEqual(order, ["TT-001", "TT-002", "TT-003"])


class TestStaleDependentsAreReopened(unittest.TestCase):
    """A ticket earns `done` against a particular version of what it was built
    on. Requeue that dependency — `forge retry --ticket` on something already
    green is a normal thing to do after reading a diff — and the pass above it
    was judged against a contract being replaced."""

    def _run(self, *specs, reopen=True):
        orch, _root, run_id = _stub_orchestrator()
        orch.config.loop.reopen_stale_dependents = reopen
        orch.store.add_tickets(
            run_id,
            [
                Ticket(tid, position=i, status=status, needs=list(needs), spec=spec)
                for i, (tid, status, needs, spec) in enumerate(specs)
            ],
        )
        # Stamp every done ticket against its dependencies as they stand.
        for ticket in orch.store.list_tickets(run_id):
            if ticket.status == TICKET_DONE:
                ticket.dep_stamp = orch._dep_stamp(run_id, ticket)
                orch.store.update_ticket(run_id, ticket)
        return orch, run_id

    def _statuses(self, orch, run_id):
        return {t.ticket_id: t.status for t in orch.store.list_tickets(run_id)}

    def test_a_settled_backlog_reopens_nothing(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        self.assertEqual(orch._reopen_stale(run_id), [])

    def test_requeueing_a_dependency_reopens_what_passed_on_it(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        orch.store.reset_tickets(run_id, ticket_ids=["TT-001"])

        self.assertEqual(orch._reopen_stale(run_id), ["TT-002"])
        self.assertEqual(self._statuses(orch, run_id)["TT-002"], TICKET_PENDING)

    def test_a_rewritten_dependency_spec_reopens_its_dependent(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        dep = orch.store.list_tickets(run_id)[0]
        dep.spec = "respec rewrote this"
        orch.store.update_ticket(run_id, dep)

        self.assertEqual(orch._reopen_stale(run_id), ["TT-002"])

    def test_the_reopen_is_transitive(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
            ("TT-003", TICKET_DONE, ["TT-002"], "c"),
        )
        orch.store.reset_tickets(run_id, ticket_ids=["TT-001"])

        self.assertEqual(orch._reopen_stale(run_id), ["TT-002", "TT-003"])

    def test_an_unrelated_ticket_is_not_reopened(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
            ("TT-003", TICKET_DONE, [], "c"),
        )
        orch.store.reset_tickets(run_id, ticket_ids=["TT-001"])

        self.assertEqual(orch._reopen_stale(run_id), ["TT-002"])
        self.assertEqual(self._statuses(orch, run_id)["TT-003"], TICKET_DONE)

    def test_a_dependency_that_reran_unchanged_invalidates_nothing(self):
        """Status is deliberately not part of the fingerprint: passing again on
        the same contract is not a reason to redo the work above it."""
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        dep = orch.store.list_tickets(run_id)[0]
        dep.attempts = 2
        orch.store.update_ticket(run_id, dep)

        self.assertEqual(orch._reopen_stale(run_id), [])

    def test_widened_dependency_scope_counts_as_a_change(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        dep = orch.store.list_tickets(run_id)[0]
        dep.allowed_files = ["src/new.rs"]
        orch.store.update_ticket(run_id, dep)

        self.assertEqual(orch._reopen_stale(run_id), ["TT-002"])

    def test_the_switch_off_warns_and_leaves_the_ticket_done(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
            reopen=False,
        )
        orch.store.reset_tickets(run_id, ticket_ids=["TT-001"])

        self.assertEqual(orch._reopen_stale(run_id), [])
        self.assertEqual(self._statuses(orch, run_id)["TT-002"], TICKET_DONE)
        messages = " ".join(e["message"] for e in orch.store.events_after(0))
        self.assertIn("reopenStaleDependents is off", messages)

    def test_the_log_names_which_dependency_forced_the_reopen(self):
        orch, run_id = self._run(
            ("TT-001", TICKET_DONE, [], "a"),
            ("TT-002", TICKET_DONE, ["TT-001"], "b"),
        )
        orch.store.reset_tickets(run_id, ticket_ids=["TT-001"])
        orch._reopen_stale(run_id)

        messages = " ".join(e["message"] for e in orch.store.events_after(0))
        self.assertIn("TT-002: reopened", messages)
        self.assertIn("TT-001", messages)


class TestOrphanedTestsNeverOutliveTheirTicket(unittest.TestCase):
    """Verification is whole-project, so a test file whose ticket never landed
    fails every other ticket in the backlog — and none of them has it in scope
    to delete. The per-ticket discard covers a ticket that fails inside the
    loop; this covers the ones it cannot see."""

    def _orch(self):
        orch, root, run_id = _stub_orchestrator()
        (root / "tests").mkdir()
        return orch, root, run_id

    def test_the_run_sweeps_an_orphan_whose_ticket_was_skipped(self):
        orch, root, run_id = self._orch()
        orphan = root / "tests" / "tt_004_test.rs"
        orphan.write_text("#[test]\nfn a() {}\n", encoding="utf-8")
        orch.store.add_tickets(run_id, [Ticket("TT-004", status=TICKET_SKIPPED)])

        orch._finish(run_id)

        self.assertFalse(orphan.exists())

    def test_a_passing_tickets_tests_are_left_alone(self):
        orch, root, run_id = self._orch()
        kept = root / "tests" / "tt_002_test.rs"
        kept.write_text("#[test]\nfn a() {}\n", encoding="utf-8")
        orch.store.add_tickets(run_id, [Ticket("TT-002", status=TICKET_DONE)])

        orch._finish(run_id)

        self.assertTrue(kept.exists())

    def test_a_file_belonging_to_no_ticket_is_not_touched(self):
        """Ownership is by the id-derived name. Anything else in tests/ is
        somebody's, and the run does not get to guess whose."""
        orch, root, run_id = self._orch()
        theirs = root / "tests" / "integration_suite.rs"
        theirs.write_text("#[test]\nfn a() {}\n", encoding="utf-8")
        orch.store.add_tickets(run_id, [Ticket("TT-004", status=TICKET_FAILED)])

        orch._finish(run_id)

        self.assertTrue(theirs.exists())

    def test_ownership_holds_across_directory_and_extension(self):
        orch, root, run_id = self._orch()
        (root / "spec").mkdir()
        for name in ("tests/tt_005_test.js", "spec/tt_005_test.py"):
            (root / name).write_text("x\n", encoding="utf-8")
        orch.store.add_tickets(run_id, [Ticket("TT-005", status=TICKET_FAILED)])

        orch._finish(run_id)

        self.assertFalse((root / "tests" / "tt_005_test.js").exists())
        self.assertFalse((root / "spec" / "tt_005_test.py").exists())

    def test_the_sweep_runs_before_the_final_verify_reads_the_tree(self):
        """The orphan is exactly what the final check would trip over, so
        removing it afterwards would report a red build it had already fixed."""
        orch, root, run_id = _stub_orchestrator({"test": "cmd"})
        (root / "tests").mkdir()
        orphan = root / "tests" / "tt_004_test.rs"
        orphan.write_text("#[test]\nfn a() {}\n", encoding="utf-8")
        orch.store.add_tickets(run_id, [Ticket("TT-004", status=TICKET_SKIPPED)])

        seen: list[bool] = []
        orch._shell = lambda *a, **k: (  # noqa: ARG005
            seen.append(orphan.exists()) or unittest.mock.Mock(ok=True, output="")
        )
        orch._finish(run_id)

        self.assertTrue(seen, "final verify never ran")
        self.assertNotIn(True, seen, "verify saw the orphan still on disk")


def _stub_orchestrator(commands: dict[str, str] | None = None):
    """An Orchestrator over a temp repo with every shell command disabled."""
    root = Path(tempfile.mkdtemp())
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
        commands=commands or {"lint": "", "typecheck": "", "test": ""},
    )
    store = Store(root / "t.db")
    return Orchestrator(config, store), root, store.create_run("goal")


class TestWritableFilesAreNeverAbridged(unittest.TestCase):
    """The executor returns whole files and is told to preserve every line it
    was not asked to change. Showing it three quarters of a file and asking for
    the complete one deletes the rest — with a successful apply, a plausible
    diff, and nothing anywhere recording that it happened."""

    def _big(self, root: Path, name: str, size: int) -> str:
        body = "\n".join(f"line_{i} = {i}" for i in range(size))
        text = f"# head\n{body}\n# TAIL_SENTINEL\n"
        (root / name).write_text(text, encoding="utf-8")
        return text

    def test_a_writable_file_reaches_the_model_entire(self):
        orch, root, _ = _stub_orchestrator()
        original = self._big(root, "big.py", 4000)
        self.assertGreater(len(original), Orchestrator._SOURCE_LIMIT)

        sources, oversized = orch._sources_for(
            Ticket("T-1", allowed_files=["big.py"]), whole=["big.py"]
        )

        self.assertEqual(oversized, [])
        self.assertEqual(sources["big.py"], original)
        self.assertIn("TAIL_SENTINEL", sources["big.py"])

    def test_a_reference_file_is_still_clipped(self):
        # Losing the tail of read-only context costs accuracy, not data.
        orch, root, _ = _stub_orchestrator()
        self._big(root, "ref.py", 4000)

        sources, oversized = orch._sources_for(Ticket("T-1", reference_files=["ref.py"]))

        self.assertEqual(oversized, [])
        self.assertNotIn("TAIL_SENTINEL", sources["ref.py"])
        self.assertIn("reference only", sources["ref.py"])

    def test_a_file_too_large_to_round_trip_blocks_the_ticket(self):
        orch, root, run_id = _stub_orchestrator()
        huge = "x = 1\n" * 40_000
        (root / "huge.py").write_text(huge, encoding="utf-8")
        self.assertGreater(len(huge), Orchestrator._WRITABLE_CEILING)
        called = []
        orch._call = lambda *a, **k: called.append(1)

        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["huge.py"]), "")

        self.assertTrue(result.blocked)
        self.assertIn("too large to rewrite in full", result.detail)
        # Blocked before spending anything, and before showing a partial copy.
        self.assertEqual(called, [])


class TestFailureHistoryReachesBothRoles(unittest.TestCase):
    """`failure_context` carries only the newest failure, which is what lets an
    executor oscillate — fix A breaks B, fix B brings A back — for its whole
    retry budget with nothing able to see the cycle."""

    def test_earlier_failures_are_carried_forward_to_the_executor(self):
        body = build_prompt(
            Ticket("T-1", spec="s"),
            "lint failed:\nerror: B is broken",
            prior_failures=["Attempt 1: lint failed:\nerror: A is broken"],
        )[-1].content

        self.assertIn("A is broken", body)
        self.assertIn("B is broken", body)
        self.assertIn("undoing each other", body)

    def test_a_first_attempt_carries_no_history_section(self):
        body = build_prompt(Ticket("T-1", spec="s"))[-1].content
        self.assertNotIn("Earlier attempts on this ticket", body)

    def test_the_reviewer_is_shown_its_own_earlier_rejections(self):
        body = review_prompt(
            Ticket("T-1", spec="s"),
            "diff --git a/x b/x",
            prior_verdicts=["REJECT\nthe error path is swallowed"],
        )[-1].content

        self.assertIn("the error path is swallowed", body)
        # The instruction that stops three attempts dying on three unrelated
        # objections is the whole point of showing them.
        self.assertIn("do not replace it with a fresh objection", body)

    def test_a_first_review_carries_no_prior_verdicts(self):
        body = review_prompt(Ticket("T-1", spec="s"), "diff")[-1].content
        self.assertNotIn("already rejected", body)

    def test_a_rejection_is_recorded_for_the_next_review(self):
        orch, _, run_id = _stub_orchestrator()
        orch._call = _replies(
            "src/a.py\n```python\nx = 1\n```", "REJECT\nmissing the error path"
        )
        rejections: list[str] = []

        orch._attempt(
            run_id, Ticket("T-1", allowed_files=["src/a.py"]), "", rejections=rejections
        )

        self.assertEqual(len(rejections), 1)
        self.assertIn("missing the error path", rejections[0])


class TestEmptyDiffShowsState(unittest.TestCase):
    """A ticket can pass verification having changed nothing. Handed
    `(empty diff)` and nothing else, one real reviewer replied "No build.sh,
    build.ps1, README.md exist" about a repo where all three did."""

    def test_an_empty_diff_carries_the_files_on_disk(self):
        body = review_prompt(
            Ticket("T-1", spec="s", allowed_files=["build.sh"]),
            "",
            state={"build.sh": "#!/usr/bin/env sh\ncargo build\n"},
        )[-1].content

        self.assertIn("cargo build", body)
        self.assertIn("what is actually on disk", body)
        # Already-done work is finished, not failed.
        self.assertIn("ACCEPT", body)

    def test_a_real_diff_does_not_carry_the_state_block(self):
        body = review_prompt(Ticket("T-1", spec="s"), "diff --git a/x b/x")[-1].content
        self.assertNotIn("what is actually on disk", body)

    def test_the_loop_reads_state_only_when_the_diff_is_empty(self):
        orch, root, run_id = _stub_orchestrator()
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        seen: list[str] = []

        def call(_run, role, messages, **_kw):
            if role == "reviewer":
                seen.append(messages[-1].content)
            return Completion(
                text="a.py\n```python\nx = 1\n```", usage=Usage(), finish_reason="stop"
            )

        orch._call = call
        # No git in a bare temp dir, so _diff returns "" — the empty-diff path.
        orch._attempt(run_id, Ticket("T-1", allowed_files=["a.py"]), "")

        self.assertEqual(len(seen), 1)
        self.assertIn("what is actually on disk", seen[0])


class TestBaselineVerifyIsOptional(unittest.TestCase):
    def test_it_is_on_by_default(self):
        self.assertTrue(LoopSettings().baseline_verify)

    def test_turning_it_off_skips_the_extra_verify_run(self):
        orch, _, run_id = _stub_orchestrator(
            commands={"lint": "", "typecheck": "", "test": "cargo test"}
        )
        orch.config.loop.baseline_verify = False
        orch.config.loop.max_attempts = 1
        ran: list[str] = []

        def shell(_run_id, name, command):
            ran.append(name)
            return StepResult(ok=True, detail="")

        orch._shell = shell
        orch._call = _replies("a.py\n```python\nx = 1\n```", "ACCEPT\nfine")

        orch._work_ticket(run_id, Ticket("T-1", allowed_files=["a.py"]))

        self.assertNotIn("baseline-test", ran)
        self.assertIn("test", ran)


class TestStatusShowsTheNewestRun(unittest.TestCase):
    """An older blocked run must not shadow a newer finished one — that
    reported `run 7: blocked` right after run 8 went six-for-six."""

    def test_a_finished_run_is_not_hidden_by_an_older_blocked_one(self):
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
        )
        store = Store(root / "t.db")
        old = store.create_run("older")
        store.set_run_status(old, "blocked", "6 ticket(s) need a human")
        new = store.create_run("newer")
        store.set_run_status(new, "done", "all tickets complete")

        state = ui_server.snapshot(store, config)

        self.assertEqual(state["run"]["id"], new)
        self.assertEqual(state["run"]["status"], "done")


class TestHealthProbeNeedsRoomToThink(unittest.TestCase):
    """A reasoning model spends output tokens before it says anything. The
    probe asked for 16 and reported `ok ... reply=''` — a pass recorded for a
    model that had not answered."""

    class _Stub(Provider):
        kind = "stub"

        def __init__(self, text: str, finish: str = "stop"):
            super().__init__("stub", {"model": "m"})
            self.text, self.finish, self.asked = text, finish, 0

        def capabilities(self):
            return Capabilities(context_window=32768, max_output_tokens=8192)

        def complete(self, messages, *, max_tokens, temperature=0.2, timeout=600):
            self.asked = max_tokens
            return Completion(text=self.text, usage=Usage(), finish_reason=self.finish)

    def test_the_probe_leaves_room_for_a_preamble(self):
        stub = self._Stub("OK")
        stub.health()
        self.assertGreaterEqual(stub.asked, 512)

    def test_an_empty_reply_is_a_failure_not_a_pass(self):
        self.assertTrue(self._Stub("", finish="length").health().startswith("FAIL"))
        self.assertIn("output limit", self._Stub("", finish="length").health())

    def test_a_real_reply_still_passes(self):
        self.assertTrue(self._Stub("OK").health().startswith("ok"))


class TestServedContextBeatsTrainedContext(unittest.TestCase):
    """`/api/show` reports what the model was trained for; `/api/ps` reports
    what Ollama actually loaded. On a real box those read 131072 and 32768.
    Planning against the larger one hands the budget gate a ceiling four times
    too high, and the server then truncates from the front of the prompt —
    dropping the system message and the spec."""

    TRAINED = {"model_info": {"gptoss.context_length": 131072}}

    def _provider(self, loaded: dict | None = None, **config):
        provider = OpenAICompatProvider(
            "local", {"baseUrl": "http://x:11434/v1", "model": "m", **config}
        )
        models = [loaded] if loaded else []
        provider_mod = sys.modules["forge.providers.openai_compat"]
        return provider, provider_mod, {"models": models}

    def _patched(self, provider_mod, ps_payload):
        return (
            unittest.mock.patch.object(provider_mod, "get_json", lambda *a, **k: ps_payload),
            unittest.mock.patch.object(provider_mod, "post_json", lambda *a, **k: self.TRAINED),
        )

    def test_the_served_window_wins(self):
        loaded = {"name": "m", "context_length": 32768, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded)
        get, post = self._patched(mod, ps)
        with get, post:
            self.assertEqual(provider.capabilities().context_window, 32768)

    def test_the_trained_window_is_the_fallback_when_nothing_is_loaded(self):
        provider, mod, ps = self._provider(None)
        get, post = self._patched(mod, ps)
        with get, post:
            self.assertEqual(provider.capabilities().context_window, 131072)

    def test_config_still_beats_both(self):
        loaded = {"name": "m", "context_length": 32768, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded, contextWindow=16384)
        get, post = self._patched(mod, ps)
        with get, post:
            self.assertEqual(provider.capabilities().context_window, 16384)

    def test_a_window_wider_than_the_server_is_warned_about(self):
        loaded = {"name": "m", "context_length": 32768, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded, contextWindow=131072, maxOutputTokens=4096)
        get, post = self._patched(mod, ps)
        with get, post:
            notes = " ".join(provider.diagnostics())
        self.assertIn("silently truncated", notes)
        self.assertIn("32,768", notes)

    def test_an_output_reserve_that_eats_the_window_is_warned_about(self):
        loaded = {"name": "m", "context_length": 32768, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded, contextWindow=32768, maxOutputTokens=32768)
        get, post = self._patched(mod, ps)
        with get, post:
            notes = " ".join(provider.diagnostics())
        self.assertIn("maxOutputTokens", notes)
        self.assertIn("Every ticket overflows", notes)

    def test_the_library_default_ratio_does_not_warn_about_itself(self):
        # 4096 of 8192 is the Capabilities default. A check that fires on it
        # trains the reader to skip the whole section.
        loaded = {"name": "m", "context_length": 8192, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded, contextWindow=8192)
        get, post = self._patched(mod, ps)
        with get, post:
            notes = " ".join(provider.diagnostics())
        self.assertNotIn("maxOutputTokens", notes)

    def test_a_sane_configuration_warns_about_nothing_important(self):
        loaded = {"name": "m", "context_length": 32768, "size": 8, "size_vram": 8}
        provider, mod, ps = self._provider(loaded, contextWindow=32768, maxOutputTokens=8192)
        get, post = self._patched(mod, ps)
        with get, post:
            notes = provider.diagnostics()
        self.assertFalse([n for n in notes if not n.startswith("note:")])

    def test_partial_vram_residency_is_reported(self):
        loaded = {"name": "m", "context_length": 32768, "size": 100, "size_vram": 40}
        provider, mod, ps = self._provider(loaded, contextWindow=32768, maxOutputTokens=8192)
        get, post = self._patched(mod, ps)
        with get, post:
            notes = " ".join(provider.diagnostics())
        self.assertIn("40% of", notes)
        self.assertIn("runs on CPU", notes)

    def test_a_non_ollama_endpoint_asks_nothing_and_warns_nothing(self):
        provider = OpenAICompatProvider(
            "remote",
            {"baseUrl": "https://api.example.com", "model": "m", "contextWindow": 8192},
        )
        mod = sys.modules["forge.providers.openai_compat"]

        def explode(*_a, **_k):
            raise AssertionError("must not probe a non-Ollama endpoint")

        with unittest.mock.patch.object(mod, "get_json", explode), \
             unittest.mock.patch.object(mod, "post_json", explode):
            self.assertEqual(provider.diagnostics(), [])

    def test_a_probe_failure_never_breaks_doctor(self):
        provider = OpenAICompatProvider(
            "local", {"baseUrl": "http://x:11434/v1", "model": "m", "contextWindow": 8192}
        )
        mod = sys.modules["forge.providers.openai_compat"]

        def explode(*_a, **_k):
            raise OSError("connection refused")

        with unittest.mock.patch.object(mod, "get_json", explode), \
             unittest.mock.patch.object(mod, "post_json", explode):
            self.assertEqual(provider.diagnostics(), [])


def _failing_shell(output: str):
    """A `_shell` stub that fails every configured command with `output`.

    Unconfigured steps still pass, as the real one does — otherwise the ticket
    fails on `lint` before reaching the step under test.
    """

    def shell(_run_id, name, command):
        if not command.strip():
            return StepResult(ok=True, detail=f"no {name} command configured; skipped")
        return StepResult(ok=False, detail=output)

    return shell


def _replies(*texts: str):
    """A `_call` stub that returns each reply in turn, repeating the last."""
    remaining = list(texts)

    def call(*_args, **_kwargs):
        text = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return Completion(text=text, usage=Usage(), finish_reason="stop")

    return call


class TestThinkingModelsThatNeverAnswer(unittest.TestCase):
    """A thinking model served over the OpenAI shape returns its chain of
    thought in a non-standard sibling field and leaves `content` empty until it
    stops thinking. Spend the whole output budget there and the reply is an
    empty string with `finish_reason: length` — which every JSON parser
    downstream reports as malformed output, sending the reader to look at the
    prompt when the output budget is what ran out."""

    THOUGHT = "Let me think about this at considerable length. " * 20

    def _provider(self, payload: dict, **config) -> OpenAICompatProvider:
        provider = OpenAICompatProvider(
            "local",
            {
                "baseUrl": "http://x:11434/v1",
                "model": "thinker",
                # Both set so capabilities() never reaches for discovery.
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                **config,
            },
        )
        mod = sys.modules["forge.providers.openai_compat"]
        self.enterContext(unittest.mock.patch.object(mod, "post_json", lambda *a, **k: payload))
        return provider

    @staticmethod
    def _payload(content, finish_reason: str, **extra) -> dict:
        return {
            "choices": [
                {"message": {"role": "assistant", "content": content, **extra},
                 "finish_reason": finish_reason}
            ]
        }

    def _complete(self, provider: OpenAICompatProvider, max_tokens: int = 4096) -> Completion:
        return provider.complete([Message(role="user", content="hi")], max_tokens=max_tokens)

    def test_budget_spent_entirely_on_reasoning_names_the_cause(self):
        provider = self._provider(self._payload("", "length", reasoning=self.THOUGHT))
        with self.assertRaises(ProviderBadResponse) as caught:
            self._complete(provider)
        message = str(caught.exception)
        self.assertIn("hidden reasoning", message)
        self.assertIn("reasoning_effort", message)

    def test_the_deepseek_and_vllm_spelling_is_recognized_too(self):
        provider = self._provider(self._payload("", "length", reasoning_content=self.THOUGHT))
        with self.assertRaises(ProviderBadResponse):
            self._complete(provider)

    def test_reasoning_nested_under_a_dict_is_recognized_too(self):
        provider = self._provider(self._payload("", "length", reasoning={"content": self.THOUGHT}))
        with self.assertRaises(ProviderBadResponse):
            self._complete(provider)

    def test_an_empty_reply_that_was_not_truncated_still_passes_through(self):
        """Only the combination is diagnosable. A model that simply had nothing
        to say is a different problem, and mislabelling it would send the reader
        to raise a limit that was never reached."""
        provider = self._provider(self._payload("", "stop", reasoning=self.THOUGHT))
        self.assertEqual(self._complete(provider).text, "")

    def test_truncation_with_real_content_is_left_to_the_callers(self):
        """Half an answer is the case every role already refuses explicitly."""
        provider = self._provider(self._payload("partial answ", "length"))
        completion = self._complete(provider)
        self.assertTrue(completion.truncated)
        self.assertEqual(completion.text, "partial answ")

    def test_a_thinking_model_that_finishes_returns_its_answer(self):
        provider = self._provider(self._payload('{"ok":1}', "stop", reasoning=self.THOUGHT))
        self.assertEqual(self._complete(provider).text, '{"ok":1}')


class TestPlannerOutputBudget(unittest.TestCase):
    """`forge plan` asks for one of the longest replies in the system. A fixed
    ceiling is too small for a model that thinks before it writes, and a reply
    cut off mid-JSON has to say so rather than read as nonsense."""

    class _Planner(Provider):
        kind = "stub"

        def __init__(self, completion: Completion, max_output: int):
            super().__init__("stub", {})
            self._completion, self._max_output = completion, max_output
            self.asked_for = 0

        def complete(self, messages, *, max_tokens, temperature=0.2, timeout=600):
            self.asked_for = max_tokens
            return self._completion

        def capabilities(self) -> Capabilities:
            return Capabilities(context_window=131072, max_output_tokens=self._max_output)

    REPLY = '{"tickets":[{"id":"TT-001","title":"t","spec":"s","criteria":["c"],"files":["a.py"]}]}'

    def test_the_planner_gets_what_the_model_can_actually_emit(self):
        planner = self._Planner(Completion(text=self.REPLY, usage=Usage()), 32000)
        plan_with_model(planner, "spec")
        self.assertEqual(planner.asked_for, 32000)

    def test_a_small_ceiling_never_drops_below_the_old_floor(self):
        planner = self._Planner(Completion(text=self.REPLY, usage=Usage()), 1024)
        plan_with_model(planner, "spec")
        self.assertEqual(planner.asked_for, 8192)

    def test_an_explicit_budget_still_wins(self):
        planner = self._Planner(Completion(text=self.REPLY, usage=Usage()), 32000)
        plan_with_model(planner, "spec", max_tokens=2048)
        self.assertEqual(planner.asked_for, 2048)

    def test_a_truncated_plan_blames_the_output_budget_not_the_json(self):
        cut = Completion(text='{"tickets":[{"id":"TT-0', usage=Usage(), finish_reason="length")
        planner = self._Planner(cut, 8192)
        with self.assertRaises(ValueError) as caught:
            plan_with_model(planner, "spec")
        self.assertIn("ran out of output room", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
