"""Tests for the parts where a silent wrong answer is expensive.

Scope enforcement, reset-time parsing, and plan parsing are all places where a
bug does not raise — it just lets the loop do the wrong thing for hours. Those
get tests; the HTTP adapters do not, since exercising them needs a live model.

    python -m unittest discover tests
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

from forge import cli, evidence, modelfiles, respec
from forge.artifacts import Artifacts
from forge.budget import BudgetGate, ContextOverflow, RateLimitPolicy
from forge.config import ROLES, Config, ConfigError, LoopSettings, UISettings
from forge.ingest import (
    derive_needs,
    graph_problems,
    looks_like_plan,
    parse_plan,
    plan_decisions,
    plan_with_model,
    render_ticket,
    shared_file_conflicts,
    tickets_from_json,
)
from forge.ingest import ingest as ingest_document
from forge.respec import _merge_criteria, _refuse_protocol_edits
from forge.loop import _DROPPABLE_HEADINGS, _droppable, Orchestrator, StepResult
from forge.patch import (
    describe_unparsed,
    duplicate_paths,
    enforce_scope,
    foreign_bindings,
    is_safe_path,
    matches_any,
    normalize_path,
    parse_output,
)
from forge.failures import distill, errors_naming, signatures
from forge.prompts import (
    bug_prompt,
    locate_prompt,
    parse_bug,
    parse_locate,
    repro_prompt,
    build_prompt,
    parse_respec,
    parse_verdict,
    respec_prompt,
    review_prompt,
    strip_prompt_echo,
    tests_prompt,
)
from forge.providers import build_provider
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
    TICKET_BLOCKED,
    TICKET_BUG,
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

    def test_a_path_under_a_markdown_heading_is_still_a_path(self):
        # Thirteen replies across four cycles, all `#### src/game.rs` above a
        # correct implementation. Telling the model its path line was missing
        # changed nothing — it does not experience that line as missing.
        parsed = parse_output("#### src/game.rs\n```rust\npub struct Game;\n```")

        self.assertEqual([e.path for e in parsed.edits], ["src/game.rs"])
        self.assertEqual(parsed.edits[0].content, "pub struct Game;\n")

    def test_a_bold_path_is_still_a_path(self):
        parsed = parse_output("**src/game.rs**\n```rust\nx\n```")
        self.assertEqual([e.path for e in parsed.edits], ["src/game.rs"])

    def test_a_heading_that_is_not_a_path_is_not_one(self):
        # The widened rule must not turn ordinary prose into a file.
        parsed = parse_output("### Using build.ps1 (Windows)\n```powershell\nx\n```")
        self.assertEqual(parsed.edits, [])

    def _tt_006_response(self):
        """Verbatim shape of the response that broke TT-006.

        The README is wrapped in three backticks and contains three-backtick
        fences of its own, so its block ends at the first one. Its remaining
        prose — a path on its own line ahead of a fence — then parses as one
        more file, and `apply_edits` is last-write-wins, so that fragment
        overwrote the real `build.sh`.
        """
        f = "`" * 3
        return (
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

    def test_a_file_cut_short_by_its_own_fence_never_becomes_an_edit(self):
        parsed = parse_output(self._tt_006_response())

        # Only the file whose block genuinely closed where it was meant to.
        self.assertEqual([e.path for e in parsed.edits], ["build.sh"])
        self.assertEqual(parsed.truncated, ["README.md", "./build.sh"])

    def test_the_phantom_alone_is_still_refused(self):
        # The case that actually reached disk. Told about the duplicate, the
        # model restructured until only the invented block parsed — nothing
        # collided, nothing was caught, and `build.sh` came back as 57 bytes of
        # somebody else's markdown while two files were never written at all.
        f = "`" * 3
        body = (
            f"README.md\n{f}\n"
            "# Tetris\n\n"
            f"{f}sh\nrustup target add wasm32-unknown-unknown\n{f}\n\n"
            "### PowerShell\n\n"
            f"{f}powershell\n.\\build.ps1\n{f}\n"
        )
        parsed = parse_output(body)

        self.assertEqual(parsed.edits, [])
        self.assertIn("README.md", parsed.truncated)

    def test_a_file_whose_fences_are_shorter_than_its_wrapper_is_kept(self):
        # The shape the executor is asked for, and the one the check must not
        # flag: nothing inside can close a fence longer than itself.
        inner, outer = "`" * 3, "`" * 4
        readme = f"# Title\n\n{inner}sh\n./build.sh\n{inner}\n\n## More\n\ndone\n"
        parsed = parse_output(f"README.md\n{outer}md\n{readme}{outer}\n")

        self.assertEqual(parsed.truncated, [])
        self.assertEqual(parsed.edits[0].content, readme)

    def test_distinct_paths_are_not_duplicates(self):
        fence = "`" * 3
        parsed = parse_output(f"a.rs\n{fence}\nx\n{fence}\n\nb.rs\n{fence}\ny\n{fence}\n")
        self.assertEqual(duplicate_paths(parsed), [])

    def test_duplicate_detection_sees_through_path_spelling(self):
        fence = "`" * 3
        parsed = parse_output(f"build.sh\n{fence}\nx\n{fence}\n\n./build.sh\n{fence}\ny\n{fence}\n")
        self.assertEqual(duplicate_paths(parsed), ["build.sh"])


class TestUnparsedOutput(unittest.TestCase):
    """Why a reply produced no edits.

    "No file edits" is true of a model that decided there was nothing to do, of
    one that wrote a whole file and forgot the path line, and of one that put
    the path line inside the fence. Reporting all three identically sent a
    respec looking for defects in the spec when the fix was a header line.
    """

    def test_a_fenced_block_with_no_path_line_says_so(self):
        # TT-002: five consecutive attempts, each carrying a complete and valid
        # src/board.rs, none of them named.
        fence = "`" * 3
        message = describe_unparsed(f"{fence}rust\nfn main() {{}}\n{fence}\n")
        self.assertIn("no file path", message)

    def test_a_path_line_inside_the_fence_says_so(self):
        fence = "`" * 3
        message = describe_unparsed(
            f"{fence}\nbuild.sh\n#!/usr/bin/env sh\nset -eu\n{fence}\n"
        )
        self.assertIn("inside the fenced block", message)

    def test_a_path_line_with_unfenced_contents_says_so(self):
        message = describe_unparsed(
            "build.sh\n#!/usr/bin/env sh\nset -eu\n\nbuild.ps1\nCopy-Item a b\n"
        )
        self.assertIn("did not fence their contents", message)

    def test_a_reply_carrying_no_file_content_raises_no_complaint(self):
        # Not a formatting failure — there is nothing here that was meant to be
        # a file. The caller judges it against the criteria instead of spending
        # an attempt on it.
        self.assertEqual(
            describe_unparsed("I have reviewed the files and they look correct."), ""
        )

    def test_a_reply_that_parsed_is_not_second_guessed(self):
        fence = "`" * 3
        self.assertEqual(describe_unparsed(f"a.rs\n{fence}\nx\n{fence}\n"), "")


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

    def test_a_bullet_that_is_one_code_span_loses_its_backticks(self):
        # What the stripping is for: a file path is written `src/piece.rs` and
        # the backticks are punctuation, not part of the name.
        self.assertEqual(parse_plan(self.PLAN)[0].allowed_files, ["a.py"])

    def test_a_criterion_that_opens_and_closes_with_code_spans_keeps_both(self):
        # Taking one character off each end of a criterion that begins and ends
        # with inline code removes the *opening* backtick of the first span and
        # the *closing* backtick of the last, leaving unbalanced markdown in
        # every prompt that renders it — and inviting the planner to "reword"
        # the criterion at respec time by repairing the punctuation, which the
        # provenance check then reads as tampering with a human's contract.
        plan = self.PLAN.replace(
            "- returns 1 for input 0",
            "- `piece::WIDTH` is 10 and `piece::HEIGHT` is 20",
        )
        self.assertEqual(
            parse_plan(plan)[0].criteria,
            ["`piece::WIDTH` is 10 and `piece::HEIGHT` is 20"],
        )

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


class TestTheDashboardSharesTheStoreWithTheLoop(unittest.TestCase):
    """`forge go` hands one Store to the dashboard, which serves every request
    on a thread of its own. Interleaving those reads with the loop's writes on
    a single sqlite3 connection is undefined use of the driver whatever
    `check_same_thread` says — it eventually raised `bad parameter or other API
    misuse` and killed a run fifteen retry cycles in."""

    def test_reads_from_other_threads_do_not_disturb_the_writer(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1")])
        failures: list[Exception] = []
        stop = threading.Event()

        def read():
            try:
                while not stop.is_set():
                    store.list_tickets(run_id)
                    store.events_after(0)
                    store.ticket_counts(run_id)
            except Exception as exc:  # noqa: BLE001 - the point of the test
                failures.append(exc)

        readers = [threading.Thread(target=read, daemon=True) for _ in range(4)]
        for reader in readers:
            reader.start()
        try:
            for index in range(300):
                store.log(run_id, f"event {index}", kind="ticket")
                step = store.start_step(run_id, "T-1", "build")
                store.end_step(step, "ok", "detail")
        finally:
            stop.set()
            for reader in readers:
                reader.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertEqual(len(store.events_after(0, limit=1000)), 300)


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
            # Pre-flight is startup behaviour and these endpoints are stubs; the
            # cycle counting under test begins after it.
            loop=LoopSettings(
                **{"respec_on_retry": False, "preflight": False, **loop_settings}
            ),
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

    def test_a_skipped_ticket_is_requeued_but_not_respecced(self):
        # It never ran, so the only evidence is which dependency was missing.
        # Handed that under "what happened, oldest attempt first", the planner
        # rewrote three untried specs twice each — one acquiring a fabricated
        # xorshift constant, another a `lib.rs must contain exactly` clause that
        # contradicted the two tickets after it.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[
                Ticket("T-1", status="failed", spec="old spec", attempts=3),
                Ticket("T-2", status="skipped", spec="untouched", needs=["T-1"]),
            ],
            retry_cycles=1,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: the spec never said which file")

        seen: list[str] = []

        def call(_run_id, _role, messages, **_kwargs):
            asked = "\n".join(m.content for m in messages)
            seen.append("T-2" if "T-2" in asked else "T-1")
            return Completion(text=json.dumps({"spec": "new spec"}), usage=Usage())

        orchestrator._call = call
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), True)

        self.assertEqual(seen, ["T-1"])
        by_id = {t.ticket_id: t for t in store.list_tickets(run_id)}
        self.assertEqual(by_id["T-2"].spec, "untouched")
        # Still requeued — it has to run once its dependency lands.
        self.assertEqual(by_id["T-2"].status, "pending")

    def test_revising_a_never_run_ticket_does_not_buy_another_cycle(self):
        # The brake at `not revised` exists to stop a cycle that would hand the
        # executor an unchanged ticket. Revisions to tickets that never ran used
        # to satisfy it, so two further cycles were bought on work nothing had
        # learned anything from.
        orchestrator, store, run_id = self._orchestrator(
            tickets=[
                Ticket("T-1", status="failed", spec="old spec", attempts=3),
                Ticket("T-2", status="skipped", spec="untouched", needs=["T-1"]),
            ],
            retry_cycles=2,
            respec_on_retry=True,
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")

        # The planner says the failing ticket is right as written.
        orchestrator._call = lambda *_a, **_k: Completion(
            text=json.dumps({"spec": "old spec"}), usage=Usage()
        )
        self.assertIs(orchestrator._retry_cycle(run_id, "blocked"), False)

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


class TestTheSampleConfigStaysHonest(unittest.TestCase):
    """`templates/config.sample.json` is what a person copies. A sample that
    does not load is worse than none — it sends the reader hunting through
    their own edits for a mistake the file shipped with."""

    SAMPLE = Path(__file__).resolve().parents[1] / "templates" / "config.sample.json"

    def _loaded(self) -> Config:
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            self.SAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return Config.load(root)

    def test_it_loads_and_validates(self):
        config = self._loaded()
        self.assertEqual(sorted(config.roles), sorted(ROLES))
        self.assertEqual(config.record_role, "reviewer")

    def test_every_declared_model_can_be_built(self):
        # Including the one no role uses: it is there to be swapped in, and a
        # sample that only works until you do that is a trap.
        config = self._loaded()
        for name in config.models:
            provider = build_provider(name, config.model_block(name))
            self.assertTrue(provider.kind)

    def test_the_spend_caps_are_real_policies(self):
        config = self._loaded()
        policies = config.rate_limit_policies()
        self.assertFalse(policies["claude"].is_empty)
        self.assertFalse(policies["api"].is_empty)

    def test_it_names_every_loop_setting(self):
        # The guard that keeps the sample and CONFIG.md from rotting: a knob
        # added to LoopSettings without a line here fails this test rather
        # than quietly going undocumented.
        written = json.loads(self.SAMPLE.read_text(encoding="utf-8"))["loop"]
        expected = {
            _camel(field.name) for field in dataclasses.fields(LoopSettings)
        }
        self.assertEqual(set(written), expected)

    def test_the_reference_documents_every_loop_setting(self):
        reference = (
            Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md"
        ).read_text(encoding="utf-8")

        missing = [
            _camel(field.name)
            for field in dataclasses.fields(LoopSettings)
            if f"`{_camel(field.name)}`" not in reference
        ]
        self.assertEqual(missing, [], "undocumented loop settings")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


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

    def test_the_conversational_executor_is_off_by_default(self):
        self.assertEqual(self._load({}).loop.executor_turns, 0)

    def test_the_turn_count_is_read_and_survives_a_write(self):
        config = self._load({"executorTurns": 2})
        self.assertEqual(config.loop.executor_turns, 2)
        config.write()
        self.assertEqual(Config.load(config.root).loop.executor_turns, 2)

    def test_a_negative_turn_count_is_rejected(self):
        # Nothing sensible to mean by it, and clamping would hide the typo.
        with self.assertRaises(ConfigError):
            self._load({"executorTurns": -1})

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
        # Grouped under headings rather than tagged per line: the planner is
        # asked to copy these back verbatim, and a per-line tag is part of the
        # line it copies.
        self.assertIn("you may not change these", section[:plan_line])
        self.assertIn("you may revise or retire these", section[plan_line:added_line])
        self.assertNotIn("you may not change", section[added_line:])

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

    def test_a_new_criterion_is_refused_while_the_criteria_are_locked(self):
        # This used to be allowed, on the reasoning that a plan can specify
        # something in prose and state no criterion for it, and that adding one
        # cannot lower the bar. Lowering was never the failure. Respec runs on
        # a ticket that has just exhausted its attempts, so the bar only ever
        # rose: one ticket went from nine criteria to sixteen across six
        # cycles, and what blocked it at the end was invented in cycle four.
        # An under-specified plan is now reported instead — see the refusal
        # message — and `respecCriteria: true` restores the old behaviour.
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        result = respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(
                spec="new", criteria=["the plan's bar", "clearing one line scores 100"]
            ),
            budget=1024,
        )

        self.assertEqual(store.list_tickets(run_id)[0].criteria, ["the plan's bar"])
        self.assertEqual(result.minted_criteria, ["clearing one line scores 100"])

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
            ["a criterion the human wrote by hand"],
        )
        # The point of this test: the plan's own removed criterion stays
        # removed. The addition is refused separately, and that is the
        # ratchet rule rather than this one.
        self.assertEqual(result.refused_criteria, [])
        self.assertEqual(result.minted_criteria, ["and one addition"])

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
            run_id,
            [Ticket("T-1", spec="old", criteria=["yields [6,3,5,7,4]"], attempts=3)],
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

    def test_the_context_is_anchored_too(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", spec="s", context="the plan's rule")])

        ticket = store.list_tickets(run_id)[0]
        self.assertEqual(ticket.original_context, "the plan's rule")

        ticket.context = "something a revision wrote"
        ticket.original_context = "a rewritten history"
        store.update_ticket(run_id, ticket)
        self.assertEqual(
            store.list_tickets(run_id)[0].original_context, "the plan's rule"
        )

    def test_a_context_only_change_counts_as_drift(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1", spec="s", context="the plan's rule")])

        ticket = store.list_tickets(run_id)[0]
        self.assertFalse(ticket.drifted)
        ticket.context = "a revision's paragraph"
        self.assertTrue(ticket.drifted)


class TestThePlansContextSurvivesARespec(unittest.TestCase):
    """`context` was the one plan-authored field with no provenance rule.

    Respec returns a whole new string, so the plan's paragraph was simply gone:
    in one run five of six tickets lost the executor's bare-path-line rule and
    the do-not-write-tests rule to a sentence of the planner's own reasoning
    about why scaffold files keep being omitted. The system prompt still
    carried both rules, so this was degradation rather than deletion — but the
    redundancy holding a weak local model to format is what got deleted.
    """

    PLAN_CONTEXT = "Write each file as a bare path line, then the contents."

    def _store(self, context=PLAN_CONTEXT):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec="old", context=context, status="failed")]
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _reply(self, **payload):
        def call(_messages, _budget):
            return Completion(text=json.dumps(payload), usage=Usage())

        return call

    def test_respec_cannot_delete_the_plans_context(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="a revised spec", context="The board is 10x20."),
            budget=1024,
        )

        stored = store.list_tickets(run_id)[0].context
        self.assertIn(self.PLAN_CONTEXT, stored)
        self.assertIn("The board is 10x20", stored)

    def test_a_revision_that_kept_the_paragraph_is_not_given_it_twice(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(
                spec="a revised spec",
                context=f"{self.PLAN_CONTEXT}\n\nThe board is 10x20.",
            ),
            budget=1024,
        )

        stored = store.list_tickets(run_id)[0].context
        self.assertEqual(stored.count(self.PLAN_CONTEXT), 1)

    def test_the_restoration_reaches_the_run_log(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="a revised spec", context="only mine"),
            budget=1024,
        )

        messages = [row["message"] for row in store.events_after(0)]
        self.assertTrue(
            any("put back" in message for message in messages),
            "a context the loop restored must be visible to a human",
        )

    def test_a_ticket_the_plan_gave_no_context_is_left_to_the_planner(self):
        # Nothing to protect, so nothing is prepended — the planner's paragraph
        # stands alone rather than being appended to an empty anchor.
        store, run_id = self._store(context="")
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(spec="a revised spec", context="the whole story"),
            budget=1024,
        )

        self.assertEqual(store.list_tickets(run_id)[0].context, "the whole story")


class TestADecisionInSpecProseIsProtected(unittest.TestCase):
    """A plan can state a decision as well as a requirement, and the criteria
    ratchet never covered it.

    One plan opened with "Design decisions, already made — implement them, do
    not revisit them", and one of them was that randomness is a xorshift32.
    Respec observed that the criteria only require determinism and revised the
    spec to "an internal deterministic PRNG". A Numerical Recipes LCG shipped,
    every criterion passed, and the reviewer accepted it correctly because no
    criterion named xorshift. The ticket was green and the decision was gone.
    """

    DECISION = "Randomness is a xorshift32 seeded from JavaScript."
    SPEC = (
        "Implement Game::tick.\n"
        "\n"
        "### Design decisions, already made\n"
        "\n"
        f"{DECISION}\n"
    )

    def _store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec=self.SPEC, criteria=["ticks"], status="failed")]
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _reply(self, **payload):
        def call(_messages, _budget):
            return Completion(text=json.dumps(payload), usage=Usage())

        return call

    def test_a_marked_decision_is_read_out_of_the_plans_prose(self):
        self.assertEqual(plan_decisions(self.SPEC), [self.DECISION])

    def test_a_line_may_mark_itself_where_there_is_no_room_for_a_section(self):
        found = plan_decisions(
            "- **Decision:** the store is SQLite, not Postgres.\n"
            "The board is ten columns wide.\n"
        )
        self.assertEqual(found, ["- **Decision:** the store is SQLite, not Postgres."])

    def test_a_spec_revision_that_drops_a_decision_is_refused(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store,
            run_id,
            ticket,
            call=self._reply(
                spec="Implement Game::tick with an internal deterministic PRNG.",
                context="the executor should start here",
            ),
            budget=1024,
        )

        stored = store.list_tickets(run_id)[0]
        self.assertEqual(stored.spec, self.SPEC)
        # Only the spec is refused; the rest of the revision still lands.
        self.assertIn("the executor should start here", stored.context)

    def test_the_refusal_reaches_the_run_log(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store, run_id, ticket, call=self._reply(spec="a deterministic PRNG"), budget=1024
        )

        messages = [row["message"] for row in store.events_after(0)]
        self.assertTrue(any("marked as settled" in message for message in messages))

    def test_a_revision_that_keeps_the_decision_goes_through(self):
        store, run_id = self._store()
        ticket = store.list_tickets(run_id)[0]
        revised = f"Implement Game::tick and Game::lock.\n\n{self.DECISION}"

        respec.revise(store, run_id, ticket, call=self._reply(spec=revised), budget=1024)

        self.assertEqual(store.list_tickets(run_id)[0].spec, revised)

    def test_unmarked_prose_stays_freely_revisable(self):
        # This protects what the plan labelled, not prose in general. A spec
        # with no decisions section is revised exactly as before.
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id, [Ticket("T-1", spec="Implement Game::tick.", status="failed")]
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        ticket = store.list_tickets(run_id)[0]

        respec.revise(
            store, run_id, ticket, call=self._reply(spec="Implement Game::step."), budget=1024
        )

        self.assertEqual(store.list_tickets(run_id)[0].spec, "Implement Game::step.")


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


class TestUiHostAndPortFlags(unittest.TestCase):
    """A run binds its dashboard at startup and will not rebind, so watching an
    in-progress run from another machine means a second dashboard on another
    address. Overrides apply to the invocation only — nothing is written back,
    because a flag reached for once should not quietly change every later run."""

    def _args(self, root, **overrides):
        parsed = cli.build_parser().parse_args(["--root", str(root), "ui"])
        for key, value in overrides.items():
            setattr(parsed, key, value)
        return parsed

    def _project(self):
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
                    "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                    "ui": {"host": "127.0.0.1", "port": 8799},
                }
            ),
            encoding="utf-8",
        )
        return root

    def _bound(self, root, **overrides):
        """Run cmd_ui far enough to see what it would bind, then stop."""
        seen = {}

        def fake_serve(config, _store):
            seen["host"] = config.ui.host
            seen["port"] = config.ui.port
            raise KeyboardInterrupt  # unwinds before the idle loop

        with unittest.mock.patch.object(cli.ui_server, "serve", fake_serve):
            try:
                cli.cmd_ui(self._args(root, **overrides))
            except KeyboardInterrupt:
                pass
        return seen

    def test_the_flags_exist_and_default_to_none(self):
        parsed = cli.build_parser().parse_args(["ui"])
        self.assertIsNone(parsed.host)
        self.assertIsNone(parsed.port)

    def test_without_flags_config_decides(self):
        root = self._project()
        self.assertEqual(self._bound(root), {"host": "127.0.0.1", "port": 8799})

    def test_host_overrides_config(self):
        root = self._project()
        self.assertEqual(self._bound(root, host="0.0.0.0")["host"], "0.0.0.0")

    def test_port_overrides_config(self):
        root = self._project()
        self.assertEqual(self._bound(root, port=8800)["port"], 8800)

    def test_the_override_is_not_written_back(self):
        root = self._project()
        self._bound(root, host="0.0.0.0", port=8800)

        saved = json.loads((root / ".hybridforge" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["ui"], {"host": "127.0.0.1", "port": 8799})

    def test_an_overridden_bind_still_warns(self):
        """The warning reads `config.ui`, so an override must reach it before
        `serve` — otherwise `--host 0.0.0.0` exposes the stop button silently."""
        config = Config(root=Path("."))
        config.ui = UISettings(host="0.0.0.0", port=8800)

        self.assertIn("NO authentication", exposure_warning(config))
        self.assertIn("8800", exposure_warning(config))

    def test_a_taken_port_explains_itself(self):
        root = self._project()

        def refuse(_config, _store):
            raise OSError(48, "Address already in use")

        with unittest.mock.patch.object(cli.ui_server, "serve", refuse):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_ui(self._args(root, port=8799))

        self.assertIn("--port", str(caught.exception))


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


class TestTheBaselineIsAnchoredToTheTicket(unittest.TestCase):
    """A retry cycle inherits the previous cycle's work in the tree, because
    autoCommit is off and nothing reverts a failed ticket. Re-snapshotting per
    run therefore measures the ticket against its own output: the executor
    rewrites it byte for byte, git reports nothing, and the reviewer is asked
    to approve a change it cannot see. One run drew twenty-eight rejections
    across nine cycles that way, on an implementation that was fine."""

    def _repo(self):
        root = Path(tempfile.mkdtemp())
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t.local"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=False)
        subprocess.run(
            ["git", "commit", "-qm", "initial"], cwd=root, capture_output=True, check=False
        )
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
        )
        orch = Orchestrator(config, Store(root / "t.db"))
        return orch, root, orch.store.create_run("g")

    def _capture(self, orch, run_id, ticket):
        """What `_run_ticket` does at the top of each run."""
        if not ticket.baseline_tree:
            ticket.baseline_tree = orch._snapshot()
            orch.store.update_ticket(run_id, ticket)
        return ticket.baseline_tree

    def test_a_retry_still_sees_the_work_the_first_cycle_wrote(self):
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["web/main.js"])
        orch.store.add_tickets(run_id, [ticket])

        self._capture(orch, run_id, ticket)
        (root / "web").mkdir()
        (root / "web" / "main.js").write_text("export const go = 1;\n", encoding="utf-8")

        # Cycle two: same ticket, file already on disk, rewritten identically.
        reloaded = orch.store.list_tickets(run_id)[0]
        baseline = self._capture(orch, run_id, reloaded)
        diff = orch._diff(baseline, reloaded.allowed_files)

        self.assertIn("web/main.js", diff)
        self.assertIn("export const go", diff)

    def test_re_snapshotting_per_run_is_what_produced_the_empty_diff(self):
        """The behavior being replaced, asserted so the fix cannot silently
        regress to it."""
        orch, root, _run_id = self._repo()
        (root / "web").mkdir()
        (root / "web" / "main.js").write_text("export const go = 1;\n", encoding="utf-8")

        fresh = orch._snapshot()  # what the old code did on every retry

        self.assertEqual(orch._diff(fresh, ["web/main.js"]).strip(), "")

    def test_another_tickets_work_is_excluded_even_across_cycles(self):
        """The pinned baseline stops time from isolating the ticket, so the
        path filter has to. Without it the reviewer sees work its executor did
        not do and rejects it as out of scope."""
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["web/main.js"])
        orch.store.add_tickets(run_id, [ticket])
        baseline = self._capture(orch, run_id, ticket)

        (root / "web").mkdir()
        (root / "web" / "main.js").write_text("mine = 1\n", encoding="utf-8")
        (root / "build.sh").write_text("# another ticket landed this\n", encoding="utf-8")

        diff = orch._diff(baseline, ticket.allowed_files)

        self.assertIn("web/main.js", diff)
        self.assertNotIn("build.sh", diff)

    def test_the_test_file_is_in_scope_even_though_the_plan_never_listed_it(self):
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["src/a.rs"])
        orch.store.add_tickets(run_id, [ticket])
        baseline = self._capture(orch, run_id, ticket)

        (root / "src").mkdir()
        (root / "src" / "a.rs").write_text("fn a() {}\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "tt_005_test.rs").write_text("#[test]\nfn t() {}\n", encoding="utf-8")

        diff = orch._diff(baseline, [*ticket.allowed_files, "tests/tt_005_test.rs"])

        self.assertIn("src/a.rs", diff)
        self.assertIn("tt_005_test.rs", diff)

    def test_a_glob_in_scope_falls_back_to_the_unscoped_diff(self):
        """A glob is a scope rule, not a filename. Handing it to git as a
        pathspec would apply git's matching rules and show the reviewer less
        than the ticket changed, which is worse than showing it more."""
        orch, root, run_id = self._repo()
        baseline = orch._snapshot()
        (root / "src").mkdir()
        (root / "src" / "deep.rs").write_text("fn d() {}\n", encoding="utf-8")

        diff = orch._diff(baseline, ["src/**/*.rs"])

        self.assertIn("deep.rs", diff)

    def test_an_unusable_baseline_degrades_rather_than_failing(self):
        orch, root, _run_id = self._repo()
        (root / "later.txt").write_text("x\n", encoding="utf-8")

        diff = orch._diff("0" * 40, ["later.txt"])

        self.assertIn("later.txt", diff)

    def test_the_baseline_is_captured_once_and_then_reused(self):
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["a.txt"])
        orch.store.add_tickets(run_id, [ticket])

        first = self._capture(orch, run_id, ticket)
        (root / "a.txt").write_text("changed\n", encoding="utf-8")
        second = self._capture(orch, run_id, orch.store.list_tickets(run_id)[0])

        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_the_unchanged_fallback_has_nothing_left_to_report(self):
        """The signal that the cause is gone rather than papered over. The
        contents-instead-of-diff section still exists for a file rewritten
        identically inside one cycle; a retry should no longer need it."""
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["web/main.js"])
        orch.store.add_tickets(run_id, [ticket])
        baseline = self._capture(orch, run_id, ticket)

        (root / "web").mkdir()
        (root / "web" / "main.js").write_text("export const go = 1;\n", encoding="utf-8")
        # Cycle two rewrites it byte for byte.
        reloaded = orch.store.list_tickets(run_id)[0]
        diff = orch._diff(self._capture(orch, run_id, reloaded), reloaded.allowed_files)

        self.assertEqual(baseline, reloaded.baseline_tree)
        self.assertEqual(orch._written_but_unchanged(["web/main.js"], diff), {})

    def test_it_survives_a_restart(self):
        """The baseline is persisted state now, so a daemon killed mid-run must
        resume against the same starting point rather than the tree it wakes to."""
        orch, root, run_id = self._repo()
        ticket = Ticket("TT-005", allowed_files=["a.txt"])
        orch.store.add_tickets(run_id, [ticket])
        captured = self._capture(orch, run_id, ticket)

        reopened = Store(root / "t.db")
        self.assertEqual(reopened.list_tickets(run_id)[0].baseline_tree, captured)


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

    def test_a_file_cut_short_by_its_own_fence_never_reaches_disk(self):
        # TT-006 in full. `build.sh` was on disk and correct; the README's block
        # closed inside itself, and the fragment parsed out of its remaining
        # prose was written over the script. The step logged `apply ok`, the
        # file came back as 57 bytes of markdown, and the two files the ticket
        # actually needed were never written.
        orch, root, run_id = self._orchestrator()
        script = "#!/usr/bin/env sh\nset -eu\n"
        (root / "build.sh").write_text(script, encoding="utf-8")
        f = "`" * 3
        orch._call = lambda *a, **k: self._completion(
            f"README.md\n{f}\n# Tetris\n\n"
            f"{f}sh\nrustup target add wasm32-unknown-unknown\n{f}\n\n"
            f"### PowerShell\n\n{f}powershell\n.\\build.ps1\n{f}\n",
            "stop",
        )

        result = orch._attempt(
            run_id, Ticket("T-1", allowed_files=["build.sh", "README.md"]), ""
        )

        self.assertFalse(result.ok)
        self.assertIn("LONGER fence", result.detail)
        self.assertEqual((root / "build.sh").read_text(encoding="utf-8"), script)

    def test_the_files_that_parsed_cleanly_are_written_anyway(self):
        # The recovery path. One real response carried a correct build.sh and
        # build.ps1 beside a truncated README; refusing all three left the
        # corrupt build.sh already on disk with no way to be replaced, and the
        # ticket could not finish no matter what the executor sent.
        orch, root, run_id = self._orchestrator()
        (root / "build.sh").write_text("### stale markdown fragment\n", encoding="utf-8")
        f = "`" * 3
        orch._call = lambda *a, **k: self._completion(
            f"build.sh\n{f}sh\ncargo build --release\n{f}\n\n"
            f"build.ps1\n{f}powershell\ncargo build\n{f}\n\n"
            f"README.md\n{f}\n# Tetris\n\n{f}sh\nrustup target add wasm32\n{f}\n\n"
            f"### PowerShell\n\n{f}powershell\n.\\build.ps1\n{f}\n",
            "stop",
        )

        result = orch._attempt(
            run_id,
            Ticket("T-1", allowed_files=["build.sh", "build.ps1", "README.md"]),
            "",
        )

        # Incomplete, so the attempt still fails — but it made progress.
        self.assertFalse(result.ok)
        self.assertIn(
            "cargo build --release", (root / "build.sh").read_text(encoding="utf-8")
        )
        self.assertTrue((root / "build.ps1").exists())
        self.assertFalse((root / "README.md").exists())

    def test_the_failure_names_what_landed_and_what_did_not(self):
        orch, root, run_id = self._orchestrator()
        f = "`" * 3
        orch._call = lambda *a, **k: self._completion(
            f"build.sh\n{f}sh\ncargo build --release\n{f}\n\n"
            f"README.md\n{f}\n# T\n\n{f}sh\nx\n{f}\n\n## More\n\ndone\n{f}\n",
            "stop",
        )

        result = orch._attempt(
            run_id, Ticket("T-1", allowed_files=["build.sh", "README.md"]), ""
        )

        self.assertIn("README.md", result.detail)
        self.assertIn("LONGER fence", result.detail)
        self.assertIn("is on disk", result.detail)
        self.assertIn("build.sh", result.detail)

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

    def test_an_example_test_decides_the_directory(self):
        """Where the suite lives is the example's to say. Which language it is
        written in is not — see the test command tests below."""
        orch, _ = self._orchestrator(test_command="python -m pytest")
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


class TestRespecMayWidenScopeButNotTheGraph(unittest.TestCase):
    """Widening into a file another ticket writes is legal — a ticket is a
    testable unit, not a file lease. It is only *safe* once the pair is
    ordered: without the edge they race for the file and whichever runs second
    overwrites the first. Respec asks for the file; the backlog decides who
    goes first."""

    def _store(self, first_files=("src/game.rs",), second_files=("src/wasm.rs",)):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            [
                Ticket("T-1", status="failed", attempts=3, position=0,
                       allowed_files=list(first_files), spec="one"),
                Ticket("T-2", position=1, allowed_files=list(second_files), spec="two"),
            ],
        )
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _revise(self, store, run_id, **payload):
        def call(_messages, _budget):
            return Completion(text=json.dumps(payload), usage=Usage())

        ticket = store.list_tickets(run_id)[0]
        return respec.revise(store, run_id, ticket, call=call, budget=1024)

    def _by_id(self, store, run_id):
        return {t.ticket_id: t for t in store.list_tickets(run_id)}

    def test_taking_on_another_tickets_file_adds_the_ordering_edge(self):
        store, run_id = self._store()
        self._revise(
            store, run_id,
            spec="revised", allowed_files=["src/game.rs", "src/wasm.rs"],
        )

        after = self._by_id(store, run_id)
        # T-1 is position 0, so the later ticket is the one that waits.
        self.assertEqual(after["T-2"].needs, ["T-1"])
        self.assertEqual(after["T-1"].needs, [])

    def test_the_widening_is_still_applied(self):
        store, run_id = self._store()
        self._revise(
            store, run_id,
            spec="revised", allowed_files=["src/game.rs", "src/wasm.rs"],
        )

        self.assertIn("src/wasm.rs", self._by_id(store, run_id)["T-1"].allowed_files)

    def test_the_new_edge_is_reported(self):
        store, run_id = self._store()
        self._revise(
            store, run_id,
            spec="revised", allowed_files=["src/game.rs", "src/wasm.rs"],
        )

        messages = " ".join(e["message"] for e in store.events_after(0))
        self.assertIn("now waits for", messages)
        self.assertIn("src/wasm.rs", messages)

    def test_widening_into_nobodys_file_adds_no_edge(self):
        store, run_id = self._store()
        self._revise(
            store, run_id,
            spec="revised", allowed_files=["src/game.rs", "src/brand_new.rs"],
        )

        after = self._by_id(store, run_id)
        self.assertEqual(after["T-1"].needs, [])
        self.assertEqual(after["T-2"].needs, [])

    def test_an_existing_edge_is_not_duplicated_or_reversed(self):
        store, run_id = self._store()
        second = store.list_tickets(run_id)[1]
        second.needs = ["T-1"]
        store.update_ticket(run_id, second)

        self._revise(
            store, run_id,
            spec="revised", allowed_files=["src/game.rs", "src/wasm.rs"],
        )

        after = self._by_id(store, run_id)
        self.assertEqual(after["T-2"].needs, ["T-1"])
        self.assertEqual(after["T-1"].needs, [])

    def test_respec_may_not_edit_the_graph_itself(self):
        """The planner sees one ticket and why it failed. It cannot see the
        file conflict on the other side of an edge, so dropping one would let
        two tickets race for a file the backlog had already ordered."""
        store, run_id = self._store()
        first = store.list_tickets(run_id)[0]
        first.needs = ["T-2"]
        store.update_ticket(run_id, first)

        self._revise(store, run_id, spec="revised", needs=[])

        self.assertEqual(self._by_id(store, run_id)["T-1"].needs, ["T-2"])

    def test_the_edge_counts_as_a_revision(self):
        """A cycle whose respec changed nothing ends the run, so an ordering
        the planner caused has to register as a change."""
        store, run_id = self._store()
        result = self._revise(
            store, run_id,
            spec="one", allowed_files=["src/game.rs", "src/wasm.rs"],
        )

        self.assertTrue(result.revised)


class TestRespecMayNotRaiseTheBar(unittest.TestCase):
    """Respec runs on a ticket that has just exhausted its attempts, and its
    job is to produce one the next attempt can satisfy. Adding criteria there
    cannot serve that, and left open the bar only ever rose: one ticket went
    from the plan's nine to sixteen across six cycles, and the criterion
    blocking it at the end had been invented two cycles earlier."""

    def _store(self, criteria=("a", "b"), added=()):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [
            Ticket("T-1", spec="old", criteria=list(criteria), status="failed",
                   original_criteria=list(criteria)),
        ])
        if added:
            ticket = store.list_tickets(run_id)[0]
            ticket.criteria = list(criteria) + list(added)
            store.update_ticket(run_id, ticket)
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _revise(self, store, run_id, criteria, locked=True):
        def call(_messages, _budget):
            return Completion(
                text=json.dumps({"spec": "revised", "criteria": criteria}), usage=Usage()
            )

        return respec.revise(
            store, run_id, store.list_tickets(run_id)[0],
            call=call, budget=1024, criteria_locked=locked,
        )

    def _criteria(self, store, run_id):
        return store.list_tickets(run_id)[0].criteria

    def test_an_invented_criterion_does_not_land(self):
        store, run_id = self._store()
        result = self._revise(store, run_id, ["a", "b", "and something new"])

        self.assertEqual(self._criteria(store, run_id), ["a", "b"])
        self.assertEqual(result.minted_criteria, ["and something new"])

    def test_the_plans_criteria_are_still_restored_when_dropped(self):
        store, run_id = self._store()
        result = self._revise(store, run_id, ["a"])

        self.assertEqual(self._criteria(store, run_id), ["a", "b"])
        self.assertEqual(result.refused_criteria, ["b"])

    def test_a_criterion_an_earlier_revision_added_can_still_be_retired(self):
        """The loop may take its own back — that is how a ticket already
        inflated returns to the plan's bar."""
        store, run_id = self._store(added=["invented earlier"])
        self._revise(store, run_id, ["a", "b"])

        self.assertEqual(self._criteria(store, run_id), ["a", "b"])

    def test_an_inflated_ticket_unwinds_on_the_next_revision(self):
        """Nine to sixteen, back to nine — no migration, no new command."""
        plan = [f"criterion {i}" for i in range(9)]
        store, run_id = self._store(
            criteria=plan, added=[f"minted {i}" for i in range(7)]
        )
        self.assertEqual(len(self._criteria(store, run_id)), 16)

        self._revise(store, run_id, plan)

        self.assertEqual(self._criteria(store, run_id), plan)

    def test_a_reword_of_a_plan_criterion_is_not_treated_as_new(self):
        """`0..7` against `0..=6` is how the duplication got past the earlier
        normalised matcher; as a proposal it must not count as a mint."""
        store, run_id = self._store(criteria=["`f()` returns 0"])
        result = self._revise(store, run_id, ["f() returns 0"])

        self.assertEqual(self._criteria(store, run_id), ["`f()` returns 0"])
        self.assertEqual(result.minted_criteria, [])

    def test_the_refusal_is_reported_with_what_it_refused(self):
        store, run_id = self._store()
        self._revise(store, run_id, ["a", "b", "a bar nobody asked for"])

        messages = " ".join(e["message"] for e in store.events_after(0))
        self.assertIn("the plan states nowhere", messages)
        self.assertIn("a bar nobody asked for", messages)
        self.assertIn("a bar nobody asked for", messages)

    def test_unlocking_the_criteria_restores_the_old_behaviour(self):
        """`respecCriteria: true` is the escape hatch for anyone who wants it."""
        store, run_id = self._store()
        self._revise(store, run_id, ["a", "b", "and something new"], locked=False)

        self.assertIn("and something new", self._criteria(store, run_id))

    def test_a_revision_that_only_mints_changes_nothing(self):
        """It must not count as a revision, or the retry cycle would keep
        going on the strength of a change that was refused."""
        store, run_id = self._store()

        def call(_messages, _budget):
            return Completion(
                text=json.dumps({"spec": "old", "criteria": ["a", "b", "new"]}),
                usage=Usage(),
            )

        result = respec.revise(
            store, run_id, store.list_tickets(run_id)[0], call=call, budget=1024
        )

        self.assertFalse(result.revised)


