"""Tests for the setup wizard and the machine profile.

The wizard's whole job is to be the place mistakes are caught cheaply, so the
cases worth pinning are the ugly ones: no terminal, a malformed URL, a probe
that raises something nobody predicted, and a credential that must not be
copied into a second file.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forge import toolchain, wizard
from forge.profile import Profile, profile_path, strip_secrets


def scripted(answers: list[str]):
    """A reader that replays `answers`, then fails loudly rather than hanging."""
    remaining = list(answers)

    def reader(_prompt: str) -> str:
        if not remaining:
            raise AssertionError("wizard asked more questions than the script answers")
        return remaining.pop(0)

    return reader


def temp_repo(marker: str = "", body: str = "") -> Path:
    root = Path(tempfile.mkdtemp()) / "repo"
    root.mkdir(parents=True)
    if marker:
        (root / marker).write_text(body or "x", encoding="utf-8")
    return root


class TestProfileSecrets(unittest.TestCase):
    def test_literal_credentials_are_stripped_at_every_depth(self):
        blob = {
            "models": {
                "claude": {
                    "kind": "anthropic",
                    "apiKey": "sk-ant-LEAKED",
                    "apiKeyEnv": "ANTHROPIC_API_KEY",
                    "nested": [{"token": "abc123"}],
                }
            }
        }
        cleaned = strip_secrets(blob)
        flat = json.dumps(cleaned)
        self.assertNotIn("sk-ant-LEAKED", flat)
        self.assertNotIn("abc123", flat)
        # The supported path survives — stripping the env var name would break
        # the config rather than protect anything.
        self.assertIn("ANTHROPIC_API_KEY", flat)

    def test_saving_a_profile_never_writes_a_key(self):
        path = Path(tempfile.mkdtemp()) / "profile.json"
        Profile(
            models={"claude": {"kind": "anthropic", "apiKey": "sk-ant-LEAKED"}},
            roles={"planner": "claude"},
        ).save(path)
        self.assertNotIn("sk-ant-LEAKED", path.read_text(encoding="utf-8"))

    def test_corrupt_profile_reads_as_empty_rather_than_raising(self):
        # It holds preferences, not state. Re-asking four questions beats
        # refusing to initialize the repo.
        path = Path(tempfile.mkdtemp()) / "profile.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertTrue(Profile.load(path).is_empty)

    def test_profile_path_follows_the_env_override(self):
        with mock.patch.dict("os.environ", {"FORGE_PROFILE": "/tmp/custom.json"}):
            self.assertEqual(profile_path(), Path("/tmp/custom.json"))

    def test_round_trip_preserves_models_roles_memory_and_port(self):
        path = Path(tempfile.mkdtemp()) / "profile.json"
        Profile(
            models={"local": {"kind": "openai", "baseUrl": "http://h:11434/v1"}},
            roles={"executor": "local"},
            memory={"url": "http://h:8787/mcp"},
            ui_port=9100,
        ).save(path)

        loaded = Profile.load(path)
        self.assertEqual(loaded.models["local"]["baseUrl"], "http://h:11434/v1")
        self.assertEqual(loaded.roles["executor"], "local")
        self.assertEqual(loaded.memory["url"], "http://h:8787/mcp")
        self.assertEqual(loaded.ui_port, 9100)


class TestUrlChecking(unittest.TestCase):
    def test_missing_scheme_is_named_with_the_fix(self):
        complaint = wizard.check_url("forge-host:8787/mcp")
        self.assertIn("scheme", complaint)
        self.assertIn("http://forge-host:8787/mcp", complaint)

    def test_wrong_scheme_is_rejected(self):
        self.assertIn("not supported", wizard.check_url("ftp://host/x"))

    def test_good_urls_pass(self):
        for url in ("http://localhost:11434/v1", "https://api.example.com/v1"):
            self.assertEqual(wizard.check_url(url), "", url)


class TestProbesNeverRaise(unittest.TestCase):
    """A probe that throws takes every answer typed so far with it."""

    def test_malformed_url_is_a_failed_probe_not_a_crash(self):
        ok, detail = wizard.probe_model("local", {"kind": "openai", "baseUrl": "n", "model": "m"})
        self.assertFalse(ok)
        self.assertIn("scheme", detail)

    def test_unexpected_exception_from_health_is_caught(self):
        with mock.patch("forge.wizard.build_provider") as build:
            build.return_value.health.side_effect = RuntimeError("kaboom")
            ok, detail = wizard.probe_model(
                "local", {"kind": "openai", "baseUrl": "http://h/v1", "model": "m"}
            )
        self.assertFalse(ok)
        self.assertIn("kaboom", detail)

    def test_memory_probe_survives_an_unexpected_exception(self):
        with mock.patch("forge.wizard.MemoryClient") as client:
            client.from_config.side_effect = RuntimeError("kaboom")
            ok, detail = wizard.probe_memory("http://h/mcp", room="")
        self.assertFalse(ok)
        self.assertIn("kaboom", detail)

    def test_one_answer_routes_to_the_right_transport(self):
        # MemPalace is stdio-only, so a bare command is the common answer and
        # must not be mistaken for a malformed URL.
        self.assertEqual(
            wizard.memory_block("mempalace-mcp"), {"command": ["mempalace-mcp"], "room": ""}
        )
        self.assertEqual(
            wizard.memory_block("http://h:8787/mcp"), {"url": "http://h:8787/mcp", "room": ""}
        )

    def test_a_command_is_not_rejected_by_url_validation(self):
        with mock.patch("forge.wizard.MemoryClient") as client:
            client.from_config.return_value.describe.return_value = "ok memory transport=stdio"
            ok, _ = wizard.probe_memory("mempalace-mcp", room="")
        self.assertTrue(ok)


class TestEvidenceGathering(unittest.TestCase):
    def test_a_repo_with_nothing_to_read_yields_no_evidence(self):
        self.assertEqual(toolchain.gather_evidence(temp_repo()), [])

    def test_ci_workflows_and_build_files_are_collected(self):
        root = temp_repo()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text("run: cargo test --workspace")
        (root / "Cargo.toml").write_text("[package]\nname='x'")
        names = [name for name, _ in toolchain.gather_evidence(root)]
        self.assertIn(".github/workflows/ci.yml", names)
        self.assertIn("Cargo.toml", names)

    def test_total_evidence_is_bounded(self):
        root = temp_repo()
        for name in ("Makefile", "README.md", "CONTRIBUTING.md", "package.json"):
            (root / name).write_text("test: " + "x" * 50_000)
        total = sum(len(text) for _, text in toolchain.gather_evidence(root))
        self.assertLessEqual(total, toolchain.MAX_TOTAL_CHARS)

    def test_truncation_keeps_command_lines_from_the_end_of_a_long_doc(self):
        # A README states how to run the tests near the bottom at least as
        # often as the top; a plain head cut throws the answer away.
        body = "filler paragraph.\n" * 2000 + "\n```\npython -m unittest discover tests\n```\n"
        kept = toolchain.excerpt(body, 3000)
        self.assertLessEqual(len(kept), 3000)
        self.assertIn("python -m unittest discover tests", kept)


class TestDetectionParsing(unittest.TestCase):
    def test_a_fenced_json_reply_is_read(self):
        detection = toolchain.parse_detection(
            '```json\n{"lint": "cargo clippy", "typecheck": "", '
            '"test": "cargo nextest run", "confidence": "high"}\n```'
        )
        self.assertTrue(detection.ok)
        self.assertEqual(detection.commands["test"], "cargo nextest run")
        self.assertEqual(detection.commands["typecheck"], "")
        self.assertEqual(detection.confidence, "high")

    def test_a_non_json_reply_is_an_error_not_a_command(self):
        detection = toolchain.parse_detection("I could not determine the commands.")
        self.assertFalse(detection.ok)
        self.assertFalse(detection.found_anything)

    def test_unusable_shapes_are_rejected_rather_than_run(self):
        # Each of these looks like an answer and would fail every ticket.
        for value in (
            "cargo test ${{ matrix.flags }}",   # unexpanded CI interpolation
            "cd crates/core",                   # not a verify command
            "pytest <your test dir>",           # invented placeholder
            "# no test command",                # a comment
            "make lint\nmake test",             # a procedure, not a command
            "none",
        ):
            self.assertEqual(toolchain.clean_command(value), "", value)

    def test_ordinary_commands_survive_cleaning(self):
        for value in ("cargo test --workspace", "`npm test`", "  go test ./...  "):
            self.assertTrue(toolchain.clean_command(value))

    def test_confidence_defaults_to_low_when_unstated(self):
        detection = toolchain.parse_detection('{"test": "go test ./..."}')
        self.assertEqual(detection.confidence, "low")

    def test_detection_never_raises_when_the_provider_fails(self):
        root = temp_repo("Makefile", "test:\n\tgo test ./...")
        provider = mock.Mock()
        provider.complete.side_effect = RuntimeError("kaboom")
        detection = toolchain.detect(root, provider)
        self.assertFalse(detection.ok)
        self.assertIn("kaboom", detection.error)

    def test_a_repo_with_no_evidence_never_calls_the_model(self):
        provider = mock.Mock()
        detection = toolchain.detect(temp_repo(), provider)
        self.assertFalse(detection.ok)
        provider.complete.assert_not_called()


class TestWizardFlow(unittest.TestCase):
    def setUp(self):
        # The wizard is chatty by design; that belongs on a terminal, not in
        # the test log.
        patcher = mock.patch.object(wizard, "say")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(
        self,
        root: Path,
        answers: list[str],
        profile: Profile | None = None,
        detection: toolchain.Detection | None = None,
    ):
        # Probes and detection are stubbed: this exercises the flow, not the
        # network and not a model.
        found = detection or toolchain.Detection(
            commands={"lint": "", "typecheck": "", "test": "detected-test"},
            confidence="high",
            evidence=[".github/workflows/ci.yml"],
        )
        with mock.patch.object(wizard, "probe_model", return_value=(True, "answered")), \
             mock.patch.object(wizard, "probe_memory", return_value=(True, "connected")), \
             mock.patch.object(wizard, "detect_commands", return_value=found):
            return wizard.run(
                root,
                profile or Profile(path=Path(tempfile.mkdtemp()) / "profile.json"),
                wizard.Prompter(enabled=True, reader=scripted(answers)),
            )

    def test_full_pass_produces_a_valid_config_and_a_reusable_profile(self):
        root = temp_repo("pyproject.toml")
        config, profile = self._run(root, [
            "http://gpu:11434/v1", "qwen3.6:35b-a3b", "",   # executor
            "1", "opus",                                     # claude-cli
            "http://gpu:8787/mcp", "n",                      # memory, no write-back
            "myroom",                                        # room
            "y", "", "", "",                                 # detect, accept commands
            "src/auth/**, migrations/**",                    # neverDelegate
            "y",                                             # write
        ])

        config.validate()  # raises if any role points at an undeclared model
        self.assertEqual(config.roles["executor"], "local")
        self.assertEqual(config.roles["reviewer"], "claude")
        self.assertEqual(config.room, "myroom")
        self.assertEqual(config.commands["test"], "detected-test")
        self.assertEqual(config.never_delegate, ["src/auth/**", "migrations/**"])
        self.assertEqual(config.memory["url"], "http://gpu:8787/mcp")
        # Write-back declined stays off — it mutates a store with no undo.
        self.assertNotIn("write", config.memory)

        # The profile keeps endpoints, and nothing the next repo would decide.
        self.assertIn("local", profile.models)
        self.assertIn("claude", profile.models)
        self.assertEqual(profile.memory["url"], "http://gpu:8787/mcp")

    def test_declining_the_final_confirmation_writes_nothing(self):
        root = temp_repo("Cargo.toml")
        result = self._run(root, [
            "http://gpu:11434/v1", "m", "", "1", "opus", "",
            "r", "y", "", "", "", "", "n",
        ])
        self.assertIsNone(result)
        self.assertFalse((root / ".hybridforge").exists())

    def test_declining_detection_leaves_the_commands_blank_rather_than_guessed(self):
        # The point of dropping the marker heuristic: a repo whose commands are
        # written down nowhere gets an empty field, not a plausible default.
        root = temp_repo("Cargo.toml")
        detect = mock.Mock()
        with mock.patch.object(wizard, "probe_model", return_value=(True, "ok")), \
             mock.patch.object(wizard, "probe_memory", return_value=(True, "ok")), \
             mock.patch.object(wizard, "detect_commands", detect):
            config, _ = wizard.run(
                root,
                Profile(path=Path(tempfile.mkdtemp()) / "profile.json"),
                wizard.Prompter(enabled=True, reader=scripted([
                    "http://gpu:11434/v1", "m", "", "1", "opus", "",
                    "r", "n", "", "", "", "", "y",
                ])),
            )
        detect.assert_not_called()
        self.assertEqual(config.commands, {"lint": "", "typecheck": "", "test": ""})

    def test_a_failed_detection_leaves_the_commands_blank(self):
        config, _ = self._run(
            temp_repo("Cargo.toml"),
            ["http://gpu:11434/v1", "m", "", "1", "opus", "",
             "r", "y", "", "", "", "", "y"],
            detection=toolchain.Detection(error="planner unreachable"),
        )
        self.assertEqual(config.commands, {"lint": "", "typecheck": "", "test": ""})

    def test_saved_profile_supplies_every_default_on_the_next_repo(self):
        profile = Profile(
            models={
                "local": {"kind": "openai", "baseUrl": "http://gpu:11434/v1", "model": "qwen"},
                "claude": {"kind": "claude-cli", "model": "opus"},
            },
            roles={"planner": "claude", "executor": "local",
                   "tester": "local", "reviewer": "claude"},
            memory={"url": "http://gpu:8787/mcp"},
            path=Path(tempfile.mkdtemp()) / "profile.json",
        )
        # Every answer blank: the second repo is Enter-through except its own
        # room and commands, which is the whole point of saving the profile.
        config, _ = self._run(
            temp_repo("go.mod"),
            ["", "", "", "", "", "", "n", "", "", "", "", "", "", "y"],
            profile=profile,
        )
        self.assertEqual(config.models["local"]["baseUrl"], "http://gpu:11434/v1")
        self.assertEqual(config.models["claude"]["kind"], "claude-cli")
        self.assertEqual(config.memory["url"], "http://gpu:8787/mcp")
        self.assertEqual(config.commands["test"], "detected-test")

    def test_write_back_when_accepted_starts_in_dry_run(self):
        config, _ = self._run(temp_repo("pyproject.toml"), [
            "http://gpu:11434/v1", "m", "", "1", "opus",
            "http://gpu:8787/mcp", "y",          # memory, write-back yes
            "r", "y", "", "", "", "", "y",
        ])
        self.assertTrue(config.memory["write"])
        self.assertTrue(config.memory["dryRun"])

    def test_choosing_the_executor_as_reviewer_declares_no_second_model(self):
        config, _ = self._run(temp_repo(), [
            "http://gpu:11434/v1", "m", "",
            "4",                                  # same model as executor
            "", "r", "y", "", "", "", "", "y",
        ])
        config.validate()
        self.assertEqual(config.roles["reviewer"], "local")
        self.assertEqual(set(config.models), {"local"})

    def test_no_terminal_takes_defaults_and_never_reads_stdin(self):
        def explode(_prompt: str) -> str:
            raise AssertionError("a non-interactive wizard must not read stdin")

        root = temp_repo("Cargo.toml")
        found = toolchain.Detection(
            commands={"lint": "", "typecheck": "", "test": "cargo test --workspace"},
            evidence=["Cargo.toml"],
        )
        with mock.patch.object(wizard, "probe_model", return_value=(True, "answered")), \
             mock.patch.object(wizard, "probe_memory", return_value=(True, "ok")), \
             mock.patch.object(wizard, "detect_commands", return_value=found):
            config, _ = wizard.run(
                root,
                Profile(path=Path(tempfile.mkdtemp()) / "profile.json"),
                wizard.Prompter(enabled=False, reader=explode),
            )
        config.validate()
        self.assertEqual(config.room, root.name)
        # Detection still runs without a terminal — it needs no input, and its
        # answer is better than the blank the user is not there to fill in.
        self.assertEqual(config.commands["test"], "cargo test --workspace")

    def test_ctrl_c_aborts_without_returning_a_config(self):
        def interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt()

        with self.assertRaises(wizard.Aborted):
            wizard.run(
                temp_repo(),
                Profile(),
                wizard.Prompter(enabled=True, reader=interrupt),
            )

    def test_a_failing_probe_gives_up_after_a_bounded_number_of_retries(self):
        # Unbounded retry would trap someone whose endpoint is simply down.
        root = temp_repo()
        attempts = []

        def probe(_name, block):
            attempts.append(block)
            return False, "unreachable"

        answers = ["http://gpu:11434/v1", "m", ""] + ["y", "http://gpu:11434/v1", "m", ""] * 10
        with mock.patch.object(wizard, "probe_model", side_effect=probe), \
             mock.patch.object(wizard, "probe_memory", return_value=(False, "unreachable")):
            prompter = wizard.Prompter(enabled=True, reader=scripted(answers))
            answers_obj = wizard.Answers()
            wizard._ask_executor(answers_obj, Profile(), prompter)

        self.assertEqual(len(attempts), wizard.MAX_RETRIES)
        # It still records the answer rather than losing it.
        self.assertEqual(answers_obj.models["local"]["baseUrl"], "http://gpu:11434/v1")


if __name__ == "__main__":
    unittest.main()
