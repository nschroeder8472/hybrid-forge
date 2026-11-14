"""A Model Context Protocol server over stdio, standard library only.

The protocol here is newline-delimited JSON-RPC 2.0 on stdin and stdout. A
host (Claude Code, or any MCP client) starts `forge mcp` as a subprocess and
calls the tools in `forge.smith.tools`. Stdout carries protocol messages only;
anything a person should see goes to stderr.

Only what a tool server needs is implemented: `initialize`, `ping`,
`tools/list` and `tools/call`. Notifications are accepted and ignored.

Stdlib-only because the install promise — `pip install -e .`, not a
deployment — is worth more than an SDK dependency for a few hundred lines.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import IO, Any

from .. import __version__
from ..api import ApiError, Forge
from .tools import Tool

# The newest revision this server was written against. A client asking for
# another is answered with its own, since nothing used here differs between
# the revisions that exist.
PROTOCOL_VERSION = "2025-06-18"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "hybrid-forge, the smith's tools: anvil (run the project's checks with "
    "failures attributed to whoever caused them; mark a baseline first), gauge "
    "(measure images and the tree; contact sheets), mold (render art candidates "
    "with the project's image model, finished and measured, for you to choose "
    "from). Every tool is optional; none of them decides anything for you."
)


def kit() -> tuple[Tool, ...]:
    """The tools this server presents."""
    from ..smith.tools import SMITH_TOOLS

    return SMITH_TOOLS


def by_name() -> dict[str, Tool]:
    return {tool.name: tool for tool in kit()}


class Server:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.tools = kit()

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        ident = message.get("id")
        if ident is None:
            return None  # a notification: nothing to answer
        params = message.get("params") or {}
        try:
            if method == "initialize":
                return _result(ident, self._initialize(params))
            if method == "ping":
                return _result(ident, {})
            if method == "tools/list":
                return _result(ident, {"tools": [_describe(t) for t in self.tools]})
            if method == "tools/call":
                return _result(ident, self._call(params))
            return _error(ident, _METHOD_NOT_FOUND, f"unknown method {method!r}")
        except _InvalidParams as exc:
            return _error(ident, _INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - a server answers, it never dies
            traceback.print_exc(file=sys.stderr)
            return _error(ident, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hybrid-forge", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = (
            next((t for t in self.tools if t.name == name), None)
            if isinstance(name, str) else None
        )
        if tool is None:
            raise _InvalidParams(f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _InvalidParams("arguments must be an object")
        return call_tool(self.root, tool.name, arguments)

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def serve(self, reader: IO[str], writer: IO[str]) -> None:
        for line in reader:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError as exc:
                reply: dict[str, Any] | None = _error(None, _PARSE_ERROR, str(exc))
            else:
                reply = self.handle(message)
            if reply is not None:
                writer.write(json.dumps(reply, ensure_ascii=False) + "\n")
                writer.flush()


def call_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool and wrap its answer as an MCP tool result.

    A refusal (`ApiError`) is a tool result with `isError`, not a protocol
    error: it is an answer the host is meant to read and act on, such as "no
    baseline by that name". Each call opens the project fresh, so an edited
    config is read on the next call rather than after a restart.
    """
    tool = by_name()[name]
    unknown = sorted(set(arguments) - set(tool.schema.get("properties", {})))
    if unknown:
        # Refused rather than ignored: a misspelt argument silently dropped is
        # a host that believes it asked for something it did not get.
        return _tool_error(
            f"{name} does not take {', '.join(unknown)}; "
            f"it takes: {', '.join(tool.schema['properties']) or 'nothing'}"
        )
    missing = [k for k in tool.schema.get("required", []) if k not in arguments]
    if missing:
        return _tool_error(f"{name} needs {', '.join(missing)}")
    try:
        forge = Forge(root)
    except ApiError as exc:
        return _tool_error(str(exc))
    try:
        payload = tool.handler(forge, arguments)
    except ApiError as exc:
        return _tool_error(str(exc))
    except (TypeError, ValueError) as exc:
        return _tool_error(f"bad arguments for {name}: {exc}")
    finally:
        forge.close()
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


class _InvalidParams(Exception):
    pass


def _describe(tool: Any) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "inputSchema": tool.schema}


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def main(root: str | Path = ".") -> int:
    # Stdout is the protocol channel; force UTF-8 and line endings the client
    # expects regardless of the console code page.
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    Server(root).serve(stdin, stdout)
    return 0
