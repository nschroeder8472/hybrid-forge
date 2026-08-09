#!/usr/bin/env python3
"""
forge-executor: a thin MCP server exposing a local OpenAI-compatible model
(Qwen3.6-35B-A3B served by Ollama or vLLM) as a delegation tool.

This is the shim that lets Claude hand an implementation ticket to the local
model. It deliberately does NOT let the executor pick its own scope: the caller
must supply an explicit spec, the files it may touch, and acceptance criteria.

Transport: stdio. Runs on whichever machine Claude Code is running on and
reaches the executor host over the network (Tailscale), so no state lives here.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("FORGE_EXECUTOR_BASE_URL", "http://localhost:11434/v1").rstrip("/")
MODEL = os.environ.get("FORGE_EXECUTOR_MODEL", "qwen3.6:35b-a3b")
TIMEOUT = int(os.environ.get("FORGE_EXECUTOR_TIMEOUT", "600"))
API_KEY = os.environ.get("FORGE_EXECUTOR_API_KEY", "")

mcp = FastMCP("forge-executor")

SYSTEM_PROMPT = """You are the executor in a plan-and-execute pipeline.

A senior engineer has already made the design decisions. Your job is to
implement the spec exactly as written, not to redesign it.

Rules:
- Only modify files listed in the allowed scope. Never touch anything else.
- Implement every acceptance criterion. Do not add unrequested features.
- Use the libraries and signatures named in the spec.
- If the spec is ambiguous or you believe it is wrong, DO NOT guess.
  Return a block starting with BLOCKED: and explain precisely what is unclear.
- Output complete file contents for each file you change, each in a fenced
  code block preceded by the file path on its own line.
"""


def _post(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _complete(user_content: str, temperature: float) -> str:
    try:
        data = _post(
            {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": temperature,
                "max_tokens": 8192,
            }
        )
    except urllib.error.URLError as exc:
        return (
            f"EXECUTOR_UNREACHABLE: could not reach {BASE_URL} ({exc}). "
            "Check that the executor host is up and reachable on this network."
        )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return f"EXECUTOR_BAD_RESPONSE: {json.dumps(data)[:2000]}"


@mcp.tool()
def delegate_implementation(
    ticket_id: str,
    spec: str,
    allowed_files: list[str],
    acceptance_criteria: list[str],
    context: str = "",
) -> str:
    """Hand a fully specified implementation ticket to the local executor model.

    Use this only when the spec is complete: interfaces named, libraries chosen,
    and acceptance criteria explicit. Do not use it for design work, for changes
    touching auth/concurrency/public API surface, or for anything where the
    correct approach is still an open question — do those yourself.

    Args:
        ticket_id: Stable identifier, e.g. IM-014.
        spec: The implementation spec. Signatures, libraries, expected behavior.
        allowed_files: Exact paths the executor may create or modify.
        acceptance_criteria: Assertions that must hold when the work is done.
        context: Relevant prior decisions, usually retrieved from MemPalace.
    """
    body = f"""Ticket: {ticket_id}

## Established project context
{context or "(none supplied)"}

## Spec
{spec}

## Allowed scope (do not modify anything outside this list)
{chr(10).join(f"- {p}" for p in allowed_files)}

## Acceptance criteria
{chr(10).join(f"- {c}" for c in acceptance_criteria)}

Implement this now."""
    return _complete(body, temperature=0.2)


@mcp.tool()
def delegate_tests(
    ticket_id: str,
    acceptance_criteria: list[str],
    target_files: list[str],
    test_file: str,
    framework: str,
    context: str = "",
) -> str:
    """Have the executor write tests for criteria that were defined upstream.

    The executor implements assertions it is given; it does not decide what to
    assert. Always pass acceptance_criteria authored during planning, never
    criteria the executor produced itself.

    Args:
        ticket_id: Stable identifier, e.g. IM-014.
        acceptance_criteria: The assertions to encode as tests.
        target_files: Source files under test.
        test_file: Path the test file should be written to.
        framework: Test framework and any conventions to follow.
        context: Relevant prior decisions, usually retrieved from MemPalace.
    """
    body = f"""Ticket: {ticket_id}

## Established project context
{context or "(none supplied)"}

## Task
Write tests in {test_file} using {framework}, covering the files below.
Encode EXACTLY the criteria listed. Do not invent additional criteria, and do
not weaken a criterion to make it easier to satisfy.

## Files under test
{chr(10).join(f"- {p}" for p in target_files)}

## Criteria to encode as assertions
{chr(10).join(f"- {c}" for c in acceptance_criteria)}
"""
    return _complete(body, temperature=0.1)


@mcp.tool()
def executor_health() -> str:
    """Check whether the executor host is reachable and which model answers."""
    result = _complete("Reply with exactly: OK", temperature=0.0)
    return f"endpoint={BASE_URL} model={MODEL} reply={result.strip()[:200]}"


if __name__ == "__main__":
    mcp.run()
