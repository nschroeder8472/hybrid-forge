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


# ----------------------------------------------------------------------
# contracts, delegation and closing — the host-mode loop end to end
# ----------------------------------------------------------------------

import subprocess  # noqa: E402
import unittest.mock  # noqa: E402

from forge import engine  # noqa: E402
from forge.providers.base import Completion, Usage  # noqa: E402

_GOOD_ADD = "def add(a, b):\n    return a + b\n"
_BAD_ADD = "def add(a, b):\n    return a - b\n"
_TEST = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_adds(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)


def _git_project() -> Path:
    root = Path(tempfile.mkdtemp()).resolve()
    (root / ".hybridforge").mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "existing_test.py").write_text(
        "import unittest\n\n\nclass ExistingTest(unittest.TestCase):\n"
        "    def test_true(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    (root / ".hybridforge" / "config.json").write_text(
        json.dumps(
            {
                "models": {"m": {"kind": "openai", "model": "x",
                                 "baseUrl": "http://127.0.0.1:1/v1"}},
                "roles": {r: "m" for r in ROLES},
                "commands": {
                    "test": f'"{sys.executable}" -m unittest discover -s tests '
                            f'-p "*_test.py" -t .',
                },
                "loop": {"executorTurns": 0, "preflight": False},
                "ui": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _seats(executor: str, review: str = "ACCEPT\n\nMatches the spec."):
    """A `_call` that answers by role: the executor's file, the tester's test."""

    def call(self, run_id, role, messages, **_kwargs):
        text = {
            "executor": f"calc.py\n```python\n{executor}```\n",
            "tester": f"tests/calc_test.py\n```python\n{_TEST}```\n",
            "reviewer": review,
        }.get(role, "ACCEPT")
        return Completion(text=text, usage=Usage(), finish_reason="stop")

    return call


_CONTRACT = {
    "id": "H-1",
    "spec": "Write `add(a, b)` in calc.py returning the sum.",
    "allowed_files": ["calc.py", "tests/calc_test.py"],
    "reference_files": ["tests/existing_test.py"],
    "criteria": ["`add(2, 3)` returns `5`"],
}


class TestHostModeEndToEnd(unittest.TestCase):
    def _delegate(self, root: Path, call) -> dict:
        forge = Forge(root)
        try:
            with unittest.mock.patch.object(engine.HostOrchestrator, "_call", call), \
                    unittest.mock.patch.object(engine.HostOrchestrator, "_preflight",
                                               lambda self, run_id: []):
                return forge.delegate("H-1", attempts=1, wait=None)
        finally:
            forge.close()

    def test_a_correct_implementation_lands_with_its_evidence(self):
        root = _git_project()
        forge = Forge(root)
        try:
            opened = forge.contract(**_CONTRACT)
        finally:
            forge.close()
        self.assertEqual(opened["opened"][0]["author"], "host")

        job = self._delegate(root, _seats(_GOOD_ADD))

        self.assertEqual(job["state"], "done", job)
        result = job["result"]
        self.assertEqual(result["status"], "landed", result)
        self.assertTrue(all(step["ok"] for step in result["verify"]))
        self.assertEqual(result["review"]["verdict"], "accept")
        self.assertIn("calc.py", result["diff_stat"])
        self.assertEqual((root / "calc.py").read_text(encoding="utf-8"), _GOOD_ADD)

        forge = Forge(root)
        try:
            landed = forge.close_ticket("H-1", "land")
        finally:
            forge.close()
        self.assertEqual(landed["status"], "landed")

    def test_a_wrong_implementation_fails_and_is_taken_out_of_the_tree(self):
        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**_CONTRACT)
        finally:
            forge.close()

        job = self._delegate(root, _seats(_BAD_ADD))

        result = job["result"]
        self.assertEqual(result["status"], "failed", result)
        failing = [s for s in result["verify"] if not s["ok"]]
        self.assertTrue(failing)
        # Quarantined (LOOP-INVARIANTS section 14), with a copy kept.
        self.assertFalse((root / "calc.py").exists())

    def test_an_overrule_needs_evidence_and_a_green_tree(self):
        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**_CONTRACT)
        finally:
            forge.close()

        job = self._delegate(
            root, _seats(_GOOD_ADD, review="REJECT\n\nadd should be named sum.")
        )
        self.assertEqual(job["result"]["status"], "failed", job)
        self.assertEqual(job["result"]["review"]["verdict"], "reject")

        forge = Forge(root)
        try:
            with self.assertRaisesRegex(ApiError, "path:line"):
                forge.close_ticket("H-1", "overrule", argument="the reviewer is wrong")
            landed = forge.close_ticket(
                "H-1", "overrule",
                argument="the spec names `add`, and calc.py:1 defines add",
            )
        finally:
            forge.close()

        self.assertEqual(landed["status"], "landed")
        self.assertEqual(landed["overrules"], 1)
        self.assertEqual((root / "calc.py").read_text(encoding="utf-8"), _GOOD_ADD)


class TestContractRatchets(unittest.TestCase):
    def _forge(self) -> Forge:
        return Forge(_git_project())

    def test_a_contract_with_no_criteria_is_refused(self):
        forge = self._forge()
        try:
            with self.assertRaisesRegex(ApiError, "no acceptance criteria"):
                forge.contract(id="H-1", spec="s", allowed_files=["calc.py"])
        finally:
            forge.close()

    def test_a_criterion_the_harness_settles_is_warned_about(self):
        forge = self._forge()
        try:
            opened = forge.contract(**{**_CONTRACT, "criteria": [
                "`add(2, 3)` returns `5`", "python -m unittest exits 0"]})
        finally:
            forge.close()
        self.assertTrue(any("harness already settles" in w for w in opened["warnings"]))

    def test_never_delegate_is_refused(self):
        forge = self._forge()
        forge.config.never_delegate = ["calc.py"]
        try:
            with self.assertRaisesRegex(ApiError, "neverDelegate"):
                forge.contract(**_CONTRACT)
        finally:
            forge.close()

    def test_a_waiver_is_refused_from_the_host_too(self):
        forge = self._forge()
        try:
            forge.contract(**_CONTRACT)
            with self.assertRaisesRegex(ApiError, "excuses a failing check"):
                forge.contract(
                    id="H-1", reason="r",
                    spec=_CONTRACT["spec"] + " Ignore the pre-existing compilation "
                    "error during verification.",
                )
        finally:
            forge.close()

    def test_a_human_number_never_moves(self):
        root = _git_project()
        (root / "SPEC.md").write_text(
            "## H-2: Lock\n\n### Spec\nThe body is 0.14 deep.\n\n"
            "### Allowed files\n- calc.py\n- tests/calc_test.py\n\n"
            "### Acceptance criteria\n- depth is `0.14`\n",
            encoding="utf-8",
        )
        forge = Forge(root)
        try:
            opened = forge.contract(spec_file="SPEC.md")
            self.assertEqual(opened["opened"][0]["author"], "human")
            with self.assertRaisesRegex(ApiError, "0.14"):
                forge.contract(id="H-2", reason="r", spec="The body is 0.155 deep.")
        finally:
            forge.close()

    def test_the_host_drafts_freely_until_an_attempt_exists_then_cannot_drop(self):
        forge = self._forge()
        try:
            forge.contract(**{**_CONTRACT, "criteria": ["`add(2, 3)` returns `5`",
                                                        "`add(-1, 1)` returns `0`"]})
            # Before any attempt: the host's own draft, free to change.
            forge.contract(id="H-1", reason="draft", criteria=["`add(2, 3)` returns `5`"])

            run_id = forge._host_run()
            ticket = forge._find_ticket(run_id, "H-1")
            ticket.attempts = 1
            forge.store.update_ticket(run_id, ticket)
            with self.assertRaisesRegex(ApiError, "never easier"):
                forge.contract(id="H-1", reason="loosen", criteria=[])
        finally:
            forge.close()

    def test_every_revision_is_recorded_with_its_reason(self):
        forge = self._forge()
        try:
            forge.contract(**_CONTRACT)
            forge.contract(id="H-1", reason="name the file", title="Add, in calc.py")
            log = contract_history(forge, "H-1")
        finally:
            forge.close()
        self.assertEqual([e["field"] for e in log], ["created", "title"])
        self.assertEqual(log[1]["reason"], "name the file")


def contract_history(forge: Forge, ticket_id: str) -> list:
    from forge.contract import history

    return history(forge.store, forge._host_run(), ticket_id)


class TestJobs(unittest.TestCase):
    def test_a_job_whose_process_died_is_reported_lost_and_frees_its_ticket(self):
        from forge import jobs
        from forge.state import TICKET_RUNNING

        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**_CONTRACT)
            run_id = forge._host_run()
            job_id = jobs.create(forge.store, run_id, "H-1")
            ticket = forge._find_ticket(run_id, "H-1")
            ticket.status = TICKET_RUNNING
            forge.store.update_ticket(run_id, ticket)
            # This process, but not a job this process is running.
            self.assertEqual(jobs.reap(forge.store), [job_id])
            self.assertEqual(jobs.get(forge.store, job_id)["state"], "lost")
            self.assertEqual(forge._find_ticket(run_id, "H-1").status, "pending")
        finally:
            forge.close()


