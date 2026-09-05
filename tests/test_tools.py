"""The read tools, the repository map, and the conversation that uses them.

Written against run 1 of `HANDBACK-DASHBOARD.md`, which ended blocked after
nine attempts having written nothing: 83% of its prompt was a test suite the
ticket never mentioned, the one file its spec named was absent, and the
executor spent every attempt asking for a shell. See docs/CONTEXT-TOOLS.md.

The scripted model here answers with tool calls the way that run's model wanted
to and could not, so what is checked is the loop's half of the exchange: that a
call is answered, that a refusal is content rather than an exception, that the
turn cap ends the conversation with an answer, and that a role whose provider
cannot take tools still gets the prompt it always got.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge.config import Config
from forge.prompts import (
    REPO_MAP_HEADING,
    TOOLS_HEADING,
    build_prompt,
    stable_prefix,
)
from forge.providers.base import (
    Capabilities,
    Completion,
    Message,
    Provider,
    ToolCall,
    ToolSpec,
    Usage,
)
from forge.providers.openai_compat import _tool_calls, _turn
from forge.repomap import repo_map
from forge.state import Store, Ticket
from forge.tools import TOOLS, MAX_RESULT_CHARS, Toolbox


def _project(root: Path) -> Path:
    """A small repository with something worth reading in it."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "store.py").write_text(
        '"""Storage."""\n\n\nRETRYABLE = ("failed", "blocked")\n\n\n'
        "class Store:\n"
        "    def save(self, row, *, flush=True):\n"
        "        return row\n",
        encoding="utf-8",
    )
    (root / "src" / "ui.py").write_text(
        "def render(rows):\n    return list(rows)\n", encoding="utf-8"
    )
    for name in ("a", "b", "c"):
        (root / "tests" / f"test_{name}.py").write_text(
            f"def test_{name}():\n    assert True\n" * 40, encoding="utf-8"
        )
    return root


class TestTheToolsAnswerWhatARoleAsks(unittest.TestCase):
    def setUp(self):
        self.root = _project(Path(tempfile.mkdtemp()))
        self.box = Toolbox(self.root)

    def _run(self, name, **arguments):
        return self.box.run(ToolCall(call_id="c1", name=name, arguments=arguments))

    def test_read_file_returns_numbered_lines(self):
        result = self._run("read_file", path="src/store.py")

        self.assertTrue(result.ok)
        self.assertIn("RETRYABLE", result.content)
        self.assertIn("     1\t", result.content)

    def test_read_file_honours_a_range_and_says_what_is_left(self):
        result = self._run("read_file", path="tests/test_a.py", start=1, end=3)

        self.assertIn("lines 1-3 of 80", result.content)
        self.assertIn("start=4", result.content)

    def test_a_path_outside_the_repository_is_refused_as_content(self):
        # Not an exception. The model gets another turn either way, and a
        # refusal it can read is worth more than a traceback it cannot.
        result = self._run("read_file", path="../../.ssh/id_rsa")

        self.assertFalse(result.ok)
        self.assertIn("outside the repository", result.content)

    def test_an_absolute_path_is_refused_with_the_form_that_works(self):
        result = self._run("read_file", path="/etc/passwd")

        self.assertFalse(result.ok)
        self.assertIn("relative to the repository root", result.content)

    def test_grep_reports_path_line_and_text(self):
        result = self._run("grep", pattern=r"RETRYABLE")

        self.assertTrue(result.ok)
        self.assertIn("src/store.py:4:", result.content)

    def test_grep_can_be_narrowed_by_a_glob_written_either_way(self):
        anywhere = self._run("grep", pattern="def ", glob="*.py")
        directory = self._run("grep", pattern="def ", glob="src/*.py")

        self.assertIn("src/store.py", anywhere.content)
        self.assertIn("src/ui.py", directory.content)
        self.assertNotIn("tests/", directory.content)

    def test_a_pattern_that_matches_nothing_says_so_and_is_not_a_failure(self):
        result = self._run("grep", pattern="nothing_matches_this")

        self.assertTrue(result.ok)
        self.assertIn("no line matches", result.content)

    def test_a_broken_regular_expression_is_reported_as_one(self):
        result = self._run("grep", pattern="(unclosed")

        self.assertFalse(result.ok)
        self.assertIn("not a valid regular expression", result.content)

    def test_outline_lists_definitions_without_bodies(self):
        result = self._run("outline", path="src/store.py")

        self.assertIn("class Store", result.content)
        self.assertIn("def save(self, row, *, flush)", result.content)
        self.assertNotIn("return row", result.content)

    def test_list_dir_marks_directories(self):
        result = self._run("list_dir", path=".")

        self.assertIn("src/", result.content)
        self.assertIn("tests/", result.content)

    def test_an_unknown_tool_names_the_ones_that_exist(self):
        result = self._run("shell", command="ls")

        self.assertFalse(result.ok)
        self.assertIn("read_file", result.content)

    def test_a_result_is_capped_and_says_how_to_narrow_it(self):
        big = "x = 1\n" * 20_000
        (self.root / "src" / "big.py").write_text(big, encoding="utf-8")

        result = self._run("read_file", path="src/big.py", start=1, end=20_000)

        self.assertLess(len(result.content), MAX_RESULT_CHARS + 200)
        self.assertIn("narrower range", result.content)

    def test_the_ledger_records_what_was_read_and_what_was_refused(self):
        self._run("read_file", path="src/store.py")
        self._run("read_file", path="../outside")

        self.assertEqual(
            [(name, ok) for name, _, ok in self.box.ledger],
            [("read_file", True), ("read_file", False)],
        )


