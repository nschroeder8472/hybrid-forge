"""The host tool surface: `forge.api`, the MCP server over it, and `forge tool`.

See docs/FORGE-AS-A-TOOL.md. The point of these tests is that the standards a
run enforces reach a host through the tools unchanged — attribution against a
measured baseline, an environment failure named as one — and that the
transport answers like an MCP server a client can rely on.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from forge.api import ApiError, Forge, HOST_RUN_KEY
from forge.config import ROLES
from forge.mcp.server import Server
from forge.mcp.tools import TOOLS
from forge.state import RUN_HOST, Store, Ticket

# A lint command whose output is whatever `errors.txt` holds, in flake8's
# shape, and which fails when there is any. Lets a test choose exactly which
# diagnostics exist before and after an "edit".
_LINT = (
    "import pathlib, sys\n"
    "text = pathlib.Path('errors.txt').read_text() if pathlib.Path('errors.txt').exists() else ''\n"
    "print(text, end='')\n"
    "sys.exit(1 if text.strip() else 0)\n"
)


def _project(commands: dict[str, str] | None = None) -> Path:
    root = Path(tempfile.mkdtemp()).resolve()
    (root / ".hybridforge").mkdir()
    (root / "lint.py").write_text(_LINT, encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    if commands is None:
        commands = {"lint": f'"{sys.executable}" lint.py'}
    (root / ".hybridforge" / "config.json").write_text(
        json.dumps(
            {
                "models": {"m": {"kind": "openai", "model": "x"}},
                "roles": {r: "m" for r in ROLES},
                "commands": commands,
                "ui": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return root


def _errors(root: Path, *lines: str) -> None:
    (root / "errors.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


class TestVerifyAttributesAgainstACapturedBaseline(unittest.TestCase):
    """A host editing files itself is held to the same attribution a ticket is:
    what was failing before it started is `already`, everything else is
    `introduced`, and only the second is its to fix."""

    def test_a_failure_present_at_capture_is_already_and_a_new_one_is_introduced(self):
        root = _project()
        _errors(root, "a.py:1:1: F821 undefined name old")
        forge = Forge(root)
        try:
            captured = forge.verify(capture="before")
            self.assertFalse(captured["ok"])

            _errors(
                root,
                "a.py:1:1: F821 undefined name old",
                "a.py:2:1: F821 undefined name new",
            )
            result = forge.verify(against="before")
        finally:
            forge.close()

        (lint,) = result["steps"]
        self.assertEqual(lint["already"], ["a.py:1:1: f821 undefined name old"])
        self.assertEqual(lint["introduced"], ["a.py:2:1: f821 undefined name new"])
        self.assertFalse(result["clean"])

    def test_only_pre_existing_failures_is_clean_but_not_ok(self):
        root = _project()
        _errors(root, "a.py:1:1: F821 undefined name old")
        forge = Forge(root)
        try:
            forge.verify(capture="before")
            result = forge.verify(against="before")
        finally:
            forge.close()

        self.assertFalse(result["ok"])
        self.assertTrue(result["clean"])
        self.assertEqual(result["steps"][0]["introduced"], [])

    def test_without_a_baseline_every_failure_is_introduced(self):
        # The safe direction to be wrong in: nothing is excused on a guess.
        root = _project()
        _errors(root, "a.py:1:1: F821 undefined name old")
        forge = Forge(root)
        try:
            result = forge.verify()
        finally:
            forge.close()

        self.assertEqual(
            result["steps"][0]["introduced"], ["a.py:1:1: f821 undefined name old"]
        )
        self.assertEqual(result["steps"][0]["blamed_files"], ["a.py"])

    def test_an_unknown_baseline_is_refused_by_name(self):
        forge = Forge(_project())
        try:
            with self.assertRaisesRegex(ApiError, "no baseline 'nope'"):
                forge.verify(against="nope")
        finally:
            forge.close()

    def test_a_command_that_never_started_is_named_as_the_environment(self):
        # LOOP-INVARIANTS §13: a launcher's own words are not a fact about code.
        root = _project({"lint": "forge-no-such-command-anywhere"})
        forge = Forge(root)
        try:
            result = forge.verify()
        finally:
            forge.close()

        (lint,) = result["steps"]
        self.assertFalse(lint["ok"])
        self.assertIn("environment", lint)
        self.assertFalse(result["clean"])


class TestTheHostRunIsNotAutopilotWork(unittest.TestCase):
    """Host steps need a run to belong to, and that run must never be picked up
    by `forge go`: the host decides when its tickets are worked."""

    def test_the_host_run_is_reused_and_never_resumable(self):
        root = _project()
        forge = Forge(root)
        try:
            first = forge._host_run()
            second = forge._host_run()
            forge.store.add_tickets(first, [Ticket("H-1")])
            self.assertEqual(first, second)
            self.assertEqual(forge.store.get_run(first)["status"], RUN_HOST)
            self.assertEqual(forge.store.get_control(HOST_RUN_KEY), str(first))
            self.assertEqual(forge.store.resumable_runs(), [])
            self.assertIsNone(forge.store.resumable_run())
            self.assertIsNone(forge.store.active_run())
        finally:
            forge.close()


class TestInspectReadsTheLedger(unittest.TestCase):
    def _seeded(self) -> tuple[Path, int, int]:
        root = _project()
        store = Store(root / ".hybridforge" / "run.db")
        try:
            run_id = store.create_run("goal")
            store.add_tickets(
                run_id, [Ticket("T-1", spec="do it", allowed_files=["a.py"], criteria=["c"])]
            )
            step_id = store.start_step(run_id, "T-1", "build")
            store.end_step(step_id, "ok", "x" * 50)
        finally:
            store.close()
        return root, run_id, step_id

    def test_a_ticket_lists_its_steps_without_their_output(self):
        root, run_id, step_id = self._seeded()
        forge = Forge(root)
        try:
            ticket = forge.inspect(ticket="T-1")
        finally:
            forge.close()
        self.assertEqual(ticket["spec"], "do it")
        self.assertEqual(ticket["steps"], [{"id": step_id, "name": "build", "status": "ok"}])

    def test_a_step_is_clipped_to_the_limit_and_says_so(self):
        root, run_id, step_id = self._seeded()
        forge = Forge(root)
        try:
            step = forge.inspect(step=step_id, limit=10)
        finally:
            forge.close()
        self.assertTrue(step["clipped"])
        self.assertTrue(step["detail"].startswith("x" * 10))

    def test_an_unknown_ticket_is_refused(self):
        root, _, _ = self._seeded()
        forge = Forge(root)
        try:
            with self.assertRaisesRegex(ApiError, "no ticket 'T-9'"):
                forge.inspect(ticket="T-9")
        finally:
            forge.close()


class TestMcpServer(unittest.TestCase):
    def _exchange(self, root: Path, *messages: dict) -> list[dict]:
        reader = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        writer = io.StringIO()
        Server(root).serve(reader, writer)
        return [json.loads(line) for line in writer.getvalue().splitlines()]

    def test_initialize_list_and_call(self):
        root = _project()
        replies = self._exchange(
            root,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "forge_verify", "arguments": {}}},
        )
        # The notification is not answered.
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, [t.name for t in TOOLS])
        call = replies[2]["result"]
        self.assertFalse(call["isError"])
        self.assertTrue(call["structuredContent"]["ok"])
        self.assertEqual(json.loads(call["content"][0]["text"]), call["structuredContent"])

    def test_a_refusal_is_a_tool_result_not_a_protocol_error(self):
        # The host is meant to read "no baseline" and act on it.
        (reply,) = self._exchange(
            _project(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "forge_verify", "arguments": {"against": "x"}}},
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("no baseline", reply["result"]["content"][0]["text"])

    def test_unknown_tool_and_method_are_protocol_errors(self):
        replies = self._exchange(
            _project(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        )
        self.assertEqual(replies[0]["error"]["code"], -32602)
        self.assertEqual(replies[1]["error"]["code"], -32601)

    def test_a_line_that_is_not_json_is_answered_and_the_server_carries_on(self):
        reader = io.StringIO('not json\n{"jsonrpc":"2.0","id":5,"method":"ping"}\n')
        writer = io.StringIO()
        Server(_project()).serve(reader, writer)
        first, second = [json.loads(line) for line in writer.getvalue().splitlines()]
        self.assertEqual(first["error"]["code"], -32700)
        self.assertEqual(second, {"jsonrpc": "2.0", "id": 5, "result": {}})

    def test_a_misspelt_argument_is_refused_not_ignored(self):
        (reply,) = self._exchange(
            _project(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "forge_verify", "arguments": {"captur": "x"}}},
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("does not take captur", reply["result"]["content"][0]["text"])

    def test_every_schema_forbids_unknown_arguments(self):
        # A misspelt argument silently ignored is a host that thinks it asked
        # for something it did not get.
        for tool in TOOLS:
            self.assertIs(tool.schema.get("additionalProperties"), False, tool.name)


if __name__ == "__main__":
    unittest.main()
