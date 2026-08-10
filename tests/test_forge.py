"""Tests for the parts where a silent wrong answer is expensive.

Scope enforcement, reset-time parsing, and plan parsing are all places where a
bug does not raise — it just lets the loop do the wrong thing for hours. Those
get tests; the HTTP adapters do not, since exercising them needs a live model.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from forge.artifacts import Artifacts
from forge.budget import BudgetGate, RateLimitPolicy
from forge.config import Config, UISettings
from forge.ingest import looks_like_plan, parse_plan, tickets_from_json
from forge.loop import Orchestrator
from forge.patch import enforce_scope, is_safe_path, matches_any, parse_output
from forge.prompts import parse_verdict, tests_prompt
from forge.providers.base import Completion, Usage
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
            test_command="python -m unittest discover tests",
            example_test=("tests/test_thing.py", "import unittest\n"),
        )
        body = messages[-1].content
        self.assertIn("python -m unittest discover tests", body)
        self.assertIn("tests/test_thing.py", body)
        self.assertIn("import unittest", body)

    def test_prompt_without_an_example_still_asks_for_repo_conventions(self):
        body = tests_prompt(Ticket("T-1"), ["app.py"])[-1].content
        self.assertIn("conventions already used in this repository", body)

    def test_failure_context_reaches_the_tester(self):
        body = tests_prompt(
            Ticket("T-1", criteria=["x is 1"]),
            ["app.py"],
            failure_context="AssertionError: '\"HI!\"' not found in source",
        )[-1].content
        self.assertIn("not found in source", body)

    def test_failure_context_forbids_weakening_a_real_failure(self):
        # The dangerous reading of "your tests failed" is "make them pass".
        # A tester that deletes an assertion turns a caught defect into a green
        # suite over broken code.
        body = tests_prompt(Ticket("T-1"), ["app.py"], failure_context="boom")[-1].content
        self.assertIn("not yours to correct", body)
        self.assertIn("keep the assertion as written", body)

    def test_a_clean_first_attempt_carries_no_failure_section(self):
        body = tests_prompt(Ticket("T-1"), ["app.py"])[-1].content
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


if __name__ == "__main__":
    unittest.main()