class TestACriterionTheSpecAlreadyStatesIsNotARatchet(unittest.TestCase):
    """The reviewer is given the spec and told to reject work that contradicts
    it, so the bar it enforces is spec ∪ criteria — while the ratchet tested
    novelty against the criteria alone. The planner was therefore forbidden
    from writing down a requirement the reviewer was required to enforce. One
    run spent three cycles on that gap over a single line: the planner proposed
    the `set -eu` criterion and was refused twice, the reviewer rejected the
    ticket for exactly that requirement twice, and the spec stated it all
    along."""

    SPEC = (
        "build.sh begins with #!/usr/bin/env sh then set -eu.\n"
        "src/lib.rs declares pub mod piece."
    )
    STATED = "build.sh must start with #!/usr/bin/env sh and set -eu"

    def _store(self, spec=SPEC, original_spec=""):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        run_id = store.create_run("goal")
        ticket = Ticket(
            "T-1",
            spec=spec,
            criteria=["the plan's bar"],
            original_criteria=["the plan's bar"],
            status="failed",
        )
        store.add_tickets(run_id, [ticket])
        if original_spec:
            # As if a later revision had rewritten the spec: the anchor keeps
            # what was ingested, which is what entailment is judged against.
            store._connection.execute(
                "UPDATE tickets SET original_spec = ? WHERE run_id = ?",
                (original_spec, run_id),
            )
            store._connection.commit()
        step = store.start_step(run_id, "T-1", "review")
        store.end_step(step, "failed", "REJECT: nope")
        return store, run_id

    def _revise(self, store, run_id, criteria, spec=SPEC):
        def call(_messages, _budget):
            return Completion(
                text=json.dumps({"spec": spec, "criteria": criteria}), usage=Usage()
            )

        return respec.revise(
            store, run_id, store.list_tickets(run_id)[0], call=call, budget=1024
        )

    def test_a_criterion_restating_the_spec_is_not_treated_as_a_new_demand(self):
        store, run_id = self._store()

        result = self._revise(store, run_id, ["the plan's bar", self.STATED])

        self.assertIn(self.STATED, store.list_tickets(run_id)[0].criteria)
        self.assertEqual(result.admitted_criteria, [self.STATED])
        self.assertEqual(result.minted_criteria, [])

    def test_the_allowance_is_logged_so_the_heuristic_can_be_audited(self):
        store, run_id = self._store()

        self._revise(store, run_id, ["the plan's bar", self.STATED])

        messages = [row["message"] for row in store.events_after(0)]
        self.assertTrue(any("restate the spec" in message for message in messages))

    def test_a_criterion_absent_from_the_spec_is_still_refused(self):
        store, run_id = self._store()
        invented = "the page includes an element with id hint"

        result = self._revise(store, run_id, ["the plan's bar", invented])

        self.assertEqual(store.list_tickets(run_id)[0].criteria, ["the plan's bar"])
        self.assertEqual(result.minted_criteria, [invented])

    def test_a_criterion_too_short_to_judge_is_refused(self):
        # Overlap on three words is coincidence, and a false positive here lets
        # the loop raise its own bar — the regression the ratchet exists to stop.
        store, run_id = self._store()

        result = self._revise(store, run_id, ["the plan's bar", "src/lib.rs declares"])

        self.assertEqual(result.minted_criteria, ["src/lib.rs declares"])

    def test_entailment_is_judged_against_the_ingested_spec(self):
        # Otherwise the loop could rewrite the spec and then mint criteria out
        # of the sentence it had just written.
        store, run_id = self._store(original_spec="a spec that says none of this")

        result = self._revise(store, run_id, ["the plan's bar", self.STATED])

        self.assertEqual(result.minted_criteria, [self.STATED])
        self.assertEqual(result.admitted_criteria, [])

    def test_the_refusal_no_longer_claims_the_plan_is_silent(self):
        # The old message read "if these are things it genuinely must do, the
        # plan is what needs changing" — false when the plan does state them,
        # in the spec, which is the case this whole guard exists for.
        store, run_id = self._store()

        self._revise(store, run_id, ["the plan's bar", "an element with id hint"])

        messages = " ".join(row["message"] for row in store.events_after(0))
        self.assertNotIn("the plan is what needs changing", messages)