class TestTheRepositoryMap(unittest.TestCase):
    def setUp(self):
        self.root = _project(Path(tempfile.mkdtemp()))

    def test_it_carries_paths_and_definitions_but_not_bodies(self):
        text = repo_map(self.root)

        self.assertIn("src/", text)
        self.assertIn("class Store", text)
        self.assertIn("RETRYABLE", text)
        self.assertNotIn("return row", text)

    def test_it_is_stable_across_calls(self):
        # The whole reason it can sit in a cached prefix.
        self.assertEqual(repo_map(self.root), repo_map(self.root))

    def test_a_map_cut_for_budget_says_what_it_dropped(self):
        text = repo_map(self.root, limit=40)

        self.assertIn("map truncated", text)
        self.assertIn("list_dir", text)

    def test_it_is_smaller_than_the_files_it_describes(self):
        pasted = sum(
            len(path.read_text(encoding="utf-8"))
            for path in self.root.rglob("*.py")
        )

        self.assertLess(len(repo_map(self.root)), pasted)


class _Scripted(Provider):
    """A provider that answers from a script, and records what it was sent."""

    kind = "scripted"

    def __init__(self, replies, *, tools_ok=True):
        super().__init__("scripted", {})
        self.replies = list(replies)
        self.tools_ok = tools_ok
        self.seen: list[list[Message]] = []
        self.tools_offered: list[int] = []

    def capabilities(self):
        return Capabilities(
            context_window=200_000,
            max_output_tokens=4096,
            supports_tools=self.tools_ok,
        )

    def complete(self, messages, *, max_tokens, temperature=0.2, timeout=0, tools=()):
        self.seen.append(list(messages))
        self.tools_offered.append(len(tools))
        text, calls = self.replies.pop(0)
        return Completion(
            text=text,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            tool_calls=list(calls),
        )


