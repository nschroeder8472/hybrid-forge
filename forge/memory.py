"""Project memory retrieval over MCP.

The loop needs prior decisions to reach the executor, or it will cheerfully
contradict a convention established weeks ago. Claude Code gets that through
its own MCP client; the daemon has no such client, so this is one.

Two constraints shaped it:

**Stdlib only.** The daemon's no-dependency promise is load-bearing — an
overnight run must not fail to start because a package is missing. So this is a
minimal JSON-RPC client over MCP rather than the MCP SDK.

**Two transports, because one server is reached two ways.** `memory.url` speaks
Streamable HTTP to a palace already listening somewhere; `memory.command` runs
one as a child process and speaks newline-delimited JSON-RPC over its stdin.
MemPalace serves both — `mempalace-mcp` for stdio, `mempalace serve` for HTTP —
and which you want follows from where the palace sits. Sharing a machine with
the daemon, `command` is the direct route: no port, no listener, no token.

**MemPalace's tool surface moves between versions**, as this project's own
setup docs warn. So nothing here hardcodes a tool name. The client asks the
server what it exposes, picks the tool that looks like search, and fills only
the parameters that tool's schema actually declares. When it cannot find one it
says which tools it *did* see, rather than failing with a mystery.

Retrieval is always best-effort. A memory outage degrades the run to "no
context" — it never ends it.

**Write-back is opt-in and guarded.** Retrieval only reads; recording mutates a
durable store that every future session will read back, with no undo. So it is
off unless `memory.write` is explicitly true, it refuses entries that look like
they contain credentials, it will never auto-select a tool whose name suggests
deletion, and `memory.dryRun` lets you watch what it *would* record before it
records anything.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .secrets import find_secrets
from .tokens import estimate_text

# Protocol versions to try, newest first. The handshake is the one place a
# version mismatch shows up, so falling back beats hard-failing.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# A tool is treated as "search" if its name contains one of these. Ordered by
# how strongly the word implies retrieval rather than mutation.
_SEARCH_HINTS = ("search", "recall", "retrieve", "query", "find", "lookup", "get_memor")

# Never auto-select a tool whose name suggests it writes. Picking a mutating
# tool because it happened to match "query" would be considerably worse than
# retrieving nothing.
_WRITE_HINTS = ("write", "create", "add", "store", "save", "delete", "remove", "update", "put")

# Names that suggest a tool records a memory. Ordered most-specific first so
# `remember` beats a generic `add`.
_WRITE_HINTS_POSITIVE = (
    "remember", "record", "write", "store", "save", "add_memor", "create_memor",
    "add_entry", "append", "note", "insert", "put", "add",
)
# Never auto-select these, whatever else their name contains. A `delete` that
# happens to include "record" would be catastrophic and silent.
_WRITE_HINTS_FORBIDDEN = (
    "delete", "remove", "drop", "purge", "clear", "destroy", "reset", "wipe",
    "prune", "forget",
)

# Candidate parameter names, by role. Only ones present in the tool's declared
# schema are sent.
_QUERY_PARAMS = ("query", "q", "text", "search", "prompt", "question")
_ROOM_PARAMS = ("room", "space", "scope", "collection", "project", "namespace")
_LIMIT_PARAMS = ("limit", "top_k", "topK", "n", "max_results", "maxResults", "k")
_ENTRY_PARAMS = ("content", "text", "entry", "body", "memory", "note", "value", "data")
_TITLE_PARAMS = ("title", "name", "summary", "subject", "label", "key")


class MemoryUnavailable(Exception):
    """Retrieval failed. Always caught by the loop — never fatal.

    Deliberately not named MemoryError: that is a builtin, and shadowing it
    would make an unrelated `except MemoryError` silently catch this instead.
    """


class MemoryRefused(MemoryUnavailable):
    """A write was rejected by our own guardrails, not by the server.

    Distinct from a transport failure: nothing was sent, and retrying the same
    content will be refused identically. The loop surfaces it loudly because a
    refused write usually means a credential reached a place it should not
    have — worth a human look even though the run continues.
    """


class MemoryUnreachable(MemoryUnavailable):
    """Transport-level failure — no server answered.

    Split out because it must not trigger protocol-version fallback. Retrying
    the handshake with an older version against a host that is simply down
    multiplies one timeout by the number of versions we know about, which on a
    twenty-ticket overnight run is an hour of nothing.
    """


# ----------------------------------------------------------------------
# Minimal MCP client — transport-agnostic half
# ----------------------------------------------------------------------


class _MCPSession:
    """The MCP conversation, with the transport left abstract.

    Streamable HTTP reaches a server already listening somewhere; stdio reaches
    one the daemon starts itself. MemPalace offers both, so the choice is about
    placement rather than capability: stdio for the ordinary case where memory
    and the daemon share a machine, HTTP for a palace running `mempalace serve`
    on another.

    Everything above the wire — the version handshake, tool discovery, tool
    calls — is identical either way, so it lives here and each transport
    supplies only `_rpc` and `_notify`.
    """

    def __init__(self, *, timeout: int = 30):
        self.timeout = timeout
        self.protocol_version: str | None = None
        self._next_id = 0

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def _notify(self, method: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release whatever the transport holds. Safe to call twice."""

    @property
    def endpoint(self) -> str:
        """How this connection is named in errors and `forge doctor`."""
        raise NotImplementedError

    def _payload(self, method: str, params: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        return payload, self._next_id

    @staticmethod
    def _result_or_raise(message: dict[str, Any], method: str) -> dict[str, Any]:
        if "error" in message:
            error = message["error"]
            raise MemoryUnavailable(
                f"{method} failed: {error.get('message')} ({error.get('code')})"
            )
        return message.get("result") or {}

    # ------------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Run the initialize handshake, negotiating a protocol version."""
        last_error: MemoryUnavailable | None = None
        for version in PROTOCOL_VERSIONS:
            try:
                result = self._rpc(
                    "initialize",
                    {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": "hybrid-forge", "version": "0.2.0"},
                    },
                )
            except MemoryUnreachable:
                # Nothing answered; a different protocol version will not help.
                raise
            except MemoryUnavailable as exc:
                last_error = exc
                continue
            # Prefer whatever the server reports it speaks.
            self.protocol_version = result.get("protocolVersion") or version
            self._notify("notifications/initialized")
            return result
        raise last_error or MemoryUnavailable("initialize failed for every known protocol version")

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list")
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MemoryUnavailable(f"tool {name!r} reported an error: {_content_text(result)[:300]}")
        return _content_text(result)


# ----------------------------------------------------------------------
# Streamable HTTP transport
# ----------------------------------------------------------------------


class MCPClient(_MCPSession):
    """MCP over Streamable HTTP — a palace reachable across a network."""

    def __init__(self, url: str, *, timeout: int = 30, headers: dict[str, str] | None = None):
        super().__init__(timeout=timeout)
        self.url = url.rstrip("/")
        self.extra_headers = headers or {}
        self.session_id: str | None = None

    @property
    def endpoint(self) -> str:
        return self.url

    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Streamable HTTP may answer with either shape; accept both.
            "Accept": "application/json, text/event-stream",
            **self.extra_headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, request_id = self._payload(method, params)

        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                # The server assigns a session on initialize; echo it thereafter.
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:  # noqa: BLE001 - error body is best-effort
                pass
            if exc.code >= 500:
                raise MemoryUnreachable(f"{self.url} returned {exc.code}: {detail}") from exc
            raise MemoryUnavailable(f"{self.url} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MemoryUnreachable(f"could not reach {self.url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MemoryUnreachable(
                f"timed out after {self.timeout}s reaching {self.url}"
            ) from exc

        return self._result_or_raise(_parse_response(body, content_type, request_id), method)

    def _notify(self, method: str) -> None:
        """Fire-and-forget notification (no id, no response expected)."""
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "method": method}).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except Exception:  # noqa: BLE001 - a dropped notification is not fatal
            pass

# ----------------------------------------------------------------------
# stdio transport
# ----------------------------------------------------------------------


class StdioMCPClient(_MCPSession):
    """MCP over a child process's stdin/stdout.

    The transport most MCP servers actually ship, MemPalace included: newline-
    delimited JSON-RPC, one message per line, no framing beyond that.

    Reading needs a thread rather than `select`, because `select` does not work
    on pipes on Windows and the daemon runs there. stderr gets a thread of its
    own for a duller reason: an undrained stderr pipe fills its OS buffer and
    the child blocks forever, which would look exactly like a hung palace. The
    tail it keeps is what turns "the server exited" into a message naming why.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        super().__init__(timeout=timeout)
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._errors: deque[str] = deque(maxlen=20)

    @property
    def endpoint(self) -> str:
        return " ".join(self.command)

    # ------------------------------------------------------------------

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, never shell=True
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Pinned rather than inherited: a palace entry containing an em
                # dash must not die on a cp1252 console, same as the providers.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=self.env,
            )
        except (OSError, ValueError) as exc:
            raise MemoryUnreachable(f"could not start {self.endpoint!r}: {exc}") from exc

        self._proc = proc
        threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        atexit.register(self.close)
        return proc

    def _pump_stdout(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._lines.put(line)
        # Sentinel: readers waiting on a reply must learn the server is gone
        # rather than sit until their timeout expires.
        self._lines.put(None)

    def _pump_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            self._errors.append(line.rstrip())

    def _stderr_tail(self) -> str:
        tail = " / ".join(self._errors)
        return f" stderr: {tail[-400:]}" if tail else ""

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._ensure_process()
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MemoryUnreachable(
                f"{self.endpoint} closed its input: {exc}{self._stderr_tail()}"
            ) from exc

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, request_id = self._payload(method, params)
        self._send(payload)

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryUnreachable(
                    f"timed out after {self.timeout}s waiting for {method} "
                    f"from {self.endpoint}{self._stderr_tail()}"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                code = self._proc.poll() if self._proc else None
                raise MemoryUnreachable(
                    f"{self.endpoint} exited (code {code}) before answering "
                    f"{method}{self._stderr_tail()}"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Servers that log to stdout are out of spec but common. A line
                # that is not JSON cannot be a reply, so it is not our problem.
                continue
            # Notifications and server-initiated requests share the stream;
            # anything not carrying our id belongs to someone else.
            if isinstance(message, dict) and message.get("id") == request_id:
                return self._result_or_raise(message, method)

    def _notify(self, method: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method})
        except MemoryUnavailable:
            pass  # a dropped notification is not fatal

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


def _parse_response(body: str, content_type: str, request_id: int) -> dict[str, Any]:
    """Read a JSON-RPC response from either a plain body or an SSE stream."""
    if "text/event-stream" in content_type:
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                message = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        raise MemoryUnavailable("event stream carried no response for this request")

    try:
        message = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MemoryUnavailable(f"non-JSON response: {body[:300]}") from exc

    # Some servers answer a single request with a batch of one.
    if isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("id") == request_id:
                return item
        raise MemoryUnavailable("batch response carried no matching id")
    return message


def _content_text(result: dict[str, Any]) -> str:
    """Flatten an MCP tool result's content blocks into text."""
    blocks = result.get("content")
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        # Some servers return structured content instead of blocks.
        structured = result.get("structuredContent")
        return json.dumps(structured, indent=2) if structured else ""
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif block.get("type") == "resource":
            resource = block.get("resource") or {}
            if resource.get("text"):
                parts.append(str(resource["text"]))
    return "\n".join(parts).strip()


def _as_argv(value: Any) -> list[str]:
    """Read `memory.command` as an argv list.

    A list is canonical and the only form that survives a Windows path with
    spaces intact. A bare string is accepted because it is what people type,
    but it is split on whitespace only — no shell quoting, because nothing here
    ever reaches a shell and pretending otherwise would invite a config that
    looks quoted and is not.
    """
    if not value:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    raise MemoryUnavailable(f"memory.command must be a list or a string, got {type(value).__name__}")


# ----------------------------------------------------------------------
# MemPalace-facing wrapper
# ----------------------------------------------------------------------


@dataclass
class MemorySettings:
    url: str = ""
    # argv for a stdio MCP server, run as a child process. The alternative to
    # `url`, not an addition to it: a stdio server has no address, and an HTTP
    # one is already running. When both are set `command` wins, and `describe`
    # says which transport is live so the ignored key is visible rather than
    # silently dropped.
    command: list[str] = field(default_factory=list)
    # Bearer token for an HTTP palace. `mempalace serve` requires one on any
    # non-loopback bind, so without this the remote case cannot connect at all.
    # `tokenEnv` names an environment variable and is the form to prefer:
    # config.json is a file this project tells you to commit, and a token in it
    # is a credential in your history. Same reasoning as `apiKeyEnv` on models.
    token: str = ""
    token_env: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    room: str = ""
    enabled: bool = True
    # Name the search tool explicitly to skip discovery, for a server whose
    # naming does not match the hints above.
    search_tool: str = ""
    limit: int = 6
    # Cap on retrieved context. Memory competes with the spec for the
    # executor's context window, and the spec must always win.
    max_tokens: int = 1200
    timeout: int = 30

    # --- write-back -------------------------------------------------
    # Off by default, and deliberately not implied by setting `url`.
    # Retrieval is read-only; write-back mutates a durable store that every
    # future session reads, with no undo. That deserves its own yes.
    write: bool = False
    write_tool: str = ""
    # Log what would be written without sending it. The honest way to find out
    # what the recorder considers durable before letting it near the palace.
    dry_run: bool = False
    # Hard cap on one entry. Memory is for decisions, not transcripts.
    max_write_chars: int = 2000

    @classmethod
    def from_config(cls, data: dict[str, Any] | None, *, room: str = "") -> "MemorySettings":
        data = data or {}
        return cls(
            url=str(data.get("url", "")),
            command=_as_argv(data.get("command")),
            token=str(data.get("token", "")),
            token_env=str(data.get("tokenEnv", "")),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            room=str(data.get("room", "") or room),
            enabled=bool(data.get("enabled", True)),
            search_tool=str(data.get("searchTool", "")),
            limit=int(data.get("limit", 6)),
            max_tokens=int(data.get("maxTokens", 1200)),
            timeout=int(data.get("timeout", 30)),
            write=bool(data.get("write", False)),
            write_tool=str(data.get("writeTool", "")),
            dry_run=bool(data.get("dryRun", False)),
            max_write_chars=int(data.get("maxWriteChars", 2000)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url or self.command) and self.enabled

    @property
    def transport(self) -> str:
        return "stdio" if self.command else "http"

    def request_headers(self) -> dict[str, str]:
        """Headers for an HTTP palace, with the bearer token resolved.

        An explicitly configured Authorization header wins over the token
        fields: someone who wrote the header by hand meant it.
        """
        headers = dict(self.headers)
        token = os.environ.get(self.token_env, "") if self.token_env else self.token
        if token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @property
    def target(self) -> str:
        """What this memory points at, for logs and `forge doctor`."""
        return " ".join(self.command) if self.command else self.url

    @property
    def writes_enabled(self) -> bool:
        return self.configured and self.write


class MemoryClient:
    """Retrieves project context, degrading to nothing when unavailable."""

    def __init__(self, settings: MemorySettings):
        self.settings = settings
        self._client: _MCPSession | None = None
        self._tool: dict[str, Any] | None = None
        self._write_tool: dict[str, Any] | None = None
        self._available_tools: list[str] = []

    @classmethod
    def from_config(cls, data: dict[str, Any] | None, *, room: str = "") -> "MemoryClient | None":
        settings = MemorySettings.from_config(data, room=room)
        return cls(settings) if settings.configured else None

    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        client: _MCPSession
        if self.settings.command:
            client = StdioMCPClient(self.settings.command, timeout=self.settings.timeout)
        else:
            client = MCPClient(
                self.settings.url,
                timeout=self.settings.timeout,
                headers=self.settings.request_headers(),
            )
        client.connect()
        tools = client.list_tools()
        self._available_tools = [str(t.get("name", "")) for t in tools]
        self._tool = _pick_search_tool(tools, self.settings.search_tool)
        if self._tool is None:
            # Under stdio this connection is a live child process, so giving up
            # has to reap it. Leaking one per attempt would leave a run trailing
            # orphaned palaces for as long as it kept retrying.
            client.close()
            raise MemoryUnavailable(
                "no search-like tool found on the memory server. "
                f"It exposes: {', '.join(self._available_tools) or '(none)'}. "
                "Set memory.searchTool in .hybridforge/config.json to name one."
            )
        # Resolved only when writes are on, so a read-only deployment never
        # fails to connect just because the server exposes no write tool.
        if self.settings.write:
            self._write_tool = _pick_write_tool(tools, self.settings.write_tool)
        self._client = client

    def describe(self) -> str:
        """One-line report for `forge doctor`."""
        try:
            self._ensure_connected()
        except MemoryUnavailable as exc:
            return (
                f"FAIL memory transport={self.settings.transport} "
                f"target={self.settings.target} error={exc}"
            )

        if not self.settings.write:
            write_state = "write=off"
        elif self._write_tool is None:
            write_state = "write=ON but NO WRITE TOOL FOUND — set memory.writeTool"
        elif self.settings.dry_run:
            write_state = f"write=dry-run({self._write_tool.get('name')})"
        else:
            write_state = f"write=ON({self._write_tool.get('name')})"

        return (
            f"ok memory transport={self.settings.transport} target={self.settings.target} "
            f"room={self.settings.room or '(unscoped)'} "
            f"read={self._tool.get('name')} {write_state} "
            f"available={', '.join(self._available_tools)}"
        )

    def close(self) -> None:
        """Shut down the transport. A no-op over HTTP; reaps the child on stdio."""
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def search(self, query: str) -> str:
        """Return relevant memory as text, or "" when there is none.

        Raises MemoryUnavailable only for genuine failures; the loop catches it and
        continues without context.
        """
        self._ensure_connected()
        assert self._client is not None and self._tool is not None

        arguments = _build_arguments(
            self._tool,
            query=query,
            room=self.settings.room,
            limit=self.settings.limit,
        )
        text = self._client.call_tool(str(self._tool["name"]), arguments)
        return _truncate(text, self.settings.max_tokens)

    def remember(self, entry: str, *, title: str = "") -> str:
        """Record a durable outcome. Returns a description of what happened.

        Refuses rather than writes when anything looks wrong. Memory is read
        into every future ticket's prompt, so a bad entry is not a one-off
        mistake — it is a mistake that keeps arriving.
        """
        if not self.settings.write:
            return "skipped (memory.write is off)"

        entry = (entry or "").strip()
        if not entry:
            return "skipped (nothing to record)"

        if len(entry) > self.settings.max_write_chars:
            return (
                f"refused ({len(entry)} chars exceeds maxWriteChars="
                f"{self.settings.max_write_chars}; memory is for decisions, "
                "not transcripts)"
            )

        # A diff can contain a credential, and the recorder summarizes diffs.
        # An entry that leaks one would then be replayed into every future
        # prompt, so this refuses instead of sanitizing — a redacted entry
        # would hide that the secret was ever in the working tree.
        leaked = find_secrets(entry)
        if leaked:
            raise MemoryRefused(
                f"refusing to write an entry containing what looks like a "
                f"credential ({', '.join(leaked)}). Nothing was sent."
            )

        self._ensure_connected()
        assert self._client is not None

        if self._write_tool is None:
            raise MemoryUnavailable(
                "memory.write is on but no write-like tool was found. "
                f"The server exposes: {', '.join(self._available_tools) or '(none)'}. "
                "Set memory.writeTool to name one."
            )

        arguments = _build_write_arguments(
            self._write_tool, entry=entry, room=self.settings.room, title=title
        )

        if self.settings.dry_run:
            return (
                f"dry-run (would call {self._write_tool['name']} with "
                f"{json.dumps(arguments)[:400]})"
            )

        self._client.call_tool(str(self._write_tool["name"]), arguments)
        return f"recorded via {self._write_tool['name']} ({len(entry)} chars)"


def _pick_search_tool(
    tools: list[dict[str, Any]], preferred: str = ""
) -> dict[str, Any] | None:
    """Choose the retrieval tool, never a mutating one."""
    by_name = {str(tool.get("name", "")): tool for tool in tools}

    if preferred:
        return by_name.get(preferred)

    readable = [
        tool
        for tool in tools
        if not any(hint in str(tool.get("name", "")).lower() for hint in _WRITE_HINTS)
    ]
    for hint in _SEARCH_HINTS:
        for tool in readable:
            if hint in str(tool.get("name", "")).lower():
                return tool
    return None


def _schema_properties(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _build_arguments(
    tool: dict[str, Any], *, query: str, room: str, limit: int
) -> dict[str, Any]:
    """Fill only the parameters this tool actually declares.

    Sending an unexpected parameter is a validation error on strict servers, so
    the schema decides what goes in rather than a fixed shape.
    """
    properties = _schema_properties(tool)
    arguments: dict[str, Any] = {}

    if properties:
        for name in _QUERY_PARAMS:
            if name in properties:
                arguments[name] = query
                break
        else:
            # Schema present but no recognizable query field: use the single
            # required string parameter if there is exactly one.
            required = [
                name
                for name in (tool.get("inputSchema") or {}).get("required", [])
                if properties.get(name, {}).get("type") == "string"
            ]
            if len(required) == 1:
                arguments[required[0]] = query

        if room:
            for name in _ROOM_PARAMS:
                if name in properties:
                    arguments[name] = room
                    break
        for name in _LIMIT_PARAMS:
            if name in properties:
                arguments[name] = limit
                break
    else:
        # No schema advertised — send the most common shape and hope.
        arguments["query"] = query
        if room:
            arguments["room"] = room

    return arguments


def _truncate(text: str, max_tokens: int) -> str:
    """Trim retrieved context to its token budget, on a paragraph boundary."""
    text = (text or "").strip()
    if not text or estimate_text(text) <= max_tokens:
        return text

    kept: list[str] = []
    used = 0
    for paragraph in text.split("\n\n"):
        cost = estimate_text(paragraph)
        if used + cost > max_tokens:
            break
        kept.append(paragraph)
        used += cost

    if not kept:  # A single oversized paragraph: cut it by characters.
        return text[: int(max_tokens * 3.3)].rstrip() + "\n\n[memory truncated]"
    return "\n\n".join(kept) + "\n\n[memory truncated]"


def ticket_query(title: str, spec: str, allowed_files: list[str]) -> str:
    """Build a narrow retrieval query for a ticket.

    Narrow on purpose: the project-memory discipline is that a ticket about the
    export pipeline should not pull the whole project history. Irrelevant
    context spends the executor's attention and invites inconsistency.
    """
    parts = [title.strip()]
    first_paragraph = spec.strip().split("\n\n")[0] if spec.strip() else ""
    if first_paragraph:
        parts.append(first_paragraph)
    if allowed_files:
        parts.append("Files: " + ", ".join(allowed_files[:8]))
    return "\n".join(p for p in parts if p).strip()


# ----------------------------------------------------------------------
# Write-back
# ----------------------------------------------------------------------


def _pick_write_tool(
    tools: list[dict[str, Any]], preferred: str = ""
) -> dict[str, Any] | None:
    """Choose the recording tool, never a destructive one.

    An explicit `preferred` name still cannot select a forbidden tool. Naming
    `delete_memories` in config is far more likely to be a typo than an
    intention, and the loop calls this unattended at 3am.
    """
    by_name = {str(tool.get("name", "")): tool for tool in tools}

    if preferred:
        if any(bad in preferred.lower() for bad in _WRITE_HINTS_FORBIDDEN):
            raise MemoryRefused(
                f"memory.writeTool names {preferred!r}, which looks destructive. "
                "Refusing to use it."
            )
        return by_name.get(preferred)

    safe = [
        tool
        for tool in tools
        if not any(
            bad in str(tool.get("name", "")).lower() for bad in _WRITE_HINTS_FORBIDDEN
        )
    ]
    for hint in _WRITE_HINTS_POSITIVE:
        for tool in safe:
            if hint in str(tool.get("name", "")).lower():
                return tool
    return None


def _build_write_arguments(
    tool: dict[str, Any], *, entry: str, room: str, title: str
) -> dict[str, Any]:
    """Fill only the parameters the write tool declares.

    Refuses rather than guessing when the entry text has nowhere obvious to go —
    sending a memory into the wrong field could silently create a malformed
    record that later reads back as authoritative.
    """
    properties = _schema_properties(tool)
    arguments: dict[str, Any] = {}

    if not properties:
        arguments["content"] = entry
        if room:
            arguments["room"] = room
        if title:
            arguments["title"] = title
        return arguments

    for name in _ENTRY_PARAMS:
        if name in properties:
            arguments[name] = entry
            break
    else:
        required = [
            name
            for name in (tool.get("inputSchema") or {}).get("required", [])
            if properties.get(name, {}).get("type") == "string"
        ]
        if len(required) == 1:
            arguments[required[0]] = entry
        else:
            raise MemoryRefused(
                f"cannot tell which parameter of {tool.get('name')!r} takes the "
                f"entry text (it declares: {', '.join(properties) or 'nothing'}). "
                "Nothing was sent; set memory.writeTool or disable memory.write."
            )

    if title:
        for name in _TITLE_PARAMS:
            if name in properties:
                arguments[name] = title
                break
    if room:
        for name in _ROOM_PARAMS:
            if name in properties:
                arguments[name] = room
                break

    return arguments