class TestFilingABugFromTheCommandLine(unittest.TestCase):
    """`forge bug` is separate from `ingest` because the shapes differ at the
    root: ingest turns a document into a backlog and takes its criteria as the
    contract, while a report is one symptom whose file scope is unknown and
    whose contract is written afterwards, by a test that has to fail first."""

    REPLY = json.dumps(
        {
            "title": "tick locks three pieces",
            "spec": "Game.tick should lock at most one piece per call",
            "allowed_files": ["src/game.py"],
            "reference_files": ["src/board.py"],
            "reproduce": "tick(3000) locks at most one piece",
        }
    )

    class _Planner(Provider):
        kind = "stub"
        replies: list[str] = []
        seen: list[str] = []

        def complete(self, messages, *, max_tokens, temperature=0.2, timeout=600):
            type(self).seen.append(_joined(messages))
            text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            return Completion(text=text, usage=Usage(), finish_reason="stop")

        def capabilities(self):
            return Capabilities(context_window=32768, max_output_tokens=8192)

    def _project(self):
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"m": {"kind": "openai", "model": "x"}},
                    "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                    "commands": {"test": "pytest -q"},
                    "neverDelegate": ["src/auth/**"],
                }
            ),
            encoding="utf-8",
        )
        return root

    def _run(self, root, *argv, reply=None, replies=None):
        planner = self._Planner("planner", {})
        type(planner).replies = list(replies or [reply or self.REPLY])
        type(planner).seen = []
        parsed = cli.build_parser().parse_args(["--root", str(root), "bug", *argv])
        out = io.StringIO()
        with unittest.mock.patch.object(Config, "provider_for", lambda self, role: planner):
            with contextlib.redirect_stdout(out):
                parsed.func(parsed)
        return out.getvalue()

    def _ticket(self, root):
        store = Store(Config.load(root).db_path)
        try:
            return store.list_tickets(int(store.latest_run()["id"]))[0]
        finally:
            store.close()

    def test_a_report_becomes_one_bug_ticket(self):
        root = self._project()

        printed = self._run(root, "pieces drop three at once after a tab switch")

        ticket = self._ticket(root)
        self.assertEqual(ticket.kind, TICKET_BUG)
        self.assertEqual(ticket.ticket_id, "BUG-001")
        self.assertEqual(ticket.allowed_files, ["src/game.py"])
        # What the reproduction has to assert, read by the tester first and by
        # the executor as the shape of the fix.
        self.assertIn("locks at most one piece", ticket.context)
        self.assertIn("BUG-001", printed)

    def test_the_report_reaches_the_planner_with_the_repository(self):
        root = self._project()
        (root / "src").mkdir()

        self._run(root, "pieces drop three at once")

        self.assertIn("pieces drop three at once", self._Planner.seen[-1])

    def test_the_ids_do_not_collide_across_runs(self):
        # The id names the reproduction's filename, so a second run reusing
        # BUG-001 would overwrite the first one's evidence.
        root = self._project()
        self._run(root, "first report")
        self._run(root, "second report")

        self.assertEqual(self._ticket(root).ticket_id, "BUG-002")

    def test_a_bug_in_a_never_delegate_path_is_left_for_a_human(self):
        root = self._project()
        reply = json.dumps(
            {"title": "t", "spec": "s", "allowed_files": ["src/auth/session.py"]}
        )

        printed = self._run(root, "login sometimes drops the session", reply=reply)

        self.assertEqual(self._ticket(root).route, "claude-only")
        self.assertIn("claude-only", printed)

    def test_a_report_the_planner_cannot_place_stops_there(self):
        root = self._project()
        reply = json.dumps({"unclear": "nothing here matches that description"})

        with self.assertRaises(SystemExit) as caught:
            self._run(root, "the printer is on fire", reply=reply)

        self.assertIn("nothing here matches", str(caught.exception))

    def _checkout(self):
        """A project that is also a git checkout, so evidence can be gathered."""
        root = self._project()
        (root / "src").mkdir()
        (root / "src" / "game.py").write_text(
            "def tick(dt):\n    while dt > 0:\n        lock()\n", encoding="utf-8"
        )
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        return root

    def test_a_vague_report_is_located_before_the_ticket_is_written(self):
        # Two passes: the first decides what to read, the second writes the
        # ticket against the contents. One pass would be choosing scope from a
        # list of filenames, which is what a report naming nothing leaves it.
        root = self._checkout()
        reply = json.dumps(
            {"title": "t", "spec": "tick locks in a loop", "allowed_files": ["src/game.py"]}
        )

        printed = self._run(
            root,
            "sometimes several pieces lock at once",
            replies=[json.dumps({"candidates": ["src/game.py"]}), reply],
        )

        self.assertEqual(len(self._Planner.seen), 2)
        self.assertIn("Name the files to read", self._Planner.seen[0])
        # The ticket was written with the code in front of it.
        self.assertIn("while dt > 0", self._Planner.seen[1])
        self.assertIn("reading src/game.py", printed)

    def test_a_survey_that_answers_nothing_useful_still_files_the_ticket(self):
        # Best effort: the second pass keeps the file list and the grep hits,
        # which is what it had before the survey existed.
        root = self._checkout()
        reply = json.dumps({"title": "t", "spec": "s", "allowed_files": ["src/game.py"]})

        self._run(
            root, "sometimes several pieces lock at once", replies=["not json at all", reply]
        )

        self.assertEqual(self._ticket(root).allowed_files, ["src/game.py"])

    def test_the_ticket_file_says_it_is_a_bug(self):
        # The file is what a human reads before spending anything, and a bug
        # ticket is read differently by the loop — it has to reproduce the
        # fault first. A file that does not say so lies about what happens next.
        root = self._project()
        self._run(root, "pieces drop three at once")

        written = (Config.load(root).tickets_dir / "BUG-001.md").read_text(encoding="utf-8")

        self.assertIn("**Kind:** bug", written)
        self.assertEqual(parse_plan(written)[0].kind, TICKET_BUG)

    def test_an_empty_report_is_refused_before_any_model_is_called(self):
        root = self._project()
        with self.assertRaises(SystemExit) as caught:
            self._run(root, "")
        self.assertIn("report is empty", str(caught.exception))


