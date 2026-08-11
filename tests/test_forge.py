"""Tests for the parts where a silent wrong answer is expensive.

Scope enforcement, reset-time parsing, and plan parsing are all places where a
bug does not raise — it just lets the loop do the wrong thing for hours. Those
get tests; the HTTP adapters do not, since exercising them needs a live model.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from forge.artifacts import Artifacts
from forge.budget import BudgetGate, RateLimitPolicy
from forge.config import Config, LoopSettings, UISettings
from forge.ingest import looks_like_plan, parse_plan, tickets_from_json
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
from forge.providers.base import Capabilities, Completion, Provider, Usage
from forge.providers.openai_compat import OpenAICompatProvider
from forge.providers.claude_cli import (
    _LIMIT_PATTERN,
    _SPEND_LIMIT_PATTERN,
    parse_reset_time,
)
from forge.state import Store, Ticket
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

    def test_a_test_file_that_predates_the_ticket_is_never_deleted(self):
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1
        (root / "tests").mkdir()
        (root / "tests" / "tt_001_test.rs").write_text("#[test]\nfn old() {}\n", "utf-8")
        orch._call = _replies(
            "src/game.rs\n```rust\npub fn go() {}\n```",
            "tests/tt_001_test.rs\n```rust\n#[test]\nfn a() {}\n```",
            "REJECT\nno",
        )

        orch._work_ticket(
            run_id,
            Ticket("TT-001", allowed_files=["src/game.rs"], criteria=["go() exists"]),
        )

        self.assertTrue((root / "tests" / "tt_001_test.rs").exists())


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


if __name__ == "__main__":
    unittest.main()