class TestTheConversation(unittest.TestCase):
    """`Orchestrator._converse`: the unit above one call."""

    def setUp(self):
        self.root = _project(Path(tempfile.mkdtemp()))
        self.store = Store(self.root / "t.db")
        self.run_id = self.store.create_run("tools")

    def _orchestrator(self, provider):
        from forge.loop import Orchestrator

        config = Config(
            root=self.root,
            models={"m": {"kind": "openai", "model": "x", "contextWindow": 200_000}},
            roles={r: "m" for r in ("planner", "executor", "tester", "reviewer")},
        )
        orch = Orchestrator(config, self.store)
        orch.config.provider_for = lambda role: provider  # noqa: ARG005
        orch.config.model_name_for = lambda role: "m"  # noqa: ARG005
        return orch

    def test_a_tool_call_is_answered_and_the_model_gets_another_turn(self):
        provider = _Scripted(
            [
                ("", [ToolCall("c1", "read_file", {"path": "src/store.py"})]),
                ("here is the patch", []),
            ]
        )
        orch = self._orchestrator(provider)

        completion = orch._converse(
            self.run_id, "executor", [Message(role="user", content="do it")],
            max_tokens=1000,
        )

        self.assertEqual(completion.text, "here is the patch")
        # Second call carried the first answer and the file's contents.
        second = provider.seen[1]
        self.assertEqual(second[-1].role, "tool")
        self.assertIn("RETRYABLE", second[-1].tool_result.content)

    def test_narration_alongside_a_tool_call_is_kept_not_taken_as_the_answer(self):
        provider = _Scripted(
            [
                ("Let me look first.", [ToolCall("c1", "list_dir", {"path": "src"})]),
                ("done", []),
            ]
        )
        orch = self._orchestrator(provider)

        orch._converse(
            self.run_id, "executor", [Message(role="user", content="do it")],
            max_tokens=1000,
        )

        assistant = [m for m in provider.seen[1] if m.role == "assistant"]
        self.assertEqual(assistant[0].content, "Let me look first.")
        self.assertEqual(assistant[0].tool_calls[0].name, "list_dir")

    def test_the_turn_cap_ends_the_conversation_with_the_tools_withdrawn(self):
        # A model that keeps reading is given a last turn with no tools and an
        # instruction to answer, rather than being cut off mid-conversation.
        reading = ("", [ToolCall("c", "list_dir", {"path": "src"})])
        provider = _Scripted([reading] * 3 + [("final answer", [])])
        orch = self._orchestrator(provider)
        orch.config.loop.tool_turns = 4

        completion = orch._converse(
            self.run_id, "executor", [Message(role="user", content="do it")],
            max_tokens=1000,
        )

        self.assertEqual(completion.text, "final answer")
        self.assertEqual(provider.tools_offered[-1], 0)
        self.assertGreater(provider.tools_offered[0], 0)
        self.assertIn(
            "last read",
            "".join(m.text for m in provider.seen[-1] if m.role == "user"),
        )

    def test_a_provider_without_tools_is_called_once_and_offered_none(self):
        provider = _Scripted([("answer", [])], tools_ok=False)
        orch = self._orchestrator(provider)

        completion = orch._converse(
            self.run_id, "executor", [Message(role="user", content="do it")],
            max_tokens=1000,
        )

        self.assertEqual(completion.text, "answer")
        self.assertEqual(provider.tools_offered, [0])

    def test_what_was_read_is_recorded_in_the_run_log(self):
        provider = _Scripted(
            [
                ("", [ToolCall("c1", "read_file", {"path": "src/store.py"})]),
                ("done", []),
            ]
        )
        orch = self._orchestrator(provider)

        orch._converse(
            self.run_id, "executor", [Message(role="user", content="do it")],
            max_tokens=1000,
        )

        logged = " ".join(row["message"] for row in self.store.events_after(0))
        self.assertIn("read_file(src/store.py", logged)