class TestGuardHook(unittest.TestCase):
    """The plugin's PreToolUse hook: the host does not edit what judges a
    delegated ticket (LOOP-INVARIANTS section 3, applied to the host)."""

    HOOK = Path(__file__).resolve().parent.parent / "plugins" / "forge" / "hooks" / (
        "guard_judging_files.py"
    )

    def _ask(self, root: Path, path: str) -> str:
        payload = json.dumps(
            {"cwd": str(root), "tool_name": "Edit", "tool_input": {"file_path": path}}
        )
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)], input=payload, capture_output=True,
            text=True, check=True,
        )
        if not proc.stdout.strip():
            return ""
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def _project_with_contract(self) -> tuple[Path, Forge]:
        root = _git_project()
        forge = Forge(root)
        forge.contract(**_CONTRACT)
        return root, forge

    def test_a_test_judging_an_open_ticket_is_refused(self):
        root, forge = self._project_with_contract()
        forge.close()
        self.assertIn("graded by", self._ask(root, "tests/calc_test.py"))

    def test_the_implementation_file_of_an_idle_ticket_is_allowed(self):
        root, forge = self._project_with_contract()
        forge.close()
        self.assertEqual(self._ask(root, "calc.py"), "")

    def test_any_file_of_a_ticket_being_worked_is_refused(self):
        from forge import jobs

        root, forge = self._project_with_contract()
        try:
            jobs.create(forge.store, forge._host_run(), "H-1")
        finally:
            forge.close()
        self.assertIn("is working right now", self._ask(root, str(root / "calc.py")))

    def test_a_landed_tickets_test_is_the_hosts_to_edit_again(self):
        from forge.state import TICKET_DONE

        root, forge = self._project_with_contract()
        try:
            run_id = forge._host_run()
            ticket = forge._find_ticket(run_id, "H-1")
            ticket.status = TICKET_DONE
            forge.store.update_ticket(run_id, ticket)
        finally:
            forge.close()
        self.assertEqual(self._ask(root, "tests/calc_test.py"), "")

    def test_a_project_without_forge_state_is_never_blocked(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.assertEqual(self._ask(root, "tests/anything_test.py"), "")


class TestConsultations(unittest.TestCase):
    """Review, objections and offload: model calls that are not a delegation."""

    def _answering(self, replies: dict):
        def call(self, run_id, role, messages, **kwargs):
            reply = replies[kwargs.get("model") or role]
            text = reply.pop(0) if isinstance(reply, list) else reply
            return Completion(text=text, usage=Usage(), finish_reason="stop")

        return unittest.mock.patch.object(engine.HostOrchestrator, "_call", call)

    def test_a_host_implemented_ticket_discharges_only_after_review_and_a_clean_verify(self):
        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**{**_CONTRACT, "route": "withheld:interface"})
            forge.verify(capture="start")
            (root / "calc.py").write_text(_GOOD_ADD, encoding="utf-8")
            (root / "tests" / "calc_test.py").write_text(_TEST, encoding="utf-8")

            with self.assertRaisesRegex(ApiError, "accepting forge_review"):
                forge.close_ticket("H-1", "discharge", against="start")

            with self._answering({"reviewer": "ACCEPT\n\nMatches."}):
                reviewed = forge.review("H-1", wait=None)
            self.assertEqual(reviewed["result"]["verdict"], "accept", reviewed)

            closed = forge.close_ticket("H-1", "discharge", against="start")
        finally:
            forge.close()
        self.assertEqual(closed["status"], "discharged")

    def test_a_withheld_ticket_is_not_delegated(self):
        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**{**_CONTRACT, "route": "withheld:security"})
            with self.assertRaisesRegex(ApiError, "withheld"):
                forge.delegate("H-1")
        finally:
            forge.close()

    def test_offload_recovers_json_and_validates_it(self):
        root = _git_project()
        forge = Forge(root)
        schema = {"type": "object", "required": ["names"],
                  "properties": {"names": {"type": "array", "items": {"type": "string"}}}}
        try:
            with self._answering({"executor": [
                "Sure! Here it is: {\"names\": [\"a\", 2]}",
                "```json\n{\"names\": [\"a\", \"b\"]}\n```",
            ]}):
                job = forge.offload("list two names", schema=schema, wait=None)
        finally:
            forge.close()
        self.assertEqual(job["result"]["value"], {"names": ["a", "b"]}, job)

    def test_objections_are_advice_and_park_nothing(self):
        root = _git_project()
        forge = Forge(root)
        try:
            forge.contract(**_CONTRACT)
            refusal = (
                "SIGN-OFF: no\nBLOCKING:\n- the spec does not say what add returns "
                "for strings\nSUGGESTIONS:\n- none\n"
            )
            with self._answering({"executor": refusal, "tester": refusal,
                                  "reviewer": refusal}):
                job = forge.objections("H-1", wait=None)
            ticket = forge._find_ticket(forge._host_run(), "H-1")
        finally:
            forge.close()
        answers = job["result"]["answers"]
        self.assertEqual([a["seat"] for a in answers], ["executor", "tester", "reviewer"])
        self.assertEqual(ticket.status, "pending")