class TestAdoptingACriterionRespecWasRefused(unittest.TestCase):
    """Respec may not add to the standard it is judged against — it runs on a
    ticket that has just failed, and a ticket that keeps failing does not need
    a higher bar. But a refused proposal is sometimes right, and accepting one
    used to mean editing `plan.md` and re-ingesting the whole backlog: a fresh
    run, and every ticket that had already passed done again."""

    PROPOSED = "clearing four lines at once scores 800"

    def _project(self):
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
                    "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                }
            ),
            encoding="utf-8",
        )
        config = Config.load(root)
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        store.add_tickets(
            run_id,
            [Ticket("TT-003", spec="s", criteria=["the plan's bar"], status="failed")],
        )
        store.log(
            run_id,
            "TT-003: respec proposed 1 criterion(s) the plan states nowhere",
            level="warn",
            kind="ticket",
            data={"minted": [self.PROPOSED], "ticket": "TT-003"},
        )
        store.close()
        return root, config

    def _run(self, root, *argv):
        parsed = cli.build_parser().parse_args(["--root", str(root), "criteria", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            parsed.func(parsed)
        return out.getvalue()

    def _tickets(self, config):
        store = Store(config.db_path)
        try:
            return store.list_tickets(int(store.latest_run()["id"]))
        finally:
            store.close()

    def test_a_refused_proposal_is_listed_with_the_command_that_adopts_it(self):
        root, _config = self._project()

        printed = self._run(root)

        self.assertIn(self.PROPOSED, printed)
        self.assertIn("forge criteria TT-003 --accept 1", printed)

    def test_accepting_one_makes_it_the_plans_own(self):
        root, config = self._project()

        self._run(root, "TT-003", "--accept", "1")

        ticket = self._tickets(config)[0]
        self.assertIn(self.PROPOSED, ticket.criteria)
        # Plan-authored from here: the ratchet protects it from the next
        # revision exactly as if a human had written it in the plan.
        self.assertIn(self.PROPOSED, ticket.original_criteria)

    def test_an_adopted_criterion_stops_being_outstanding(self):
        root, _config = self._project()
        self._run(root, "TT-003", "--accept", "1")

        printed = self._run(root)

        self.assertIn("nothing outstanding", printed)

    def test_the_ticket_file_is_rewritten_so_it_does_not_lie(self):
        root, config = self._project()

        self._run(root, "TT-003", "--accept", "1")

        written = (config.tickets_dir / "TT-003.md").read_text(encoding="utf-8")
        self.assertIn(self.PROPOSED, written)

    def test_the_adoption_is_recorded_in_the_run(self):
        root, config = self._project()

        self._run(root, "TT-003", "--accept", "1")

        store = Store(config.db_path)
        messages = " ".join(row["message"] for row in store.events_after(0))
        store.close()
        self.assertIn("a human adopted 1 criterion(s)", messages)

    def test_a_number_that_is_not_on_offer_is_refused(self):
        root, _config = self._project()

        with self.assertRaises(SystemExit) as caught:
            self._run(root, "TT-003", "--accept", "2")

        self.assertIn("there is no 2", str(caught.exception))

    def test_accepting_without_naming_a_ticket_says_so(self):
        root, _config = self._project()

        with self.assertRaises(SystemExit) as caught:
            self._run(root, "--accept", "1")

        self.assertIn("name the ticket", str(caught.exception))

    def test_adopting_the_same_criterion_twice_does_not_duplicate_it(self):
        # The second call has nothing outstanding to number, so the guard that
        # fires is the empty-list one — and either way the ticket ends with one.
        root, config = self._project()
        self._run(root, "TT-003", "--accept", "1")

        with self.assertRaises(SystemExit):
            self._run(root, "TT-003", "--accept", "1")

        ticket = self._tickets(config)[0]
        self.assertEqual(ticket.criteria.count(self.PROPOSED), 1)

    def test_a_ticket_that_already_passed_is_not_requeued_behind_your_back(self):
        root, config = self._project()
        store = Store(config.db_path)
        run_id = int(store.latest_run()["id"])
        ticket = store.list_tickets(run_id)[0]
        ticket.status = TICKET_DONE
        store.update_ticket(run_id, ticket)
        store.close()

        printed = self._run(root, "TT-003", "--accept", "1")

        self.assertIn("forge retry --ticket TT-003", printed)
        self.assertEqual(self._tickets(config)[0].status, TICKET_DONE)


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
        # Unlocked, because that is the only setting where this check still
        # decides anything. With the criteria locked, a proposed criterion the
        # ticket does not already have is refused as a mint before it reaches
        # the shared-file rule — a stricter gate that happens to subsume it.
        return respec.revise(
            store, run_id, ticket, call=call, budget=1024, criteria_locked=False
        )

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
        merged, refused, _minted = _merge_criteria(
            ticket,
            ["Game::new(0) does not panic", "tick(0) leaves y unchanged"],
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual(refused, [])

    def test_the_plans_wording_is_the_one_that_survives(self):
        ticket = self._ticket()
        merged, _refused, _minted = _merge_criteria(ticket, ["Game::new(0) does not panic"])

        self.assertIn("`Game::new(0)` does not panic", merged)

    def test_a_genuinely_dropped_criterion_is_still_restored_and_reported(self):
        ticket = self._ticket()
        merged, refused, _minted = _merge_criteria(ticket, ["`Game::new(0)` does not panic"])

        self.assertEqual(len(merged), 2)
        self.assertEqual(refused, ["`tick(0)` leaves `y` unchanged"])

    def test_a_genuinely_new_criterion_is_refused(self):
        """Adding is the ratchet. See TestRespecMayNotRaiseTheBar."""
        ticket = self._ticket()
        merged, _refused, minted = _merge_criteria(
            ticket, [*ticket.criteria, "`level` starts at 1"]
        )

        self.assertEqual(len(merged), 2)
        self.assertNotIn("`level` starts at 1", merged)
        self.assertEqual(minted, ["`level` starts at 1"])

    def test_a_spec_that_takes_up_the_reply_format_is_dropped(self):
        # Verbatim from a real revision. Respec read an unparseable response as
        # a formatting problem and wrote the cure into the spec — and the cure
        # was the one thing that guarantees nothing parses, since a fence is
        # what the parser matches on.
        ticket = Ticket("TT-006", spec="Document and script the build.",
                        original_spec="Document and script the build.")
        revision = {
            "spec": (
                "Create exactly three files. Output their raw contents directly "
                "in your response, prefixed by the filename. Do not wrap file "
                "contents in markdown code fences."
            )
        }

        dropped = _refuse_protocol_edits(ticket, revision)

        self.assertNotIn("spec", revision)
        self.assertEqual([field for field, _phrase in dropped], ["spec"])

    def test_the_context_is_guarded_the_same_way(self):
        ticket = Ticket("TT-006", spec="s", original_spec="s", context="")
        revision = {"context": "Emit each file with the path on its own line."}

        _refuse_protocol_edits(ticket, revision)

        self.assertNotIn("context", revision)

    def test_an_ordinary_revision_is_untouched(self):
        ticket = Ticket("TT-006", spec="old", original_spec="old")
        revision = {"spec": "build.sh must start with a POSIX shebang."}

        self.assertEqual(_refuse_protocol_edits(ticket, revision), [])
        self.assertIn("spec", revision)

    def test_a_ticket_whose_plan_already_talks_about_fences_stays_revisable(self):
        # A markdown tool legitimately has fences in its spec. The guard is
        # about what a revision *introduces*, not about the subject matter.
        ticket = Ticket(
            "TT-009",
            spec="Render each code fence as a <pre>.",
            original_spec="Render each code fence as a <pre>.",
        )
        revision = {"spec": "Render each code fence as a <pre>, preserving the language."}

        self.assertEqual(_refuse_protocol_edits(ticket, revision), [])
        self.assertIn("spec", revision)

    def test_a_criterion_returned_with_its_provenance_note_is_not_counted_as_new(self):
        """The observed regression, in the other direction.

        The prompt asks for plan-authored criteria back verbatim and marks each
        one. A planner that copies the mark with the criterion has changed
        nothing, but the note survived normalisation, so the same thirteen
        counted once as dropped and once as invented — a reply doing exactly as
        it was told, reported as gutting the contract and raising the bar at
        once.
        """
        plan = [f"`f{i}()` returns {i}" for i in range(13)]
        ticket = Ticket("TT-003", criteria=list(plan), original_criteria=list(plan))
        echoed = [
            f"{c}\n  _(from the plan — you may not change this)_" for c in plan
        ]
        merged, refused, minted = _merge_criteria(ticket, echoed)

        self.assertEqual(merged, plan)
        self.assertEqual(refused, [])
        self.assertEqual(minted, [])

    def test_the_note_is_stripped_in_its_inline_spelling_too(self):
        plan = ["`WIDTH` is 10"]
        ticket = Ticket("TT-003", criteria=list(plan), original_criteria=list(plan))
        _merged, refused, minted = _merge_criteria(
            ticket, ["`WIDTH` is 10 (from the plan — you may not change this)"]
        )

        self.assertEqual((refused, minted), ([], []))

    def test_a_note_on_a_revision_authored_criterion_is_stripped_as_well(self):
        ticket = Ticket(
            "TT-003",
            criteria=["from the plan", "minted earlier"],
            original_criteria=["from the plan"],
        )
        merged, refused, minted = _merge_criteria(
            ticket,
            [
                "from the plan",
                "minted earlier _(added by an earlier revision — you may revise or retire it)_",
            ],
        )

        self.assertEqual(merged, ["from the plan", "minted earlier"])
        self.assertEqual((refused, minted), ([], []))

    def test_a_genuinely_new_criterion_is_still_refused(self):
        # The note is presentation, not a passphrase: attaching it to something
        # the ticket never had must not launder it through.
        ticket = Ticket("TT-003", criteria=["a"], original_criteria=["a"])
        _merged, _refused, minted = _merge_criteria(
            ticket, ["a", "b _(from the plan — you may not change this)_"]
        )

        self.assertEqual(len(minted), 1)

    def test_thirteen_criteria_reworded_stay_thirteen(self):
        """The observed regression: a plan stating 13 reached 27 in one pass."""
        plan = [f"`f{i}()` returns {i}" for i in range(13)]
        ticket = Ticket("TT-003", criteria=list(plan), original_criteria=list(plan))
        merged, refused, _minted = _merge_criteria(
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


class TestTheTesterIsPointedAtItsOwnErrors(unittest.TestCase):
    """The tester's file is outside every other role's scope, so a style error
    in it fails the ticket for as long as the tester keeps reproducing it. One
    run spent twelve retry cycles on a single unused variable: the failure it
    was shown read as evidence about the implementation, so it rewrote the
    assertions and left the variable alone every time."""

    LINT = (
        "lint failed:\n"
        "Checking tetris v0.1.0 (D:\\repo)\n"
        "error: unused variable: `x`\n"
        "  --> tests\\tt_001_test.rs:67:10\n"
        "   |\n"
        "67 |     for (x, y) in cells {\n"
        "   = note: `-D unused-variables` implied by `-D warnings`\n"
    )

    def test_the_error_and_its_location_are_extracted_together(self):
        found = errors_naming(self.LINT, "tests/tt_001_test.rs")

        self.assertEqual(len(found), 1)
        self.assertIn("unused variable", found[0])
        self.assertIn("tt_001_test.rs:67", found[0])

    def test_a_windows_path_matches_a_posix_one(self):
        """The compiler prints `tests\\a_test.rs`; the loop holds `tests/a_test.rs`."""
        self.assertTrue(errors_naming(self.LINT, "tests/tt_001_test.rs"))

    def test_errors_in_other_files_are_not_claimed(self):
        self.assertEqual(errors_naming(self.LINT, "src/piece.rs"), [])

    def test_an_implementation_failure_yields_nothing(self):
        text = "error[E0432]: unresolved import\n  --> src/game.rs:4:5\n"
        self.assertEqual(errors_naming(text, "tests/tt_001_test.rs"), [])

    def test_no_test_path_yields_nothing(self):
        self.assertEqual(errors_naming(self.LINT, ""), [])

    def test_the_prompt_puts_them_in_front_of_the_tester(self):
        body = tests_prompt(
            Ticket("TT-001", criteria=["cells() returns four"]),
            ["src/piece.rs"],
            test_path="tests/tt_001_test.rs",
            failure_context=self.LINT,
            own_file_errors=errors_naming(self.LINT, "tests/tt_001_test.rs"),
        )[1].content

        self.assertIn("errors are in the file you are about to write", body)
        self.assertIn("unused variable", body)

    def test_a_clean_attempt_carries_no_such_section(self):
        body = tests_prompt(
            Ticket("TT-001", criteria=["cells() returns four"]),
            ["src/piece.rs"],
            test_path="tests/tt_001_test.rs",
        )[1].content

        self.assertNotIn("errors are in the file you are about to write", body)

    def test_the_three_branches_are_all_stated(self):
        body = tests_prompt(
            Ticket("TT-001", criteria=["c"]),
            ["src/piece.rs"],
            test_path="tests/tt_001_test.rs",
            failure_context="something failed",
        )[1].content

        self.assertIn("names your own test file", body)
        self.assertIn("your own assertion being wrong", body)
        self.assertIn("the implementation being wrong", body)


class TestForeignBindingsInTests(unittest.TestCase):
    """A test that re-declares its subject with `extern` or `dlopen` does not
    fail an assertion — it fails to *link*, and takes every other test in the
    same target with it. TESTER_SYSTEM forbids exactly this, in detail, and a
    small local model did it anyway: seven unresolved symbols, three cycles."""

    # The file that actually broke a run, trimmed.
    REAL = (
        'use std::ffi::CString;\n'
        'use std::os::raw::c_char;\n'
        'extern "C" {\n'
        '    fn game_new(seed: u32) -> *mut c_void;\n'
        '    fn game_score(g: *mut c_void) -> u32;\n'
        '}\n'
        '#[test]\n'
        'fn test_game_new_and_score() { unsafe { game_new(1); } }\n'
    )

    def test_the_file_that_broke_the_run_is_caught(self):
        found = foreign_bindings(self.REAL)
        self.assertEqual(len(found), 1)
        self.assertIn("extern block", found[0])

    def test_the_correct_version_of_the_same_test_is_clean(self):
        good = (
            "use tetris::wasm;\n"
            "#[test]\n"
            "fn test_game_new_and_score() {\n"
            "    let g = wasm::game_new(1);\n"
            "    assert_eq!(wasm::game_score(g), 0);\n"
            "}\n"
        )
        self.assertEqual(foreign_bindings(good), [])

    def test_ordinary_rust_declarations_are_not_flagged(self):
        for line in (
            "extern crate serde;",
            'pub extern "C" fn game_new(seed: u32) -> u32 { seed }',
            "use std::os::raw::c_char;",
        ):
            with self.subTest(line=line):
                self.assertEqual(foreign_bindings(line), [], line)

    def test_a_comment_is_not_a_declaration(self):
        self.assertEqual(foreign_bindings('// extern "C" { fn x(); }'), [])
        self.assertEqual(foreign_bindings("# lib = ctypes.CDLL('x')"), [])

    def test_other_languages_are_caught_too(self):
        for text, label in (
            ('lib = ctypes.CDLL("./libgame.so")', "ctypes"),
            ('from ctypes import cdll\ncdll.LoadLibrary("x")', "ctypes"),
            ('import "C"', "cgo import"),
            ('[DllImport("game.dll")]', "DllImport"),
            ('const lib = Deno.dlopen("./game.so", {});', "Deno.dlopen"),
            ('System.loadLibrary("game");', "System.loadLibrary"),
        ):
            with self.subTest(text=text):
                found = foreign_bindings(text)
                self.assertTrue(found, text)
                self.assertIn(label, found[0])

    def test_the_prompt_quotes_back_what_was_rejected(self):
        body = tests_prompt(
            Ticket("TT-004", criteria=["game_score() returns 0"]),
            ["src/wasm.rs"],
            test_path="tests/tt_004_test.rs",
            rejected_bindings=['extern block: extern "C" {'],
        )[1].content

        self.assertIn("rejected before it reached disk", body)
        self.assertIn('extern "C" {', body)

    def test_a_clean_answer_carries_no_rejection_section(self):
        body = tests_prompt(
            Ticket("TT-004", criteria=["game_score() returns 0"]),
            ["src/wasm.rs"],
            test_path="tests/tt_004_test.rs",
        )[1].content

        self.assertNotIn("rejected before it reached disk", body)


class TestTheTesterIsAskedAgainForForeignBindings(unittest.TestCase):
    """Rejected before it reaches disk, then asked for once more with the
    offending line quoted. A prohibition a model ignores needs an enforcement
    point, not stronger wording."""

    BAD = 'tests/tt_004_test.rs\n```rust\nextern "C" { fn game_new(); }\n```'
    GOOD = (
        "tests/tt_004_test.rs\n```rust\n"
        "use tetris::wasm;\n#[test]\nfn t() { wasm::game_new(1); }\n```"
    )

    def _run(self, *tester_replies):
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1
        orch._call = _replies(
            "src/wasm.rs\n```rust\npub fn game_new(s: u32) -> u32 { s }\n```",
            *tester_replies,
            "ACCEPT\nfine",
        )
        orch._work_ticket(
            run_id,
            Ticket("TT-004", allowed_files=["src/wasm.rs"], criteria=["game_new works"]),
        )
        return orch, root, run_id

    def _tests_file(self, root):
        return root / "tests" / "tt_004_test.rs"

    def test_a_second_answer_that_is_clean_is_kept(self):
        _orch, root, _run_id = self._run(self.BAD, self.GOOD)

        self.assertTrue(self._tests_file(root).exists())
        self.assertNotIn("extern", self._tests_file(root).read_text(encoding="utf-8"))

    def test_a_tester_that_keeps_doing_it_gets_its_tests_discarded(self):
        _orch, root, _run_id = self._run(self.BAD, self.BAD)

        self.assertFalse(self._tests_file(root).exists())

    def test_the_rejection_is_reported(self):
        orch, _root, run_id = self._run(self.BAD, self.BAD)

        messages = " ".join(e["message"] for e in orch.store.events_after(0))
        self.assertIn("foreign binding", messages)

    def test_a_clean_first_answer_is_not_asked_twice(self):
        orch, root, _run_id = self._run(self.GOOD)

        self.assertTrue(self._tests_file(root).exists())


class TestTheTestCommandDecidesTheLanguage(unittest.TestCase):
    """A polyglot repo — a Rust core with a browser shell — has test files of
    several extensions under tests/. Reading the suite's language off whichever
    one turned up first is what disabled test authoring for a whole backlog:
    the shell ticket legitimately wrote `tests/tt_005_test.js`, and from then
    on every Rust ticket was told the suite collects `.js`, wrote nothing, and
    had the skip logged as routine."""

    def _orch(self, test_command="cargo test"):
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 8192}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": test_command},
        )
        return Orchestrator(config, Store(root / "t.db")), root

    def _with_tests(self, root, *names):
        (root / "tests").mkdir(exist_ok=True)
        for name in names:
            (root / "tests" / name).write_text("x\n", encoding="utf-8")

    def test_a_stray_js_file_no_longer_hijacks_a_rust_suite(self):
        orch, root = self._orch("cargo test")
        self._with_tests(root, "tt_005_test.js")

        path, reason = orch._test_target(
            Ticket("TT-003"), ["src/game.rs"], None, orch._suite_suffix(["src/game.rs"])
        )

        self.assertEqual(path, "tests/tt_003_test.rs")
        self.assertEqual(reason, "")

    def test_the_command_outvotes_a_repo_full_of_the_other_language(self):
        orch, root = self._orch("cargo test")
        self._with_tests(root, "a_test.js", "b_test.js", "c_test.js")

        self.assertEqual(orch._suite_suffix(["src/game.rs"]), ".rs")

    def test_common_runners_resolve(self):
        for command, expected in (
            ("cargo test", ".rs"),
            ("python -m pytest -q", ".py"),
            ("python -m unittest discover tests", ".py"),
            ("go test ./...", ".go"),
            ("dotnet test", ".cs"),
            ("bundle exec rspec", ".rb"),
            ("swift test", ".swift"),
        ):
            with self.subTest(command=command):
                orch, _ = self._orch(command)
                self.assertEqual(orch._suite_suffix(["src/thing.xyz"]), expected)

    def test_a_containerised_command_still_resolves(self):
        orch, _ = self._orch(
            'docker run --rm --network none -v "/abs/repo":/w -w /w '
            "python:3.12-slim python -m pytest -q"
        )
        self.assertEqual(orch._suite_suffix(["src/app.py"]), ".py")

    def test_a_javascript_runner_lets_the_repo_break_the_tie(self):
        orch, root = self._orch("npx vitest run")
        self._with_tests(root, "a_test.ts", "b_test.ts")

        self.assertEqual(orch._suite_suffix(["src/app.ts"]), ".ts")

    def test_an_unrecognised_command_falls_back_to_the_repo_majority(self):
        """One stray fixture must not outvote a suite. The old rule took the
        first file it found, in glob order."""
        orch, root = self._orch("make check")
        self._with_tests(root, "a_test.rs", "b_test.rs", "c_test.rs", "odd_test.js")

        self.assertEqual(orch._suite_suffix(["src/game.rs"]), ".rs")

    def test_a_fresh_repo_falls_back_to_what_the_ticket_wrote(self):
        orch, _ = self._orch("make check")
        self.assertEqual(orch._suite_suffix(["src/game.rs"]), ".rs")

    def test_the_example_shown_matches_the_language_asked_for(self):
        """Otherwise the tester is handed a JavaScript file and asked for Rust."""
        orch, root = self._orch("cargo test")
        self._with_tests(root, "shell_test.js", "core_test.rs")

        found = orch._example_test([], orch._suite_suffix([]))

        self.assertIsNotNone(found)
        self.assertTrue(found[0].endswith(".rs"), found[0])

    def test_a_cross_language_ticket_is_still_told_why_it_gets_no_tests(self):
        orch, _ = self._orch("cargo test")

        path, reason = orch._test_target(
            Ticket("TT-005"), ["web/main.js"], None, orch._suite_suffix(["web/main.js"])
        )

        self.assertEqual(path, "")
        self.assertIn(".rs", reason)

    def test_a_run_that_authored_no_tests_at_all_says_so(self):
        orch, _root = self._orch("cargo test")
        run_id = orch.store.create_run("g")
        orch._tests_skipped = {"T-1", "T-2", "T-3", "T-4"}

        orch._finish(run_id)

        messages = " ".join(e["message"] for e in orch.store.events_after(0))
        self.assertIn("No ticket in this run authored tests", messages)

    def test_a_run_with_some_tests_stays_quiet(self):
        orch, _root = self._orch("cargo test")
        run_id = orch.store.create_run("g")
        orch._tests_skipped = {"T-1", "T-2"}
        orch._tests_authored = {"T-3", "T-4", "T-5"}

        orch._finish(run_id)

        messages = " ".join(e["message"] for e in orch.store.events_after(0))
        self.assertNotIn("No ticket in this run authored tests", messages)


class TestAGreenTicketMayHaveRunNothing(unittest.TestCase):
    """A backlog went green — six tickets done, lint and typecheck clean, 36
    tests passing — and the page loaded to an empty board. TT-005's criteria
    were all token-presence checks ("`web/main.js` calls
    `WebAssembly.instantiateStreaming`"), every one of them true of code that
    threw on the next line. It authored no tests, and `cargo test` runs no
    JavaScript, so its criteria were checked by reading. Nothing in the
    pipeline could have caught that. What it can do is say so."""

    def _orch(self, done: list[str], skipped: set[str], authored: set[str] = frozenset()):
        orch, _root, run_id = _stub_orchestrator({"test": "cargo test"})
        orch.store.add_tickets(
            run_id,
            [Ticket(ticket_id, status=TICKET_DONE) for ticket_id in done],
        )
        orch._tests_skipped = set(skipped)
        orch._tests_authored = set(authored)
        orch._finish(run_id)
        return " ".join(e["message"] for e in orch.store.events_after(0))

    def test_a_ticket_verified_by_reading_is_named_at_run_end(self):
        messages = self._orch(
            done=["TT-004", "TT-005"], skipped={"TT-005"}, authored={"TT-004"}
        )

        self.assertIn("TT-005 passed on review alone", messages)
        self.assertIn("rather than by running anything", messages)
        self.assertNotIn("TT-004 passed on review alone", messages)

    def test_several_are_named_together(self):
        messages = self._orch(
            done=["TT-004", "TT-005", "TT-006"],
            skipped={"TT-005", "TT-006"},
            authored={"TT-004"},
        )
        self.assertIn("TT-005, TT-006 passed on review alone", messages)

    def test_a_ticket_that_authored_tests_on_a_later_attempt_is_not_named(self):
        # Skipping on the attempt that wrote nothing and authoring on the one
        # that did is ordinary. What matters is whether the ticket ended up
        # covered, not whether it was ever briefly uncovered.
        messages = self._orch(
            done=["TT-004"], skipped={"TT-004"}, authored={"TT-004"}
        )
        self.assertNotIn("passed on review alone", messages)

    def test_a_ticket_that_never_passed_is_not_named(self):
        # The claim is about what a green ticket proved. A failed one makes no
        # claim to undercut.
        messages = self._orch(done=[], skipped={"TT-005"}, authored={"TT-004"})
        self.assertNotIn("passed on review alone", messages)


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


class TestEvidenceForABugReport(unittest.TestCase):
    """A plan says which files a ticket may write. A report does not — the file
    that needs changing is the thing being looked for. The harness gathers the
    evidence rather than the model, so it works behind every adapter and needs
    no tool grant."""

    REPORT = (
        "Pieces sometimes drop three at once after I switch tabs. Looks like "
        "`SoftDrop` in src/game.rs, maybe Game::tick."
    )

    def _repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        (root / "src" / "game.rs").write_text(
            "pub fn tick() {}\n// SoftDrop locks the piece\n", encoding="utf-8"
        )
        (root / "target").mkdir()
        (root / "target" / "junk.rs").write_text("noise\n", encoding="utf-8")
        (root / ".gitignore").write_text("target/\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        return root

    def test_the_searchable_terms_are_the_specific_ones(self):
        found = evidence.terms(self.REPORT)

        self.assertIn("SoftDrop", found)
        self.assertIn("src/game.rs", found)
        self.assertIn("Game::tick", found)
        # Prose matches everything and locates nothing.
        self.assertNotIn("sometimes", found)

    def test_the_same_word_in_two_cases_is_searched_once(self):
        self.assertEqual(evidence.terms("`SoftDrop` and softdrop"), ["SoftDrop"])

    def test_it_lists_the_files_and_where_the_words_appear(self):
        gathered = evidence.gather(self._repo(), self.REPORT)

        self.assertIn("src/game.rs", gathered)
        self.assertIn("SoftDrop", gathered)
        # git ls-files honours .gitignore, so build output never reaches the
        # prompt — a planner scoped to target/junk.rs writes a useless ticket.
        self.assertNotIn("junk.rs", gathered)

    def test_work_that_was_never_committed_is_still_searched(self):
        """The case that broke it. `autoCommit` is off by default, so a project
        the loop has just built is entirely untracked — `git ls-files` reports
        nothing about it, and the first bug report against fresh work reached
        the planner with an empty file list and came back "no repository
        evidence was provided". The report was fine; the search never looked."""
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        (root / "src" / "game.rs").write_text("fn soft_drop() {}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True, check=False)

        gathered = evidence.gather(root, "`soft_drop` locks too early")

        self.assertIn("src/game.rs", gathered)
        self.assertIn("soft_drop", gathered)

    def test_a_project_without_git_is_still_searched(self):
        root = Path(tempfile.mkdtemp())
        (root / "game.py").write_text("def soft_drop():\n    pass\n", encoding="utf-8")

        gathered = evidence.gather(root, "`soft_drop` locks too early")

        self.assertIn("game.py", gathered)
        self.assertIn("soft_drop", gathered)

    def test_the_walk_skips_what_a_gitignore_would_have(self):
        # A listing of node_modules is not evidence, and it would crowd out
        # everything that is.
        root = Path(tempfile.mkdtemp())
        (root / "node_modules" / "dep").mkdir(parents=True)
        (root / "node_modules" / "dep" / "index.js").write_text("x", encoding="utf-8")
        (root / "app.js").write_text("function draw() {}\n", encoding="utf-8")

        gathered = evidence.gather(root, "drawing is wrong")

        self.assertIn("app.js", gathered)
        self.assertNotIn("node_modules", gathered)

    def test_an_empty_directory_yields_nothing(self):
        # An honest empty block. A planner told "here is the evidence" over an
        # invented tree scopes a ticket to files that do not exist.
        self.assertEqual(evidence.gather(Path(tempfile.mkdtemp()), self.REPORT), "")


class TestLocatingAVagueReport(unittest.TestCase):
    """The report a person actually files names no function and no file: "the
    score sometimes stops updating". Specific terms find nothing in it, and a
    planner handed only a file tree is choosing scope by filename."""

    VAGUE = "The score sometimes stops updating after I clear a line."

    def _repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        (root / "src" / "scoring.py").write_text(
            "def commit_lines(count):\n    return count * 100\n", encoding="utf-8"
        )
        (root / "src" / "render.py").write_text(
            "def draw():\n    pass\n", encoding="utf-8"
        )
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        return root

    def test_a_report_naming_nothing_specific_yields_no_terms(self):
        self.assertEqual(evidence.terms(self.VAGUE), [])

    def test_its_content_words_are_searched_instead(self):
        found = evidence.prose_terms(self.VAGUE)

        self.assertIn("score", found)
        self.assertIn("line", found)
        # Grepping these returns the whole project, which locates nothing.
        for word in ("sometimes", "after", "the"):
            self.assertNotIn(word, found)

    def test_the_words_a_report_repeats_come_first(self):
        found = evidence.prose_terms("The board is wrong. The board draws twice.")
        self.assertEqual(found[0], "board")

    def test_a_vague_report_still_finds_the_code(self):
        gathered = evidence.gather(self._repo(), self.VAGUE)

        # "score" is not in scoring.py's text, but it is in its name, and
        # "line" reaches commit_lines.
        self.assertIn("commit_lines", gathered)
        # With nothing specific named, the definitions are worth their space:
        # they are the bridge from the report's words to the code's names.
        self.assertIn("Every definition in this repository", gathered)

    def test_a_report_that_named_a_symbol_gets_no_definition_dump(self):
        # It has already told us more than any word frequency will.
        gathered = evidence.gather(self._repo(), "`commit_lines` returns twice the score")
        self.assertNotIn("Every definition in this repository", gathered)

    def test_the_survey_asks_which_files_to_open(self):
        body = locate_prompt(self.VAGUE, "### Files\nsrc/scoring.py")[-1].content

        self.assertIn(self.VAGUE, body)
        self.assertIn("src/scoring.py", body)
        self.assertIn("Name the files to read", body)

    def test_candidates_are_filtered_to_files_that_exist(self):
        # A path the model invented would be read as nothing, and the ticket
        # then written as though the file had been read and found irrelevant.
        chosen = parse_locate(
            json.dumps({"candidates": ["src/scoring.py", "src/imagined.py"]}),
            known=["src/scoring.py", "src/render.py"],
        )
        self.assertEqual(chosen, ["src/scoring.py"])

    def test_an_unreadable_survey_reply_costs_nothing(self):
        self.assertEqual(parse_locate("I think it is in the scorer.", known=["a.py"]), [])

    def test_the_chosen_files_are_read_whole(self):
        root = self._repo()
        read = evidence.read_files(root, ["src/scoring.py"])
        self.assertIn("commit_lines", read["src/scoring.py"])

    def test_a_path_outside_the_project_is_not_read(self):
        root = self._repo()
        self.assertEqual(evidence.read_files(root, ["../../etc/passwd"]), {})

    def test_the_ticket_is_written_against_the_contents(self):
        body = bug_prompt(
            self.VAGUE, "### Files\nsrc/scoring.py", {"src/scoring.py": "def commit_lines(): ..."}
        )[-1].content

        self.assertIn("def commit_lines()", body)
        self.assertIn("State the defect in terms of what", body)


class TestPlanningFromABugReport(unittest.TestCase):
    def test_the_planner_is_given_the_report_and_the_repository(self):
        body = bug_prompt("pieces drop three at once", "### Files\nsrc/game.rs")[-1].content

        self.assertIn("pieces drop three at once", body)
        self.assertIn("src/game.rs", body)
        self.assertIn("every path you name", body)

    def test_a_ticket_is_parsed_out_of_the_reply(self):
        fields = parse_bug(
            json.dumps(
                {
                    "title": "tick locks three pieces",
                    "spec": "Game::tick drains its accumulator with a loop",
                    "allowed_files": ["src/game.rs"],
                    "reference_files": ["src/lib.rs"],
                    "reproduce": "tick(3000) locks at most one piece",
                }
            )
        )

        self.assertEqual(fields["allowed_files"], ["src/game.rs"])
        self.assertEqual(fields["reproduce"], "tick(3000) locks at most one piece")

    def test_a_report_the_planner_cannot_place_is_not_turned_into_a_ticket(self):
        # Better than a plausible ticket scoped to files that do not exist.
        with self.assertRaises(ValueError) as caught:
            parse_bug(json.dumps({"unclear": "nothing in this repo matches"}))
        self.assertIn("nothing in this repo matches", str(caught.exception))

    def test_a_reply_with_no_spec_is_refused(self):
        with self.assertRaises(ValueError):
            parse_bug(json.dumps({"title": "t", "allowed_files": ["a.py"]}))

    def test_the_tester_is_told_to_write_a_test_that_fails(self):
        body = repro_prompt(
            Ticket("BUG-001", title="t", spec="tick locks three pieces"),
            test_path="tests/bug_001_test.py",
            reproduce="tick(3000) locks at most one piece",
        )[-1].content
        system = repro_prompt(
            Ticket("BUG-001"), test_path="tests/bug_001_test.py"
        )[0].content

        self.assertIn("tick(3000) locks at most one piece", body)
        self.assertIn("must FAIL", system)
        self.assertIn("Assert the CORRECT behavior", system)
        # The failure has to be the assertion, not a broken file.
        self.assertIn("`assert False`", system)


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


def _joined(messages) -> str:
    """Every message in a prompt as one string.

    History now travels in its own message so the budget gate can drop it, so
    asserting against the last message alone would test where a block sits
    rather than whether the role is told.
    """
    return "\n\n".join(message.content for message in messages)


class TestHistoryIsTrimmedRatherThanBlocking(unittest.TestCase):
    """The rejection block was the one part of a prompt that grew without
    bound, and it was not droppable. A ticket that accumulated enough rejection
    text overflowed the window, and `ContextOverflow` becomes `blocked=True` —
    a hard stop for the crime of having been reviewed too often. Not reachable
    at `maxAttempts: 3`; reachable the moment that is raised, and sooner on a
    small single-model window."""

    class _Model(Provider):
        kind = "stub"

        def __init__(self, window: int):
            super().__init__("local", {})
            self._window = window

        def complete(self, messages, *, max_tokens, temperature=0.2, timeout=600):
            raise NotImplementedError

        def capabilities(self):
            return Capabilities(context_window=self._window, max_output_tokens=256)

        def count_tokens(self, messages):
            return sum(len(m.content) for m in messages)

    def _fit(self, messages, window):
        gate = BudgetGate(Store(Path(tempfile.mkdtemp()) / "t.db"), {})
        return gate.fit(
            self._Model(window),
            messages,
            max_output=256,
            droppable=lambda m: m.role == "user"
            and m.content.startswith(_DROPPABLE_HEADINGS),
        )

    def test_a_long_rejection_history_is_trimmed_rather_than_blocking(self):
        messages = review_prompt(
            Ticket("T-1", spec="the spec that must survive"),
            "diff --git a/x b/x",
            prior_verdicts=["REJECT: " + "x" * 4000 for _ in range(6)],
        )

        kept = _joined(self._fit(messages, window=4096))

        self.assertIn("the spec that must survive", kept)
        self.assertNotIn("already rejected this ticket", kept)

    def test_earlier_failures_are_droppable_too(self):
        messages = build_prompt(
            Ticket("T-1", spec="the spec that must survive"),
            prior_failures=[
                f"Attempt {index}:\n"
                + "\n".join(f"error[E{line}]: something is broken" for line in range(60))
                for index in range(6)
            ],
        )

        kept = _joined(self._fit(messages, window=4096))

        self.assertIn("the spec that must survive", kept)
        self.assertNotIn("Earlier attempts on this ticket", kept)

    def test_retrieved_memory_goes_before_the_history_does(self):
        # Both are droppable and the gate drops in message order, so the
        # prompts put context first: what has already been tried is worth more
        # than what a memory server thought was topical.
        messages = review_prompt(
            Ticket("T-1", spec="s", context="a paragraph of retrieved memory. " * 200),
            "diff",
            prior_verdicts=["REJECT: the error path is swallowed"],
        )

        kept = _joined(self._fit(messages, window=4096))

        self.assertNotIn("retrieved memory", kept)
        self.assertIn("the error path is swallowed", kept)

    def test_the_reviewer_is_shown_a_bounded_number_of_verdicts(self):
        # Trimming is the gate's last resort; the cap is what keeps it from
        # being needed. `_PRIOR_FAILURES` has always had one — this is its
        # counterpart on the side that actually grew.
        orch, _, run_id = _stub_orchestrator()
        seen: list[list[Message]] = []

        def call(_run_id, role, messages, **_kwargs):
            if role == "reviewer":
                seen.append(messages)
            return Completion(text="REJECT: still wrong", usage=Usage())

        orch._call = call
        rejections = [f"REJECT: objection {index}" for index in range(6)]

        orch._attempt(
            run_id, Ticket("T-1", allowed_files=["src/a.py"]), "", rejections=rejections
        )

        shown = _joined(seen[-1])
        self.assertIn("objection 5", shown)
        self.assertNotIn("objection 0", shown)


class TestTheExecutorCanSeeItsOwnAnswers(unittest.TestCase):
    """The executor has never seen its own output. It is handed the spec, the
    files as they exist on disk and the failures — with nothing anywhere saying
    that it wrote those files. That is the state behind "Looking at the files
    provided, I can see they already implement the spec correctly": a model
    reading its own work as somebody else's. Behind `loop.executorTurns`,
    because a model shown its own wrong answer as an assistant turn also
    defends it more readily, and which effect wins is a measurement."""

    TURNS = [
        ("src/a.py\n```python\nx = 1\n```", "lint failed: x is unused"),
        ("src/a.py\n```python\nx = 2\n```", "review rejected: still wrong"),
    ]

    def test_each_answer_is_replayed_as_the_executors_own_turn(self):
        messages = build_prompt(Ticket("T-1", spec="s"), prior_turns=self.TURNS)

        assistants = [m.content for m in messages if m.role == "assistant"]
        self.assertEqual(assistants, [reply for reply, _ in self.TURNS])

    def test_the_ticket_is_asked_once_and_not_rewritten_by_what_followed(self):
        # The executor already answered this turn. Editing it now would make
        # its own replies look like answers to a question nobody asked.
        messages = build_prompt(
            Ticket("T-1", spec="s"), "the newest failure", prior_turns=self.TURNS
        )

        first_user = next(m for m in messages if m.role == "user")
        self.assertIn("## Spec", first_user.content)
        self.assertNotIn("the newest failure", first_user.content)

    def test_the_newest_failure_is_the_last_word(self):
        messages = build_prompt(
            Ticket("T-1", spec="s"), "the newest failure", prior_turns=self.TURNS
        )

        self.assertEqual(messages[-1].role, "user")
        self.assertIn("the newest failure", messages[-1].content)
        self.assertIn("Return the complete files again", messages[-1].content)

    def test_the_stored_failure_stands_in_when_no_context_is_passed(self):
        messages = build_prompt(Ticket("T-1", spec="s"), prior_turns=self.TURNS)
        self.assertIn("review rejected: still wrong", messages[-1].content)

    def test_the_flat_failure_block_is_superseded_by_the_turns(self):
        # The same failures, each one now attached to the answer that caused
        # it. Printing both spends the window to say it twice.
        messages = build_prompt(
            Ticket("T-1", spec="s"),
            prior_failures=["Attempt 1: lint failed"],
            prior_turns=self.TURNS,
        )

        self.assertNotIn("Earlier attempts on this ticket", _joined(messages))

    def test_an_old_exchange_is_droppable_and_the_newest_one_is_not(self):
        messages = build_prompt(
            Ticket("T-1", spec="s"), "the newest failure", prior_turns=self.TURNS
        )

        # system, ticket, [answer 1, its failure], answer 2, newest failure
        roles = [m.role for m in messages]
        self.assertEqual(roles, ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(
            [_droppable(m) for m in messages],
            [False, False, True, True, True, False],
        )


class TestTurnsAreRebuiltFromTheStepLog(unittest.TestCase):
    """Held in SQLite rather than in the attempt loop, so the transport stays
    stateless and a retry cycle inherits the thread."""

    def _store(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        return store, store.create_run("goal")

    def _attempt(self, store, run_id, reply, failure, *, name="review"):
        step = store.start_step(run_id, "T-1", "build")
        store.end_step(step, "ok" if reply else "failed", reply)
        if failure:
            step = store.start_step(run_id, "T-1", name)
            store.end_step(step, "failed", failure)

    def test_a_reply_is_paired_with_the_failure_that_followed_it(self):
        store, run_id = self._store()
        self._attempt(store, run_id, "first answer", "lint failed")
        self._attempt(store, run_id, "second answer", "review rejected")

        turns = store.ticket_turns(run_id, "T-1", limit=2)

        self.assertEqual([reply for reply, _ in turns], ["first answer", "second answer"])
        self.assertIn("lint failed", turns[0][1])

    def test_a_reply_with_no_failure_after_it_is_dropped(self):
        # An attempt can end without a failed step — a reply the harness could
        # not read is refused before anything runs. Pairing it with the next
        # failure would tell the executor its code caused something it never
        # reached.
        store, run_id = self._store()
        self._attempt(store, run_id, "unreadable answer", "")
        self._attempt(store, run_id, "second answer", "review rejected")

        turns = store.ticket_turns(run_id, "T-1", limit=4)

        self.assertEqual([reply for reply, _ in turns], ["second answer"])

    def test_only_the_last_few_turns_are_kept(self):
        store, run_id = self._store()
        for index in range(5):
            self._attempt(store, run_id, f"answer {index}", f"failure {index}")

        turns = store.ticket_turns(run_id, "T-1", limit=2)

        self.assertEqual([reply for reply, _ in turns], ["answer 3", "answer 4"])

    def test_a_ticket_that_has_not_run_has_no_turns(self):
        store, run_id = self._store()
        self.assertEqual(store.ticket_turns(run_id, "T-1"), [])


class TestConversationalExecutorIsOffUntilAskedFor(unittest.TestCase):
    """A flag, and an experiment. The flat prompt stays the default."""

    def _run(self, turns: int):
        orch, _root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 2
        orch.config.loop.executor_turns = turns
        seen: list[list[Message]] = []

        def call(_run_id, role, messages, **_kwargs):
            if role == "executor":
                seen.append(messages)
            return Completion(
                text={
                    "executor": "src/a.py\n```python\nx = 1\n```",
                    "tester": "tests/t_1_test.py\n```python\ndef test_a(): pass\n```",
                }.get(role, "REJECT\nthe error path is swallowed"),
                usage=Usage(),
                finish_reason="stop",
            )

        orch._call = call
        orch._work_ticket(run_id, Ticket("T-1", allowed_files=["src/a.py"]))
        return seen

    def test_off_by_default_nothing_is_replayed(self):
        seen = self._run(turns=0)

        self.assertEqual(len(seen), 2, "the ticket should have had two attempts")
        self.assertEqual([m.role for m in seen[-1] if m.role == "assistant"], [])

    def test_on_the_second_attempt_reads_its_own_answer(self):
        seen = self._run(turns=2)

        second = seen[-1]
        assistants = [m.content for m in second if m.role == "assistant"]
        self.assertEqual(assistants, ["src/a.py\n```python\nx = 1\n```"])
        self.assertIn("the error path is swallowed", second[-1].content)


class TestAWrongDiagnosisIsReplacedRatherThanParked(unittest.TestCase):
    """A reproduction that cannot be written is a measurement, not a dead end.
    The tester saying "this code already does what the report asks for" is a
    fact about the code, and the right use of it is to look somewhere else.

    One run parked on exactly that: the level really was initialised to 1, the
    reporter really did see 0, and the answer sat one layer away in a file the
    first hypothesis never named. The report was never what was disproved."""

    REPORT = "the game starts at level 0"

    def _orch(self, hypotheses=3):
        orch, root, run_id = _stub_orchestrator({"lint": "", "typecheck": "", "test": "pytest -q"})
        orch.config.loop.max_attempts = 1
        orch.config.loop.bug_hypotheses = hypotheses
        orch.store.set_run_status(run_id, "running")
        orch.store._connection.execute(
            "UPDATE runs SET source = ? WHERE id = ?", (self.REPORT, run_id)
        )
        orch.store._connection.commit()
        orch.store.add_tickets(
            run_id,
            [
                Ticket(
                    "BUG-001",
                    title="level starts at zero",
                    kind=TICKET_BUG,
                    spec="Game.new sets level to 0 and should set it to 1",
                    allowed_files=["src/game.py"],
                )
            ],
        )
        return orch, root, run_id

    def _second_hypothesis(self, **overrides):
        return json.dumps(
            {
                "title": "the view never updates",
                "spec": overrides.get("spec", "web/main.js throws before it reads the level"),
                "allowed_files": overrides.get("allowed_files", ["web/main.js"]),
                "reproduce": "the rendered level follows the game's level",
            }
        )

    def _drive(self, orch, root, *, planner, reproduces_on):
        """Reproduction fails until the given hypothesis is in scope."""
        seen: dict[str, list[str]] = {}
        state = {"scope": None}

        def call(_run_id, role, messages, **_kwargs):
            seen.setdefault(role, []).append(_joined(messages))
            if role == "planner":
                return Completion(text=planner.pop(0) if planner else "no idea",
                                  usage=Usage(), finish_reason="stop")
            if role == "tester":
                state["scope"] = orch.store.list_tickets(1)[0].allowed_files
                return Completion(
                    text="tests/bug_001_test.py\n```python\ndef test_x():\n    assert True\n```",
                    usage=Usage(), finish_reason="stop",
                )
            return Completion(text="ACCEPT", usage=Usage(), finish_reason="stop")

        def shell(_run_id, name, command):
            if not command.strip():
                return StepResult(ok=True, detail="")
            proven = state["scope"] == reproduces_on
            if proven:
                return StepResult(ok=False, detail="tests/bug_001_test.py::test_x FAILED\nassert 0 == 1")
            return StepResult(ok=True, detail="1 passed")

        orch._call = call
        orch._shell = shell
        return seen

    def test_a_disproved_explanation_is_replaced_and_the_ticket_continues(self):
        orch, root, run_id = self._orch()
        seen = self._drive(
            orch, root, planner=[self._second_hypothesis()], reproduces_on=["web/main.js"]
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        ticket = orch.store.list_tickets(run_id)[0]
        self.assertEqual(ticket.allowed_files, ["web/main.js"])
        self.assertIn("throws before it reads the level", ticket.spec)
        # It got past reproduction on the second hypothesis rather than parking.
        self.assertTrue(orch.store.reproduced(run_id, "BUG-001"))

    def test_the_planner_is_shown_the_report_and_what_disproved_the_guess(self):
        orch, root, run_id = self._orch()
        seen = self._drive(
            orch, root, planner=[self._second_hypothesis()], reproduces_on=["web/main.js"]
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        asked = seen["planner"][0]
        self.assertIn(self.REPORT, asked)
        self.assertIn("The explanation that was just disproved", asked)
        self.assertIn("Game.new sets level to 0", asked)

    def test_the_next_guess_cannot_be_the_last_one_again(self):
        orch, root, run_id = self._orch()
        seen = self._drive(
            orch,
            root,
            planner=[self._second_hypothesis(), self._second_hypothesis(spec="another idea")],
            reproduces_on=["never"],
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        second = seen["planner"][1]
        self.assertIn("Already ruled out", second)
        self.assertIn("Game.new sets level to 0", second)
        self.assertIn("throws before it reads the level", second)

    def test_the_block_lists_every_hypothesis_it_tried(self):
        # The work the ticket actually did. Without it the next person starts
        # from the report and repeats all of it.
        orch, root, run_id = self._orch()
        self._drive(
            orch,
            root,
            planner=[self._second_hypothesis(), self._second_hypothesis(spec="a third idea")],
            reproduces_on=["never"],
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        note = orch.store.list_tickets(run_id)[0].blocked_note
        self.assertEqual(orch.store.list_tickets(run_id)[0].status, TICKET_BLOCKED)
        self.assertIn("Hypotheses tried and ruled out", note)
        self.assertIn("Game.new sets level to 0", note)

    def test_a_planner_with_nothing_better_parks_immediately(self):
        # An honest question beats a third wrong ticket.
        orch, root, run_id = self._orch()
        seen = self._drive(orch, root, planner=["no idea at all"], reproduces_on=["never"])

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertEqual(orch.store.list_tickets(run_id)[0].status, TICKET_BLOCKED)
        self.assertEqual(len(seen["planner"]), 1)
        messages = " ".join(row["message"] for row in orch.store.events_after(0))
        self.assertIn("no further diagnosis", messages)

    def test_one_hypothesis_is_the_old_behaviour(self):
        orch, root, run_id = self._orch(hypotheses=1)
        seen = self._drive(
            orch, root, planner=[self._second_hypothesis()], reproduces_on=["web/main.js"]
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertEqual(orch.store.list_tickets(run_id)[0].status, TICKET_BLOCKED)
        self.assertNotIn("planner", seen)


class TestABugIsReproducedBeforeItIsFixed(unittest.TestCase):
    """The loop verifies what the criteria say, so a defect nobody wrote a
    criterion for survives the whole pipeline. Two shipped that way in one
    green run. A bug ticket inverts the order: a test that asserts the correct
    behavior is written first and must fail, and the fix is not done until that
    same test passes."""

    REPRO = "tests/bug_001_test.py"
    TEST_FAILURE = (
        "tests/bug_001_test.py::test_one_piece_per_tick FAILED\n"
        "assert 3 == 1  # three pieces locked in one tick"
    )

    def _orch(self, *, commands=None, ticket=None):
        orch, root, run_id = _stub_orchestrator(
            commands or {"lint": "", "typecheck": "", "test": "pytest -q"}
        )
        orch.config.loop.max_attempts = 2
        orch.store.add_tickets(
            run_id,
            [
                ticket
                or Ticket(
                    "BUG-001",
                    title="tick locks three pieces",
                    kind=TICKET_BUG,
                    spec="Game.tick should lock at most one piece per call",
                    allowed_files=["src/a.py"],
                    context="tick(3000) locks at most one piece",
                )
            ],
        )
        return orch, root, run_id

    def _shell_until_fixed(self, root: Path):
        """The suite fails while the bug is on disk and passes once it is not."""

        def shell(_run_id, name, command):
            if not command.strip():
                return StepResult(ok=True, detail="")
            source = root / "src" / "a.py"
            fixed = source.exists() and "fixed" in source.read_text(encoding="utf-8")
            if fixed:
                return StepResult(ok=True, detail="1 passed")
            return StepResult(ok=False, detail=self.TEST_FAILURE)

        return shell

    def _calls(self, orch, *, tester: str, executor: str):
        seen: dict[str, list[str]] = {}

        def call(_run_id, role, messages, **_kwargs):
            seen.setdefault(role, []).append(_joined(messages))
            text = {"tester": tester, "executor": executor}.get(role, "ACCEPT")
            return Completion(text=text, usage=Usage(), finish_reason="stop")

        orch._call = call
        return seen

    _GOOD_TEST = (
        "tests/bug_001_test.py\n```python\ndef test_one_piece_per_tick():\n"
        "    assert locked(3000) == 1\n```"
    )
    _FIX = "src/a.py\n```python\n# fixed\ndef tick():\n    pass\n```"

    def test_the_reproduction_is_written_first_and_has_to_fail(self):
        orch, root, run_id = self._orch()
        orch._shell = self._shell_until_fixed(root)
        seen = self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertEqual(orch.store.list_tickets(run_id)[0].status, TICKET_DONE)
        # The proof is durable, and it is the failure the test produced.
        self.assertIn("three pieces locked", orch.store.reproduced(run_id, "BUG-001"))
        self.assertTrue((root / self.REPRO).exists())
        # The executor is told what failed and that it cannot edit the proof.
        self.assertIn("three pieces locked", seen["executor"][0])
        self.assertIn("outside this ticket's scope", seen["executor"][0])

    def test_no_further_tests_are_authored_for_a_bug_ticket(self):
        # The contract was written before the fix, by a role that could not see
        # it. Authoring more now would let the party being judged add to it.
        orch, root, run_id = self._orch()
        orch._shell = self._shell_until_fixed(root)
        self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        names = [
            row["name"]
            for row in orch.store._connection.execute(
                "SELECT name FROM steps WHERE ticket_id = 'BUG-001'"
            )
        ]
        self.assertIn("reproduce", names)
        self.assertNotIn("tests", names)

    def test_the_reviewer_is_shown_the_red_to_green_evidence(self):
        orch, root, run_id = self._orch()
        orch._shell = self._shell_until_fixed(root)
        seen = self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        review = seen["reviewer"][-1]
        self.assertIn("reproduced before it was fixed", review)
        self.assertIn("three pieces locked", review)
        self.assertIn("fixes the *cause*", review)

    def test_a_reproduction_that_passes_proves_nothing_and_parks(self):
        orch, _root, run_id = self._orch()
        orch._shell = lambda _r, _n, command: StepResult(ok=True, detail="1 passed")
        seen = self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        stored = orch.store.list_tickets(run_id)[0]
        self.assertEqual(stored.status, TICKET_BLOCKED)
        self.assertIn("could not be reproduced", stored.blocked_note)
        # Asked twice with the passing output quoted back, then parked. The
        # executor is never reached: there is nothing to fix on faith.
        self.assertEqual(len(seen["tester"]), 2)
        self.assertNotIn("executor", seen)
        self.assertIn("proved nothing", seen["tester"][1])

    def test_an_unreproducible_bug_is_not_retried_forever(self):
        """A real run spent fifteen retry cycles on one report — two tester
        calls apiece — and would have spent them forever under `retryCycles:
        -1`. Nothing between cycles makes an undemonstrable fault
        demonstrable, and neither existing brake catches it: the ticket never
        takes an attempt, so there is no respec to come back unchanged, and the
        tester's prose varies enough that the evidence fingerprint differs
        every time."""
        orch, _root, run_id = self._orch()
        orch.config.loop.retry_cycles = -1
        orch._shell = lambda _r, _n, _c: StepResult(ok=True, detail="1 passed")
        self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)
        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        went_again = orch._retry_cycle(run_id, "blocked")

        self.assertFalse(went_again)
        messages = " ".join(row["message"] for row in orch.store.events_after(0))
        self.assertIn("never reproduced", messages)

    def test_a_bug_that_did_reproduce_is_retried_normally(self):
        # The exclusion is about proof, not about being a bug ticket: one that
        # demonstrated its fault and then failed to fix it is ordinary work.
        orch, root, run_id = self._orch()
        orch.config.loop.retry_cycles = -1
        orch.config.loop.respec_on_retry = False
        orch._shell = lambda _r, _n, _c: StepResult(ok=False, detail=self.TEST_FAILURE)
        self._calls(orch, tester=self._GOOD_TEST, executor="src/a.py\n```python\n# no fix\n```")
        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertTrue(orch._retry_cycle(run_id, "blocked"))

    def test_the_block_names_the_layers_the_suite_cannot_reach(self):
        """The case this came from: a report said the game starts at level 0.
        The Rust set it to 1, so no test of that code could fail — and the
        symptom was real, in a JavaScript file that threw on its second line
        and left the page showing a hardcoded `Level: 0`. `cargo test` runs no
        JavaScript, so nothing in the pipeline could reach it."""
        orch, root, run_id = self._orch()
        (root / "web").mkdir()
        (root / "web" / "main.js").write_text("run()\n", encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        orch._shell = lambda _r, _n, _c: StepResult(ok=True, detail="1 passed")
        self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        note = orch.store.list_tickets(run_id)[0].blocked_note
        self.assertIn(".js", note)
        self.assertIn("no ticket here can reach it", note)

    def test_a_single_language_project_gets_no_such_note(self):
        orch, root, run_id = self._orch()
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        orch._shell = lambda _r, _n, _c: StepResult(ok=True, detail="1 passed")
        self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertNotIn("also contains", orch.store.list_tickets(run_id)[0].blocked_note)

    def test_a_report_too_vague_to_assert_is_handed_back(self):
        orch, _root, run_id = self._orch()
        orch._shell = lambda _r, _n, _c: StepResult(ok=False, detail=self.TEST_FAILURE)
        seen = self._calls(
            orch,
            tester="BLOCKED: the report does not say what value was expected",
            executor=self._FIX,
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        stored = orch.store.list_tickets(run_id)[0]
        self.assertEqual(stored.status, TICKET_BLOCKED)
        self.assertIn("cannot be turned into a test", stored.blocked_note)
        self.assertEqual(len(seen["tester"]), 1, "a refusal is an answer, not a retry")

    def test_the_fix_cannot_edit_its_own_proof(self):
        orch, root, run_id = self._orch()
        orch._shell = self._shell_until_fixed(root)
        self._calls(
            orch,
            tester=self._GOOD_TEST,
            executor=self._FIX
            + f"\n\n{self.REPRO}\n```python\ndef test_one_piece_per_tick():\n    assert True\n```",
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        kept = (root / self.REPRO).read_text(encoding="utf-8")
        self.assertIn("locked(3000) == 1", kept)
        self.assertNotIn("assert True", kept)

    def test_the_reproduction_survives_a_ticket_that_never_passed(self):
        # An unverified feature test is deleted, because it fails every later
        # ticket and none of them can reach it. A reproduction is the opposite:
        # it is the one assertion here demonstrated against real behavior, and
        # it is half of what the ticket was for.
        orch, root, run_id = self._orch()
        orch._shell = lambda _r, _n, _c: StepResult(ok=False, detail=self.TEST_FAILURE)
        self._calls(
            orch, tester=self._GOOD_TEST, executor="src/a.py\n```python\n# no fix\n```"
        )

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])
        orch._sweep_orphan_tests(run_id)

        self.assertEqual(orch.store.list_tickets(run_id)[0].status, TICKET_FAILED)
        self.assertTrue((root / self.REPRO).exists(), "the evidence must outlive the ticket")

    def test_a_reproduction_that_does_not_build_is_not_evidence(self):
        # A test that will not import fails the command for a reason that has
        # nothing to do with the bug, and the executor cannot fix it — the file
        # is outside its scope. Distinct from a failing assertion, which names
        # the same file and *is* the evidence.
        orch, _root, run_id = self._orch()
        broken = (
            "ImportError: cannot import name 'locked'\n"
            "tests/bug_001_test.py:1: in <module>\n"
        )
        orch._shell = lambda _r, _n, _c: StepResult(ok=False, detail=broken)
        seen = self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        stored = orch.store.list_tickets(run_id)[0]
        self.assertEqual(stored.status, TICKET_BLOCKED)
        self.assertIn("does not build", stored.blocked_note)
        self.assertEqual(len(seen["tester"]), 2)
        self.assertIn("errors are in the file you are about to write", seen["tester"][1])

    def test_a_second_cycle_does_not_reproduce_it_again(self):
        # Once the fix lands the test passes, so re-running reproduction would
        # find nothing wrong and park a ticket whose work is nearly done.
        orch, root, run_id = self._orch()
        orch._shell = self._shell_until_fixed(root)
        step = orch.store.start_step(run_id, "BUG-001", "reproduce")
        orch.store.end_step(step, "ok", self.TEST_FAILURE)
        seen = self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        self.assertNotIn("tester", seen)
        self.assertIn("three pieces locked", seen["executor"][0])

    def test_a_project_with_no_test_command_cannot_prove_anything(self):
        orch, _root, run_id = self._orch(commands={"lint": "", "typecheck": "", "test": ""})
        self._calls(orch, tester=self._GOOD_TEST, executor=self._FIX)

        orch._work_ticket(run_id, orch.store.list_tickets(run_id)[0])

        stored = orch.store.list_tickets(run_id)[0]
        self.assertEqual(stored.status, TICKET_BLOCKED)
        self.assertIn("no test command", stored.blocked_note)

    def test_the_baseline_never_excuses_the_reproduction(self):
        # The hole this closes: on a retry cycle the reproduction is already on
        # disk and already failing, so it pre-dates the attempt by every
        # measure the amnesty uses.
        orch, _root, run_id = self._orch()
        failure = (
            "error[E0001]: assertion failed\n"
            "  --> tests/bug_001_test.rs:3:1\n"
        )
        orch._shell = lambda _r, _n, _c: StepResult(ok=False, detail=failure)
        ticket = orch.store.list_tickets(run_id)[0]

        excused = orch._baseline_failures(run_id, ticket)
        not_excused = orch._baseline_failures(
            run_id, ticket, extra_scope=["tests/bug_001_test.rs"]
        )

        self.assertTrue(excused.get("test"), "an unrelated failure is still excused")
        self.assertEqual(not_excused, {})


class TestARetryCycleRemembersWhatFailed(unittest.TestCase):
    """`history` and `rejections` are locals in the attempt loop, and a retry
    cycle enters it fresh. So cycle 2's reviewer met a ticket it had already
    rejected three times as though for the first time and re-raised the same
    objections, while the one nudge designed to notice a third identical
    objection — "a rejection that repeats is evidence the spec is wrong" —
    could never fire, because the list it reads was empty exactly when it
    mattered. Both records were durable in the step log the whole time."""

    def _seeded(self, attempt_base: int):
        orch, root, run_id = _stub_orchestrator()
        orch.config.loop.max_attempts = 1

        step = orch.store.start_step(run_id, "TT-001", "review")
        orch.store.end_step(step, "failed", "REJECT: the error path is swallowed")
        step = orch.store.start_step(run_id, "TT-001", "lint")
        orch.store.end_step(step, "failed", "error[E0433]: unresolved import")

        seen: dict[str, str] = {}

        def call(_run_id, role, messages, **_kwargs):
            seen[role] = _joined(messages)
            return {
                "executor": "src/game.rs\n```rust\npub fn go() {}\n```",
                "tester": "tests/tt_001_test.rs\n```rust\n#[test]\nfn a() {}\n```",
            }.get(role, "ACCEPT")

        orch._call = lambda run_id, role, messages, **kw: Completion(
            text=call(run_id, role, messages, **kw), usage=Usage(), finish_reason="stop"
        )
        orch._work_ticket(
            run_id,
            Ticket(
                "TT-001",
                allowed_files=["src/game.rs"],
                criteria=["go() exists"],
                attempt_base=attempt_base,
            ),
        )
        return seen

    def test_a_second_cycle_reviewer_sees_the_first_cycles_rejections(self):
        seen = self._seeded(attempt_base=3)
        self.assertIn("the error path is swallowed", seen["reviewer"])
        self.assertIn("do not replace it with a fresh objection", seen["reviewer"])

    def test_a_second_cycle_executor_sees_the_first_cycles_failures(self):
        seen = self._seeded(attempt_base=3)
        self.assertIn("E0433", seen["executor"])

    def test_a_first_cycle_starts_with_nothing_to_remember(self):
        # The step log is per ticket, not per cycle. Seeding unconditionally
        # would show a ticket its own current cycle back to itself.
        seen = self._seeded(attempt_base=0)
        self.assertNotIn("already rejected", seen["reviewer"])
        self.assertNotIn("Earlier attempts on this ticket", seen["executor"])


class TestFailureHistoryReachesBothRoles(unittest.TestCase):
    """`failure_context` carries only the newest failure, which is what lets an
    executor oscillate — fix A breaks B, fix B brings A back — for its whole
    retry budget with nothing able to see the cycle."""

    def test_earlier_failures_are_carried_forward_to_the_executor(self):
        prompt = _joined(
            build_prompt(
                Ticket("T-1", spec="s"),
                "lint failed:\nerror: B is broken",
                prior_failures=["Attempt 1: lint failed:\nerror: A is broken"],
            )
        )

        self.assertIn("A is broken", prompt)
        self.assertIn("B is broken", prompt)
        self.assertIn("undoing each other", prompt)

    def test_a_first_attempt_carries_no_history_section(self):
        prompt = _joined(build_prompt(Ticket("T-1", spec="s")))
        self.assertNotIn("Earlier attempts on this ticket", prompt)

    def test_the_reviewer_is_shown_its_own_earlier_rejections(self):
        prompt = _joined(
            review_prompt(
                Ticket("T-1", spec="s"),
                "diff --git a/x b/x",
                prior_verdicts=["REJECT\nthe error path is swallowed"],
            )
        )

        self.assertIn("the error path is swallowed", prompt)
        # The instruction that stops three attempts dying on three unrelated
        # objections is the whole point of showing them.
        self.assertIn("do not replace it with a fresh objection", prompt)

    def test_a_first_review_carries_no_prior_verdicts(self):
        prompt = _joined(review_prompt(Ticket("T-1", spec="s"), "diff"))
        self.assertNotIn("already rejected", prompt)

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

    def test_the_reviewer_must_cite_what_it_looked_at(self):
        # Reviewers reject work that is plainly present — one said a canvas
        # "does not specify a width of 240 and a height of 480" about a file
        # whose second line said exactly that, three times running.
        system = review_prompt(Ticket("T-1", spec="s"), "diff")[0].content

        self.assertIn("EVERY objection must cite", system)
        self.assertIn("name the exact text", system)

    def test_a_verdict_that_echoes_the_prompt_is_not_fed_back(self):
        # Observed: the reviewer copied the prompt's own headings into its
        # verdict, which was then quoted into the next attempt's prompt and
        # offered for copying again. The block nested on itself every round.
        orch, _, run_id = _stub_orchestrator()
        echoed = (
            "REJECT\nthe error path is swallowed\n\n"
            "## You have already rejected this ticket\n"
            "### Attempt 1\n"
            "REJECT\nsomething else entirely\n\n"
            "Read these before deciding.\n"
        )
        orch._call = _replies("src/a.py\n```python\nx = 1\n```", echoed)
        rejections: list[str] = []

        orch._attempt(
            run_id, Ticket("T-1", allowed_files=["src/a.py"]), "", rejections=rejections
        )

        self.assertEqual(rejections, ["REJECT\nthe error path is swallowed"])
        self.assertNotIn("already rejected", rejections[0])


class TestAnUnreadableReplyIsAskedForAgain(unittest.TestCase):
    """A reply that did not parse is a formatting mistake, not a failed
    implementation, and spending a whole attempt on one buys nothing — the next
    attempt re-reads the same spec and the model repeats itself. One ticket lost
    six of its nine attempts to a fenced block with no path line above it, while
    the three that parsed drew specific review objections it never had the
    budget left to answer."""

    GOOD = "src/game.rs\n```rust\npub struct Game;\n```"
    NO_PATH = "Looking at the error, I can see the issue:\n\n```rust\npub struct Game;\n```"

    def _orchestrator(self):
        orch, root, run_id = _stub_orchestrator()
        return orch, root, run_id

    def test_a_second_ask_inside_the_attempt_recovers_the_work(self):
        orch, root, run_id = self._orchestrator()
        replies = iter([self.NO_PATH, self.GOOD])
        asked: list[str] = []

        def call(_run_id, role, messages, **_kwargs):
            if role != "executor":
                return Completion(text="ACCEPT\nfine", usage=Usage())
            asked.append("\n".join(m.content for m in messages))
            return Completion(text=next(replies), usage=Usage())

        orch._call = call
        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["src/game.rs"]), "")

        self.assertTrue(result.ok)
        self.assertEqual(len(asked), 2)
        # The second ask carries the complaint, and tells it not to rewrite.
        self.assertIn("could not be read", asked[1])
        self.assertIn("no file path", asked[1])
        self.assertIn("code was never the problem", asked[1])
        self.assertEqual(
            (root / "src" / "game.rs").read_text(encoding="utf-8").strip(),
            "pub struct Game;",
        )

    def test_a_readable_reply_is_never_asked_twice(self):
        orch, _root, run_id = self._orchestrator()
        builds = []

        def call(_run_id, role, _messages, **_kwargs):
            if role != "executor":
                return Completion(text="ACCEPT\nfine", usage=Usage())
            builds.append(1)
            return Completion(text=self.GOOD, usage=Usage())

        orch._call = call
        orch._attempt(run_id, Ticket("T-1", allowed_files=["src/game.rs"]), "")

        self.assertEqual(len(builds), 1)

    def test_twice_unreadable_spends_the_attempt(self):
        orch, _root, run_id = self._orchestrator()
        builds = []

        def call(_run_id, _role, _messages, **_kwargs):
            builds.append(1)
            return Completion(text=self.NO_PATH, usage=Usage())

        orch._call = call
        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["src/game.rs"]), "")

        self.assertFalse(result.ok)
        self.assertIn("no file path", result.detail)
        self.assertEqual(len(builds), 2)

    def test_a_blocked_reply_is_a_decision_not_a_formatting_mistake(self):
        orch, _root, run_id = self._orchestrator()
        builds = []

        def call(_run_id, _role, _messages, **_kwargs):
            builds.append(1)
            return Completion(text="BLOCKED: two criteria contradict", usage=Usage())

        orch._call = call
        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["src/game.rs"]), "")

        self.assertTrue(result.blocked)
        self.assertEqual(len(builds), 1)

    def test_a_reply_with_no_file_content_is_not_asked_again(self):
        # The 1.2 case: nothing to write may be the honest answer, and asking
        # again would talk a finished ticket into inventing an edit.
        orch, _root, run_id = self._orchestrator()
        builds = []

        def call(_run_id, role, _messages, **_kwargs):
            if role != "executor":
                return Completion(text="ACCEPT\nalready satisfied", usage=Usage())
            builds.append(1)
            return Completion(text="The files already implement the spec.", usage=Usage())

        orch._call = call
        orch._attempt(run_id, Ticket("T-1", allowed_files=["src/game.rs"]), "")

        self.assertEqual(len(builds), 1)

    def test_a_partly_readable_reply_is_kept_rather_than_risked(self):
        # Something parsed, so it is written and the attempt reports what is
        # missing. Asking again could trade a partial answer for a worse one.
        orch, root, run_id = self._orchestrator()
        f = "`" * 3
        builds = []

        def call(_run_id, _role, _messages, **_kwargs):
            builds.append(1)
            return Completion(
                text=f"build.sh\n{f}sh\ncargo build\n{f}\n\n"
                f"README.md\n{f}\n# T\n\n{f}sh\nx\n{f}\n\n## More\n\ndone\n{f}\n",
                usage=Usage(),
            )

        orch._call = call
        orch._attempt(
            run_id, Ticket("T-1", allowed_files=["build.sh", "README.md"]), ""
        )

        self.assertEqual(len(builds), 1)
        self.assertTrue((root / "build.sh").exists())


class TestAnExecutorThatWritesNothing(unittest.TestCase):
    """Disk is never reverted between attempts and the executor is shown the
    current files, so "there is nothing to change" is sometimes the honest
    answer. Failing the attempt for it is how a finished ticket failed three
    times a cycle — one reply read "Looking at the files provided, I can see
    they already implement the spec correctly." It did."""

    NOTHING = "Looking at the files provided, they already implement the spec."

    def test_an_empty_reply_is_reviewed_against_disk_instead_of_failing(self):
        orch, root, run_id = _stub_orchestrator()
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        seen: list[str] = []

        def call(_run_id, role, messages, **_kwargs):
            seen.append(role)
            if role == "reviewer":
                return Completion(
                    text="ACCEPT\nalready satisfied on disk", usage=Usage()
                )
            return Completion(text=self.NOTHING, usage=Usage())

        orch._call = call
        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["src/a.py"]), "")

        self.assertTrue(result.ok)
        self.assertIn("reviewer", seen)

    def test_the_file_already_there_is_left_alone(self):
        orch, root, run_id = _stub_orchestrator()
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        orch._call = lambda _r, role, *_a, **_k: Completion(
            text="ACCEPT\nfine" if role == "reviewer" else self.NOTHING, usage=Usage()
        )

        orch._attempt(run_id, Ticket("T-1", allowed_files=["src/a.py"]), "")

        self.assertEqual(
            (root / "src" / "a.py").read_text(encoding="utf-8"), "x = 1\n"
        )

    def test_no_test_file_is_authored_for_an_attempt_that_wrote_nothing(self):
        # A test on disk for an attempt that changed nothing is the orphan the
        # fixed-path rule exists to prevent.
        orch, _root, run_id = _stub_orchestrator()
        roles: list[str] = []

        def call(_run_id, role, *_a, **_k):
            roles.append(role)
            return Completion(
                text="ACCEPT\nfine" if role == "reviewer" else self.NOTHING,
                usage=Usage(),
            )

        orch._call = call
        orch._attempt(
            run_id, Ticket("T-1", allowed_files=["src/a.py"], criteria=["c"]), ""
        )

        self.assertNotIn("tester", roles)

    def test_a_reply_the_parser_could_not_read_still_fails(self):
        # The distinction 1.0 drew: content that was meant to be a file, and
        # arrived unreadable, is a failure and says which shape it was.
        orch, _root, run_id = _stub_orchestrator()
        fence = "`" * 3
        orch._call = _replies(f"{fence}python\nx = 1\n{fence}\n")

        result = orch._attempt(run_id, Ticket("T-1", allowed_files=["src/a.py"]), "")

        self.assertFalse(result.ok)
        self.assertIn("no file path", result.detail)


class TestStrippingThePromptEcho(unittest.TestCase):
    """What the reviewer wrote survives; what it copied does not."""

    def test_a_clean_verdict_is_untouched(self):
        verdict = "REJECT\n- `main.js:12` calls game_input(' ') rather than 4."
        self.assertEqual(strip_prompt_echo(verdict), verdict)

    def test_stripping_is_idempotent(self):
        once = strip_prompt_echo("ACCEPT\nfine\n\n### Attempt 1\nold\n")
        self.assertEqual(strip_prompt_echo(once), once)

    def test_a_quoted_heading_inside_a_citation_survives(self):
        # 2.1 asks the reviewer to quote what it looked at, and what it looked
        # at may be a README. An ordinary markdown heading in a citation is not
        # an echo of the prompt.
        verdict = "REJECT\nREADME.md line 8 reads:\n  ## Building\nwhich never mentions rustup."
        self.assertEqual(strip_prompt_echo(verdict), verdict)

    def test_a_wholesale_copy_of_the_prompt_keeps_only_the_verdict(self):
        self.assertEqual(
            strip_prompt_echo("ACCEPT\nlooks right\n\n## Spec\nbuild the thing\n"),
            "ACCEPT\nlooks right",
        )

    def test_an_empty_verdict_survives_the_trip(self):
        self.assertEqual(strip_prompt_echo(""), "")


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


class TestTheBaselineExcuseStopsAtTheTicketsScope(unittest.TestCase):
    """Amnesty covers what a ticket cannot fix, and nothing else.

    Nothing reverts a failed ticket, so a retry starts with the previous
    cycle's breakage on disk — and used to collect a baseline that forgave it.
    One ticket left four clippy errors in `src/board.rs`, was requeued, and
    passed its lint step on the grounds that the errors pre-dated the attempt.
    They did. It wrote them.
    """

    LINT = (
        "error: casting to the same type is unnecessary (`i32` -> `i32`)\n"
        "  --> src/board.rs:64:48\n"
        "   |\n"
        "error[E0308]: mismatched types\n"
        "  --> web/main.js:12:3\n"
        "   |\n"
    )

    def _baseline(self, allowed):
        orch, _root, run_id = _stub_orchestrator(
            commands={"lint": "cargo clippy", "typecheck": "", "test": ""}
        )
        orch._shell = lambda *_a, **_k: StepResult(ok=False, detail=self.LINT)
        return orch._baseline_failures(
            run_id, Ticket("T-1", allowed_files=allowed)
        ).get("lint", set())

    def test_breakage_in_a_file_the_ticket_may_write_is_not_excused(self):
        excused = self._baseline(["src/board.rs"])

        self.assertEqual(len(excused), 1)
        self.assertNotIn("board.rs", " ".join(excused))

    def test_breakage_outside_the_scope_is_still_excused(self):
        # The chain the baseline exists to break: an error in a file the ticket
        # cannot open must not spend its three attempts.
        excused = self._baseline(["web/main.js"])

        self.assertEqual(len(excused), 1)
        self.assertIn("board.rs", " ".join(excused))

    def test_a_ticket_owning_everything_is_excused_nothing(self):
        self.assertEqual(self._baseline(["src/board.rs", "web/main.js"]), set())

    def test_a_glob_scope_still_claims_its_files(self):
        self.assertNotIn("board.rs", " ".join(self._baseline(["src/**"])))

    def test_scope_matching_folds_case(self):
        # `signatures` lowercases, so a `Cargo.toml` in allowed_files would
        # otherwise never match the `cargo.toml` in its own diagnostic.
        self.assertTrue(
            Orchestrator._signature_scope(
                "error: invalid manifest --> cargo.toml:3:1", ["Cargo.toml"]
            )
        )

    def test_a_signature_with_no_location_stays_excusable(self):
        # Nothing to attribute it to, and blaming a ticket for a diagnostic
        # that names no file is the wrong direction to guess in.
        self.assertFalse(
            Orchestrator._signature_scope("error: linking failed", ["src/board.rs"])
        )


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


class TestAnImpossibleBudgetBlamesTheConfig(unittest.TestCase):
    """`input_budget = window - output - margin` can come out at or below zero,
    and then no prompt of any size fits. Reporting that as a ticket too large
    to run sends the reader off to split tickets that were never the problem —
    one run said exactly that about six tickets of 1-3k tokens while the real
    cause was a model missing from the server."""

    class _Model(Provider):
        kind = "stub"

        def __init__(self, window: int, output: int):
            super().__init__("local", {})
            self._window, self._output = window, output

        def complete(self, messages, *, max_tokens, temperature=0.2, timeout=600):
            raise NotImplementedError

        def capabilities(self):
            return Capabilities(context_window=self._window, max_output_tokens=self._output)

        def count_tokens(self, messages):
            return sum(len(m.content) for m in messages)

    def _fit(self, window, output, text="x" * 400, droppable=None):
        gate = BudgetGate(Store(Path(tempfile.mkdtemp()) / "t.db"), {})
        return gate.fit(
            self._Model(window, output),
            [Message(role="user", content=text)],
            max_output=output,
            droppable=droppable,
        )

    def test_a_negative_budget_names_the_configuration(self):
        with self.assertRaises(ContextOverflow) as caught:
            self._fit(window=8192, output=65536)
        message = str(caught.exception)

        self.assertIn("no room for a prompt of any size", message)
        self.assertIn("configuration or discovery failure", message)
        # Says so outright rather than leaving the reader to infer it — the
        # advice this replaces was "split it", which cannot help.
        self.assertIn("not a ticket that is too large", message)
        self.assertNotIn("Split the ticket", message)

    def test_it_reports_both_numbers_that_produced_it(self):
        with self.assertRaises(ContextOverflow) as caught:
            self._fit(window=8192, output=65536)
        message = str(caught.exception)

        self.assertIn("8.2k", message)
        self.assertIn("65.5k", message)

    def test_it_fires_before_any_optional_context_is_dropped(self):
        """Dropping memory to fit an impossible budget is wasted work, and the
        message it would produce afterwards is the wrong one."""
        seen = []

        with self.assertRaises(ContextOverflow) as caught:
            self._fit(window=8192, output=65536,
                      droppable=lambda m: seen.append(m) or True)

        self.assertEqual(seen, [])
        self.assertIn("no room for a prompt of any size", str(caught.exception))

    def test_a_genuinely_oversized_ticket_still_says_so(self):
        """The ordinary case has to keep its own advice."""
        with self.assertRaises(ContextOverflow) as caught:
            self._fit(window=4096, output=1024, text="x" * 90_000)

        self.assertIn("Split the ticket", str(caught.exception))

    def test_a_prompt_that_fits_is_untouched(self):
        kept = self._fit(window=131072, output=8192, text="x" * 400)
        self.assertEqual(len(kept), 1)


class TestTheLoopProbesBeforeItSpends(unittest.TestCase):
    """`forge doctor` catches a dead endpoint in two seconds. `forge go` did
    not ask, so a missing model produced a full backlog of blocked tickets, a
    respec over each, and a stop — every message describing the symptom rather
    than the cause."""

    def _orchestrator(self, preflight=True, model="stub"):
        root = Path(tempfile.mkdtemp())
        config = Config(
            root=root,
            models={"m": {"kind": "openai", "baseUrl": "http://127.0.0.1:1/v1",
                          "model": model, "contextWindow": 8192,
                          "maxOutputTokens": 1024}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
            commands={"lint": "", "typecheck": "", "test": ""},
        )
        config.loop.preflight = preflight
        store = Store(config.db_path)
        run_id = store.create_run("goal")
        store.add_tickets(run_id, [Ticket("T-1")])
        return Orchestrator(config, store), store, run_id

    def test_an_unreachable_model_stops_the_run_before_any_ticket(self):
        orch, store, run_id = self._orchestrator()
        worked = []
        orch._work_ticket = lambda *a, **k: worked.append(a)

        outcome = orch.run(run_id)

        self.assertEqual(outcome, "failed")
        self.assertEqual(worked, [])
        self.assertEqual(store.list_tickets(run_id)[0].status, TICKET_PENDING)

    def test_it_says_which_role_and_that_nothing_was_spent(self):
        orch, store, run_id = self._orchestrator()
        orch._work_ticket = lambda *a, **k: None

        orch.run(run_id)

        messages = " ".join(e["message"] for e in store.events_after(0))
        self.assertIn("Cannot start", messages)
        self.assertIn("Nothing has been spent", messages)
        self.assertIn("forge doctor", messages)

    def test_each_model_is_probed_once_not_each_role(self):
        """Four roles on one model is the common config; it should cost one
        call, not four."""
        orch, _store, run_id = self._orchestrator()
        calls = []

        def health(self):
            calls.append(self.name)
            return "ok"

        orch._work_ticket = lambda *a, **k: None
        with unittest.mock.patch.object(OpenAICompatProvider, "health", health):
            orch._preflight(run_id)

        self.assertEqual(calls, ["m"])

    def test_a_reachable_model_lets_the_run_proceed(self):
        orch, _store, run_id = self._orchestrator()
        with unittest.mock.patch.object(
            OpenAICompatProvider, "health", lambda self: "ok name=m"
        ):
            self.assertEqual(orch._preflight(run_id), [])

    def test_the_probe_can_be_turned_off(self):
        orch, _store, run_id = self._orchestrator(preflight=False)
        self.assertEqual(orch._preflight(run_id), [])


class TestGeneratedModelfiles(unittest.TestCase):
    """The settings a Modelfile carries are exactly the ones nothing else can
    reach, so hand-writing them is where the drift lives — one setup carried
    `num_ctx 32768` across three models trained for eight times that, and
    nothing reported it."""

    SHOW_ALIAS = {
        "parameters": 'top_k                 40\ntemperature           0.7\nstop  "<|im_end|>"\n',
        "details": {"parent_model": "devstral:24b"},
        "model_info": {"llama.context_length": 131072},
    }
    SHOW_BASE = {
        "parameters": "",
        "details": {"parent_model": ""},
        "model_info": {"llama.context_length": 131072},
    }

    def _config(self, model, contextWindow=None, kind="openai"):
        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        block = {"kind": kind, "baseUrl": "http://127.0.0.1:11434/v1", "model": model}
        if contextWindow:
            block["contextWindow"] = contextWindow
        (root / ".hybridforge" / "config.json").write_text(json.dumps({
            "models": {"local": block},
            "roles": {r: "local" for r in ("planner", "executor", "tester", "reviewer")},
        }), encoding="utf-8")
        return Config.load(root)

    def _plan(self, config, show):
        mod = sys.modules["forge.providers.openai_compat"]
        with unittest.mock.patch.object(mod, "post_json", lambda *a, **k: show), \
             unittest.mock.patch.object(mod, "get_json", lambda *a, **k: {"models": []}):
            return modelfiles.plan(config)

    def test_a_tuned_alias_is_rebuilt_under_its_own_name(self):
        entries = self._plan(self._config("forge-alt"), self.SHOW_ALIAS)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].create_as, "forge-alt")
        self.assertFalse(entries[0].rename)

    def test_the_modelfile_is_from_the_real_base_not_the_alias(self):
        """`FROM forge-alt` on a file used to rebuild `forge-alt` is circular
        and loses the weights it was derived from."""
        entries = self._plan(self._config("forge-alt"), self.SHOW_ALIAS)

        self.assertTrue(entries[0].text.startswith("FROM devstral:24b\n"))

    def test_a_base_model_gets_a_new_name_rather_than_being_overwritten(self):
        """Building under the base's own name replaces the weights the file is
        FROM — the one outcome this must never produce."""
        entries = self._plan(self._config("devstral:24b"), self.SHOW_BASE)

        self.assertEqual(entries[0].create_as, "forge-local")
        self.assertTrue(entries[0].rename)
        self.assertTrue(entries[0].text.startswith("FROM devstral:24b\n"))

    def test_the_base_models_own_sampling_recipe_is_preserved(self):
        entries = self._plan(self._config("forge-alt"), self.SHOW_ALIAS)

        self.assertIn("PARAMETER top_k 40", entries[0].text)

    def test_defaults_fill_in_when_the_base_ships_nothing(self):
        entries = self._plan(self._config("devstral:24b"), self.SHOW_BASE)

        self.assertIn("PARAMETER top_k 20", entries[0].text)
        self.assertIn("PARAMETER min_p 0", entries[0].text)

    def test_a_configured_window_wins_over_the_trained_maximum(self):
        entries = self._plan(self._config("forge-alt", contextWindow=65536), self.SHOW_ALIAS)

        self.assertIn("PARAMETER num_ctx 65536", entries[0].text)
        self.assertIn("trained for 131,072", entries[0].text)

    def test_a_full_window_carries_no_smaller_note(self):
        entries = self._plan(self._config("forge-alt", contextWindow=131072), self.SHOW_ALIAS)

        self.assertIn("PARAMETER num_ctx 131072", entries[0].text)
        self.assertNotIn("trained for", entries[0].text)

    def test_non_ollama_endpoints_are_skipped(self):
        """A Modelfile means nothing to vLLM or OpenRouter, and writing one
        would imply otherwise."""
        config = self._config("gpt-4o", kind="anthropic")

        self.assertEqual(modelfiles.plan(config), [])

    def test_writing_puts_them_under_the_project_config_dir(self):
        config = self._config("forge-alt")
        mod = sys.modules["forge.providers.openai_compat"]
        with unittest.mock.patch.object(mod, "post_json", lambda *a, **k: self.SHOW_ALIAS), \
             unittest.mock.patch.object(mod, "get_json", lambda *a, **k: {"models": []}):
            written = modelfiles.write(config)

        self.assertEqual(len(written), 1)
        entry, path = written[0]
        self.assertEqual(path.name, "Modelfile.local")
        self.assertEqual(path.parent, config.config_dir / "models")
        self.assertIn("ollama create forge-alt -f", entry.command)


class TestSamplingIsConfigurablePerModel(unittest.TestCase):
    """A model ships a sampling recipe its authors chose, and the loop's own
    per-role temperature overrides only that one knob. The rest are settable
    per model block so a model can be run the way it was meant to be."""

    def _payload(self, config, **call):
        sent = {}
        provider = OpenAICompatProvider(
            "m", {"baseUrl": "http://x/v1", "model": "m", "contextWindow": 8192, **config}
        )
        mod = sys.modules["forge.providers.openai_compat"]

        def capture(_url, body, **_kw):
            sent.update(body)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        with unittest.mock.patch.object(mod, "post_json", capture):
            provider.complete([Message(role="user", content="hi")], max_tokens=16, **call)
        return sent

    def test_nothing_configured_sends_no_sampling_knobs(self):
        """An unset knob must stay off the wire — sending top_p 1.0 because
        nobody chose one would overrule the model's own shipped 0.8."""
        sent = self._payload({})

        for key in ("top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty"):
            self.assertNotIn(key, sent)

    def test_each_knob_reaches_the_payload_in_its_wire_spelling(self):
        sent = self._payload({
            "topP": 0.8, "topK": 20, "minP": 0.05,
            "presencePenalty": 1.5, "frequencyPenalty": 0.5,
        })

        self.assertEqual(sent["top_p"], 0.8)
        self.assertEqual(sent["top_k"], 20)
        self.assertEqual(sent["min_p"], 0.05)
        self.assertEqual(sent["presence_penalty"], 1.5)
        self.assertEqual(sent["frequency_penalty"], 0.5)

    def test_zero_is_a_value_not_an_absence(self):
        """`min_p: 0` is a real setting and must not be dropped as falsey."""
        sent = self._payload({"minP": 0, "presencePenalty": 0})

        self.assertEqual(sent["min_p"], 0.0)
        self.assertEqual(sent["presence_penalty"], 0.0)

    def test_top_k_is_sent_as_an_integer(self):
        self.assertIsInstance(self._payload({"topK": 20})["top_k"], int)

    def test_configured_temperature_overrides_the_roles_request(self):
        sent = self._payload({"temperature": 0.6}, temperature=0.0)
        self.assertEqual(sent["temperature"], 0.6)

    def test_without_config_the_roles_temperature_is_used(self):
        sent = self._payload({}, temperature=0.1)
        self.assertEqual(sent["temperature"], 0.1)

    def test_extra_body_still_wins_over_a_named_knob(self):
        """The escape hatch stays an escape hatch."""
        sent = self._payload({"topP": 0.8, "extraBody": {"top_p": 0.5}})
        self.assertEqual(sent["top_p"], 0.5)


class TestOllamaModelNamesCarryAnImplicitTag(unittest.TestCase):
    """`/api/ps` reports `forge-exec:latest`; config writes `forge-exec`,
    because that is how every `ollama run` example spells it. Comparing them
    exactly matched nothing, so the served window was always discarded and the
    budget gate planned against the architectural maximum instead — 262,144
    for a server holding 32,768, with the overflow truncated off the front of
    the prompt where the system message and the spec live."""

    LOADED = {
        "models": [
            {"name": "forge-exec:latest", "model": "forge-exec:latest",
             "context_length": 32768, "size": 8, "size_vram": 8}
        ]
    }
    TRAINED = {"model_info": {"qwen35moe.context_length": 262144}}

    def _provider(self, model: str):
        provider = OpenAICompatProvider(
            "local", {"baseUrl": "http://x:11434/v1", "model": model}
        )
        mod = sys.modules["forge.providers.openai_compat"]
        self.enterContext(
            unittest.mock.patch.object(mod, "get_json", lambda *a, **k: self.LOADED)
        )
        self.enterContext(
            unittest.mock.patch.object(mod, "post_json", lambda *a, **k: self.TRAINED)
        )
        return provider

    def test_an_untagged_config_name_matches_the_tagged_report(self):
        provider = self._provider("forge-exec")

        self.assertTrue(provider._ollama_loaded())
        self.assertEqual(provider._discover_context_window(), 32768)

    def test_a_tagged_config_name_still_matches(self):
        provider = self._provider("forge-exec:latest")

        self.assertEqual(provider._discover_context_window(), 32768)

    def test_a_different_model_does_not_match(self):
        provider = self._provider("forge-alt")

        self.assertEqual(provider._ollama_loaded(), {})
        self.assertEqual(provider._discover_context_window(), 262144)

    def test_a_non_latest_tag_is_not_confused_with_latest(self):
        """`forge-exec:v2` and `forge-exec:latest` are different models."""
        provider = self._provider("forge-exec:v2")

        self.assertEqual(provider._ollama_loaded(), {})

    def test_the_diagnostic_about_a_smaller_window_can_now_fire(self):
        """It is gated on knowing the served size, so the name bug silenced the
        one warning that would have reported the name bug."""
        provider = self._provider("forge-exec")
        provider._caps = None
        notes = provider.diagnostics()

        self.assertTrue(any("32,768" in note for note in notes), notes)

    def test_tag_normalisation_leaves_a_digest_reference_alone(self):
        from forge.providers.openai_compat import _tagged

        self.assertEqual(_tagged("forge-exec"), "forge-exec:latest")
        self.assertEqual(_tagged("forge-exec:latest"), "forge-exec:latest")
        self.assertEqual(_tagged("forge-exec:v2"), "forge-exec:v2")
        self.assertEqual(_tagged("m@sha256:abc"), "m@sha256:abc")


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


class TestRecorderOutputBudget(unittest.TestCase):
    """The recorder's answer is tiny, but the budget is the configured one.

    A cap is not an allocation — a model replying `NOTHING` spends five tokens
    whatever it is allowed — while a thinking model handed a small cap spends
    all of it before writing anything, then reports an output budget the
    operator never set and cannot find. Observed as "forge-plan spent its
    entire 1,024-token output budget on hidden reasoning" on a model configured
    for 65,536.
    """

    def test_the_recorder_gets_the_configured_budget(self):
        orch, _root, run_id = _stub_orchestrator()
        # Distinct from the old hard-coded ceiling, or the assertion passes for
        # the wrong reason — the stub's own budget is 1,024.
        orch.config.models["m"]["maxOutputTokens"] = 65536
        orch.memory = SimpleNamespace(
            settings=SimpleNamespace(write=True),
            remember=lambda *a, **k: None,
        )
        asked: list[int] = []

        def call(_run_id, _role, _messages, *, max_tokens, **_kwargs):
            asked.append(max_tokens)
            return Completion(text="NOTHING", usage=Usage())

        orch._call = call
        orch._record_outcome(
            run_id,
            Ticket("T-1"),
            diff="d",
            review="ACCEPT",
            corrections="",
            retrieved="",
        )

        self.assertEqual(asked, [orch._output_budget(orch.config.record_role)])
        self.assertNotEqual(asked, [1024])


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