class TestThePromptLayout(unittest.TestCase):
    """Phase 3 and 4: a stable prefix, and reading granted rather than pasted."""

    def _ticket(self):
        return Ticket(
            ticket_id="T-1",
            title="wire it",
            spec="add the key",
            allowed_files=["src/ui.py"],
            reference_files=["src/store.py"],
            criteria=["render returns a list"],
        )

    def test_the_map_and_tools_note_come_before_anything_the_ticket_owns(self):
        messages = build_prompt(
            self._ticket(),
            sources={"src/ui.py": "def render(rows): ...", "src/store.py": "..."},
            repository_map="src/\n  store.py",
            can_read=True,
        )

        headings = [m.text[:40] for m in messages]
        map_at = next(i for i, h in enumerate(headings) if REPO_MAP_HEADING in h)
        tools_at = next(i for i, h in enumerate(headings) if TOOLS_HEADING in h)
        ticket_at = next(i for i, h in enumerate(headings) if h.startswith("Ticket:"))
        self.assertLess(map_at, ticket_at)
        self.assertLess(tools_at, ticket_at)
        self.assertEqual(messages[0].role, "system")

    def test_a_role_that_can_read_is_given_reference_names_not_contents(self):
        messages = build_prompt(
            self._ticket(),
            sources={"src/ui.py": "WRITABLE BODY", "src/store.py": "REFERENCE BODY"},
            can_read=True,
        )

        body = "\n".join(m.text for m in messages)
        self.assertIn("src/store.py", body)
        self.assertNotIn("REFERENCE BODY", body)
        # The file it must return complete is still pasted complete.
        self.assertIn("WRITABLE BODY", body)

    def test_a_role_that_cannot_read_still_gets_the_pasted_reference(self):
        messages = build_prompt(
            self._ticket(),
            sources={"src/ui.py": "WRITABLE BODY", "src/store.py": "REFERENCE BODY"},
            can_read=False,
        )

        body = "\n".join(m.text for m in messages)
        self.assertIn("REFERENCE BODY", body)

    def test_the_reviewer_gets_the_map_and_the_tools_too(self):
        # The role where reading changes what a verdict is worth: run 1
        # produced nine rejections quoting a function that had never been
        # written, against an empty diff it could not check.
        from forge.prompts import review_prompt

        messages = review_prompt(
            self._ticket(), "", repository_map="src/\n  store.py", can_read=True
        )

        body = "\n".join(m.text for m in messages)
        self.assertIn(REPO_MAP_HEADING, body)
        self.assertIn(TOOLS_HEADING, body)

    def test_the_tester_gets_them_as_well(self):
        from forge.prompts import write_tests_prompt

        messages = write_tests_prompt(
            self._ticket(),
            ["src/ui.py"],
            test_path="tests/test_ui.py",
            repository_map="src/\n  store.py",
            can_read=True,
        )

        body = "\n".join(m.text for m in messages)
        self.assertIn(REPO_MAP_HEADING, body)
        self.assertIn(TOOLS_HEADING, body)

    def test_the_prefix_is_identical_for_two_different_tickets(self):
        # What makes it cacheable. Two tickets, one repository, same bytes.
        first = stable_prefix("MAP", {"lint": ".flake8"}, can_read=True)
        second = stable_prefix("MAP", {"lint": ".flake8"}, can_read=True)

        self.assertEqual(
            [m.content for m in first], [m.content for m in second]
        )


class TestTheWireCarriesAToolExchange(unittest.TestCase):
    def test_a_result_goes_back_against_the_id_it_answers(self):
        from forge.providers.base import ToolResult

        turn = _turn(
            Message(
                role="tool",
                content="",
                tool_result=ToolResult(call_id="abc", name="grep", content="hit"),
            )
        )

        self.assertEqual(turn, {"role": "tool", "tool_call_id": "abc", "content": "hit"})

    def test_arguments_that_do_not_parse_still_produce_a_named_call(self):
        # A truncated reply cuts the JSON mid-object. The call is kept so the
        # tool layer can refuse it by name and say what was wrong.
        calls = _tool_calls(
            {"tool_calls": [{"id": "x", "function": {"name": "grep", "arguments": "{"}}]}
        )

        self.assertEqual(calls[0].name, "grep")
        self.assertEqual(calls[0].arguments, {})

    def test_every_declared_tool_has_a_schema_the_wire_can_encode(self):
        for spec in TOOLS:
            self.assertIsInstance(spec, ToolSpec)
            self.assertEqual(spec.parameters["type"], "object")
            self.assertTrue(spec.description.strip())


if __name__ == "__main__":
    unittest.main()
