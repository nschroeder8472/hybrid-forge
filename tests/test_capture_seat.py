"""What phase 1's live run found about the seat that looks at a capture.

docs/CREATIVE-TOOLS.md, phase 1: three Blender tickets landed, and the
`viewModel` that was meant to judge their captures saw none of them. Three
causes, each fixed here and held by a test:

1. **A view model that cannot see was accepted.** `roleNeeds` refuses a role
   whose model lacks a capability, but `loop.viewModel` is not a role, so a
   claude-cli block with no tools passed validation and was sent filenames.
2. **claude-cli loaded the user's MCP servers.** `--tools ""` turns off the
   CLI's built-in tools only; the reviewer tried to open its captures through
   a claude.ai connector and one review spent 845 s waiting on it.
3. **The capture command had to guess what changed.** With `autoCommit` off,
   git reports every earlier ticket's work as changed, so each review rendered
   the whole backlog. The loop now names the files the attempt wrote.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.config import ROLES, Config, ConfigError  # noqa: E402
from forge.loop import Orchestrator  # noqa: E402
from forge.providers.base import Message  # noqa: E402
from forge.providers.claude_cli import ClaudeCLIProvider  # noqa: E402
from forge.state import Store, Ticket  # noqa: E402

LOCAL = {
    "kind": "openai",
    "baseUrl": "http://127.0.0.1:1/v1",
    "model": "stub",
    "contextWindow": 8192,
    "maxOutputTokens": 1024,
}


def config_with_view(view_block: dict) -> Config:
    config = Config(
        root=Path(tempfile.mkdtemp()).resolve(),
        models={"m": LOCAL, "eye": view_block},
        roles={role: "m" for role in ROLES},
    )
    config.loop.view_model = "eye"
    return config


class ViewModelMustSeeTests(unittest.TestCase):
    def test_a_claude_cli_view_model_with_no_tools_is_refused(self) -> None:
        config = config_with_view({"kind": "claude-cli", "model": "opus"})
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        self.assertIn("cannot be shown an image", str(caught.exception))
        self.assertIn('"tools": "Read"', str(caught.exception))

    def test_a_claude_cli_view_model_that_can_read_is_accepted(self) -> None:
        config_with_view({"kind": "claude-cli", "model": "opus", "tools": "Read"}).validate()

    def test_no_view_model_is_not_checked(self) -> None:
        config = config_with_view({"kind": "claude-cli", "model": "opus"})
        config.loop.view_model = ""
        config.validate()


class ClaudeCliLoadsNoMcpTests(unittest.TestCase):
    def argv(self, block: dict) -> list[str]:
        seen: list[list[str]] = []

        class Captured(Exception):
            pass

        def run(argv, **_kwargs):
            seen.append(list(argv))
            raise Captured()

        provider = ClaudeCLIProvider("c", {"kind": "claude-cli", **block})
        with unittest.mock.patch("shutil.which", return_value="claude"), \
                unittest.mock.patch("subprocess.run", side_effect=run):
            try:
                provider.complete([Message("user", "hi")], max_tokens=16)
            except Exception:  # noqa: BLE001 - the call is cut off on purpose
                pass
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_a_role_gets_no_mcp_servers(self) -> None:
        argv = self.argv({"tools": "Read"})
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)

    def test_allow_all_tools_keeps_the_users_servers(self) -> None:
        self.assertNotIn("--strict-mcp-config", self.argv({"allowAllTools": True}))


class CaptureIsToldWhatWasWrittenTests(unittest.TestCase):
    # Writes the variable's value next to where pictures go, so the test can
    # read what the command was told.
    RECORDER = (
        'python -c "'
        "import os,pathlib;"
        "d=pathlib.Path(os.environ['FORGE_CAPTURE_DIR']);"
        "(d/'files.txt').write_text(os.environ.get('FORGE_CAPTURE_FILES','<unset>'))"
        '"'
    )

    def test_the_command_gets_the_attempts_files_relative_to_its_workspace(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "art" / "scenes").mkdir(parents=True)
        (root / "art" / "scenes" / "anvil.py").write_text("x = 1\n", encoding="utf-8")
        config = Config(
            root=root,
            models={"m": LOCAL},
            roles={role: "m" for role in ROLES},
        )
        from forge.config import Workspace

        config.declare_workspaces([
            Workspace(root="art", commands={"lint": "", "test": "", "capture": self.RECORDER}),
        ])
        store = Store(config.db_path)
        self.addCleanup(store.close_all)
        orchestrator = Orchestrator(config, store)
        ticket = Ticket(
            "SCN-001", title="t", spec="s", criteria=["c"],
            allowed_files=["art/scenes/anvil.py", "art/tests/anvil_test.py"],
        )
        written = ["art/scenes/anvil.py", "art/tests/anvil_test.py", "README.md"]

        orchestrator._capture(store.create_run("goal"), ticket, 1, written)

        recorded = next(config.config_dir.rglob("files.txt")).read_text(encoding="utf-8")
        self.assertEqual(
            recorded.split(os.pathsep), ["scenes/anvil.py", "tests/anvil_test.py"]
        )


if __name__ == "__main__":
    unittest.main()