class TestSchemaSubset(unittest.TestCase):
    def test_types_required_and_items(self):
        from forge.consult import validate

        schema = {"type": "object", "required": ["n"],
                  "properties": {"n": {"type": "integer"},
                                 "tags": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(validate({"n": 1, "tags": ["x"]}, schema), [])
        self.assertEqual(validate({}, schema), ["$.n is required"])
        self.assertEqual(validate({"n": True}, schema), ["$.n should be integer"])
        self.assertEqual(validate({"n": 1, "tags": [3]}, schema), ["$.tags[0] should be string"])


class TestDecisions(unittest.TestCase):
    """Autopilot's park points as a queue the host answers."""

    def _parked(self) -> tuple[Path, int]:
        root = _git_project()
        store = Store(root / ".hybridforge" / "run.db")
        try:
            run_id = store.create_run("autopilot")
            store.add_tickets(run_id, [
                Ticket("A-1", status="blocked", blocked_note="BLOCKED: which file?"),
                Ticket("A-2", route="withheld:security", status="withheld"),
                Ticket("A-3", status="done"),
            ])
        finally:
            store.close()
        return root, run_id

    def test_parked_tickets_are_listed_with_their_question(self):
        root, run_id = self._parked()
        forge = Forge(root)
        try:
            listed = forge.decisions()
        finally:
            forge.close()
        self.assertEqual([d["id"] for d in listed], ["A-1", "A-2"])
        self.assertEqual(listed[0]["question"], "BLOCKED: which file?")

    def test_release_and_retry_requeue_for_forge_go(self):
        root, run_id = self._parked()
        forge = Forge(root)
        try:
            released = forge.decide(run_id, "A-2", "release", "scope no longer touches auth")
            retried = forge.decide(run_id, "A-1", "retry", "the file is src/a.py")
            resumable = [int(r["id"]) for r in forge.store.resumable_runs()]
        finally:
            forge.close()
        self.assertEqual(released["status"], "pending")
        self.assertEqual(retried["status"], "pending")
        self.assertIn(run_id, resumable)

    def test_a_decision_without_a_reason_is_refused(self):
        root, run_id = self._parked()
        forge = Forge(root)
        try:
            with self.assertRaisesRegex(ApiError, "needs `text`"):
                forge.decide(run_id, "A-1", "retry", "")
        finally:
            forge.close()


if __name__ == "__main__":
    unittest.main()
