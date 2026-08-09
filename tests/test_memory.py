"""Tests for MCP-backed memory retrieval.

Run against a stub MCP server rather than a live MemPalace, because the whole
point of this code is surviving a server whose tool surface we cannot predict.
The stub therefore exposes a deliberately awkward surface: a mutating tool
listed first, a retrieval tool with a non-obvious name, and parameters that are
not called `query`/`room`/`limit`.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from forge.memory import (
    MemoryClient,
    MemorySettings,
    MemoryUnavailable,
    _build_arguments,
    _content_text,
    _pick_search_tool,
    _truncate,
    ticket_query,
)

# A surface designed to break naive assumptions: the write tool matches the
# "query"-ish hint list first, and the read tool uses non-standard params.
STUB_TOOLS = [
    {
        "name": "palace_write_entry",
        "description": "Store a memory",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "palace_recall",
        "description": "Recall prior decisions",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "space": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["q"],
        },
    },
]


class StubMCPHandler(BaseHTTPRequestHandler):
    sse = False
    calls: list[dict] = []

    def log_message(self, *args):  # noqa: A003
        return

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method = body.get("method")

        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return

        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": STUB_TOOLS}
        elif method == "tools/call":
            type(self).calls.append(body["params"])
            result = {
                "content": [
                    {"type": "text", "text": "Decision: chose tiny-skia over resvg (2026-05)."}
                ],
                "isError": False,
            }
        else:
            result = {}

        message = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}

        if self.sse:
            payload = f"event: message\ndata: {json.dumps(message)}\n\n".encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(message).encode()
            content_type = "application/json"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Mcp-Session-Id", "stub-session-1")
        self.end_headers()
        self.wfile.write(payload)


def start_stub(sse: bool = False):
    handler = type("Bound", (StubMCPHandler,), {"sse": sse, "calls": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler, f"http://127.0.0.1:{server.server_port}/mcp"


class TestToolSelection(unittest.TestCase):
    def test_never_picks_a_mutating_tool(self):
        # palace_write_entry matches "query" via its schema and comes first;
        # picking it would write to memory instead of reading.
        chosen = _pick_search_tool(STUB_TOOLS)
        self.assertEqual(chosen["name"], "palace_recall")

    def test_explicit_override_wins(self):
        chosen = _pick_search_tool(STUB_TOOLS, preferred="palace_write_entry")
        self.assertEqual(chosen["name"], "palace_write_entry")

    def test_returns_none_when_nothing_looks_like_search(self):
        self.assertIsNone(_pick_search_tool([{"name": "delete_everything"}]))


class TestArgumentBuilding(unittest.TestCase):
    def test_uses_the_names_the_schema_declares(self):
        args = _build_arguments(STUB_TOOLS[1], query="png export", room="marquee", limit=4)
        self.assertEqual(args, {"q": "png export", "space": "marquee", "top_k": 4})

    def test_omits_room_when_the_tool_has_no_such_param(self):
        tool = {"inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}
        self.assertEqual(_build_arguments(tool, query="x", room="r", limit=3), {"query": "x"})

    def test_falls_back_to_the_lone_required_string(self):
        tool = {
            "inputSchema": {
                "type": "object",
                "properties": {"needle": {"type": "string"}},
                "required": ["needle"],
            }
        }
        self.assertEqual(_build_arguments(tool, query="x", room="", limit=3), {"needle": "x"})

    def test_schemaless_tool_gets_the_common_shape(self):
        self.assertEqual(
            _build_arguments({}, query="x", room="r", limit=3), {"query": "x", "room": "r"}
        )


class TestLiveRetrieval(unittest.TestCase):
    def _client(self, url, **kwargs):
        return MemoryClient(MemorySettings(url=url, room="marquee", **kwargs))

    def test_end_to_end_over_json(self):
        server, handler, url = start_stub()
        try:
            text = self._client(url).search("png export at configurable DPI")
            self.assertIn("tiny-skia", text)
            self.assertEqual(handler.calls[0]["name"], "palace_recall")
            self.assertEqual(handler.calls[0]["arguments"]["space"], "marquee")
        finally:
            server.shutdown()

    def test_end_to_end_over_sse(self):
        # Streamable HTTP may answer either way; both must parse.
        server, _, url = start_stub(sse=True)
        try:
            self.assertIn("tiny-skia", self._client(url).search("anything"))
        finally:
            server.shutdown()

    def test_unreachable_server_raises_not_hangs(self):
        client = self._client("http://127.0.0.1:9/mcp", timeout=2)
        with self.assertRaises(MemoryUnavailable):
            client.search("x")

    def test_describe_reports_the_discovered_surface(self):
        server, _, url = start_stub()
        try:
            report = self._client(url).describe()
            self.assertTrue(report.startswith("ok"))
            self.assertIn("palace_recall", report)
        finally:
            server.shutdown()


class TestLoopIntegration(unittest.TestCase):
    def test_memory_failure_never_kills_a_run(self):
        """A memory outage must degrade the run, not end it."""
        import tempfile
        from pathlib import Path

        from forge.config import Config
        from forge.loop import Orchestrator
        from forge.state import Store, Ticket

        root = Path(tempfile.mkdtemp())
        (root / ".hybridforge").mkdir()
        (root / ".hybridforge" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"m": {"kind": "openai", "model": "x"}},
                    "roles": {r: "m" for r in ("planner", "executor", "tester", "reviewer")},
                    # Port 9 is the discard port — reliably refuses.
                    "memory": {"url": "http://127.0.0.1:9/mcp", "room": "r", "timeout": 2},
                }
            )
        )
        config = Config.load(root)
        store = Store(config.db_path)
        run_id = store.create_run("goal")

        orchestrator = Orchestrator(config, store)
        self.assertIsNotNone(orchestrator.memory)

        retrieved = orchestrator._retrieve_context(run_id, Ticket("T-1", "title", spec="s"))
        self.assertEqual(retrieved, "")

        warnings = [
            row for row in store.events_after(0) if row["kind"] == "memory"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "warn")

        # Second failure must not re-log — twenty tickets should not mean
        # twenty identical warnings.
        orchestrator._retrieve_context(run_id, Ticket("T-2", "title", spec="s"))
        self.assertEqual(
            len([row for row in store.events_after(0) if row["kind"] == "memory"]), 1
        )

    def test_no_memory_configured_is_silent(self):
        self.assertIsNone(MemoryClient.from_config({}))
        self.assertIsNone(MemoryClient.from_config({"url": "http://x", "enabled": False}))


class TestHelpers(unittest.TestCase):
    def test_query_stays_narrow(self):
        query = ticket_query(
            "Add PNG export",
            "Long spec paragraph one.\n\nParagraph two is not included.",
            ["src/png.rs"],
        )
        self.assertIn("Add PNG export", query)
        self.assertIn("src/png.rs", query)
        self.assertNotIn("Paragraph two", query)

    def test_truncation_respects_the_token_budget(self):
        text = "\n\n".join(f"Paragraph {i} " + "word " * 50 for i in range(40))
        trimmed = _truncate(text, max_tokens=100)
        self.assertLess(len(trimmed), len(text))
        self.assertIn("[memory truncated]", trimmed)

    def test_short_text_is_untouched(self):
        self.assertEqual(_truncate("short note", 1000), "short note")

    def test_content_text_handles_structured_results(self):
        self.assertIn("alpha", _content_text({"structuredContent": {"k": "alpha"}}))


if __name__ == "__main__":
    unittest.main()


class TestSecretScanner(unittest.TestCase):
    """A scanner that silently matches nothing is worse than no scanner."""

    CLEAN = [
        "chose tiny-skia over resvg for rendering",
        "convention: read the key from ANTHROPIC_API_KEY, never hardcode it",
        "set API_KEY to your_api_key_here in .env",
        "the auth token is stored in the keychain",
    ]
    DIRTY = [
        "set ANTHROPIC_API_KEY=sk-ant-abcdefghij0123456789xyz",
        "api_key: 9f8e7d6c5b4a39281706abcdef012345",
        "use AKIAIOSFODNN7EXAMPLE for the bucket",
        "token ghp_abcdefghijklmnopqrstuvwxyz012345",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer eyJhbGciOiJI0123456789abcdefghij",
    ]

    def test_passes_prose_about_credentials(self):
        from forge.secrets import find_secrets

        for text in self.CLEAN:
            self.assertEqual(find_secrets(text), [], text)

    def test_catches_credential_shapes(self):
        from forge.secrets import find_secrets

        for text in self.DIRTY:
            self.assertTrue(find_secrets(text), text)

    def test_placeholder_exemption_does_not_cover_structural_patterns(self):
        # AWS's own doc key contains "EXAMPLE"; a real key could too. Structural
        # matches are never exempted, only the loose keyword pattern is.
        from forge.secrets import find_secrets

        self.assertTrue(find_secrets("AKIAIOSFODNN7EXAMPLE"))


class TestWriteToolSelection(unittest.TestCase):
    TOOLS = [
        {"name": "palace_delete_entry"},
        {"name": "palace_recall"},
        {"name": "palace_remember"},
    ]

    def test_never_picks_a_destructive_tool(self):
        from forge.memory import _pick_write_tool

        self.assertEqual(_pick_write_tool(self.TOOLS)["name"], "palace_remember")

    def test_explicit_destructive_name_is_refused(self):
        from forge.memory import MemoryRefused, _pick_write_tool

        with self.assertRaises(MemoryRefused):
            _pick_write_tool(self.TOOLS, preferred="palace_delete_entry")

    def test_returns_none_when_no_write_tool_exists(self):
        from forge.memory import _pick_write_tool

        self.assertIsNone(_pick_write_tool([{"name": "palace_recall"}]))


class TestWriteGuards(unittest.TestCase):
    def _client(self, **kwargs):
        return MemoryClient(
            MemorySettings(url="http://127.0.0.1:9/mcp", room="r", write=True, **kwargs)
        )

    def test_writes_are_off_by_default(self):
        settings = MemorySettings.from_config({"url": "http://x/mcp"})
        self.assertFalse(settings.write)
        self.assertFalse(settings.writes_enabled)

    def test_credential_entry_is_refused_before_any_connection(self):
        from forge.memory import MemoryRefused

        # Port 9 would hang/refuse — reaching it at all means the scan came
        # after the network call, which would be the wrong order.
        with self.assertRaises(MemoryRefused):
            self._client().remember("deploy key sk-ant-abcdefghij0123456789xyzzy")

    def test_oversized_entry_is_refused(self):
        result = self._client(max_write_chars=50).remember("x" * 200)
        self.assertIn("exceeds maxWriteChars", result)

    def test_empty_entry_is_skipped(self):
        self.assertIn("nothing to record", self._client().remember("   "))

    def test_disabled_writes_never_reach_the_network(self):
        client = MemoryClient(MemorySettings(url="http://127.0.0.1:9/mcp"))
        self.assertIn("off", client.remember("anything"))


class TestWriteEndToEnd(unittest.TestCase):
    def test_writes_through_a_nonstandard_schema(self):
        server, handler, url = start_stub()
        try:
            client = MemoryClient(
                MemorySettings(url=url, room="marquee", write=True, write_tool="palace_recall")
            )
            # Reuse the stub's recall tool as a stand-in write target: what is
            # under test is schema-driven argument mapping, not the tool name.
            result = client.remember("Chose tiny-skia.", title="Renderer")
            self.assertIn("recorded", result)
            self.assertEqual(handler.calls[-1]["arguments"]["q"], "Chose tiny-skia.")
        finally:
            server.shutdown()

    def test_dry_run_sends_nothing(self):
        server, handler, url = start_stub()
        try:
            client = MemoryClient(
                MemorySettings(
                    url=url, room="marquee", write=True,
                    write_tool="palace_recall", dry_run=True,
                )
            )
            result = client.remember("Chose tiny-skia.", title="Renderer")
            self.assertIn("dry-run", result)
            # tools/list happened, but no tools/call.
            self.assertEqual(handler.calls, [])
        finally:
            server.shutdown()


class TestRecordParsing(unittest.TestCase):
    def test_nothing_is_the_common_answer(self):
        from forge.prompts import parse_record

        for reply in ("NOTHING", "NOTHING\n", "nothing"):
            self.assertEqual(parse_record(reply), ("", ""))

    def test_extracts_title_and_body(self):
        from forge.prompts import parse_record

        title, entry = parse_record("TITLE: Use tiny-skia\nBecause resvg pulls a font stack.")
        self.assertEqual(title, "Use tiny-skia")
        self.assertIn("font stack", entry)

    def test_tolerates_a_preamble(self):
        from forge.prompts import parse_record

        title, _ = parse_record("Sure.\nTITLE: Slug rules\nSlugs stay in slug.py.")
        self.assertEqual(title, "Slug rules")

    def test_title_with_no_body_records_nothing(self):
        from forge.prompts import parse_record

        self.assertEqual(parse_record("TITLE: bare"), ("", ""))
