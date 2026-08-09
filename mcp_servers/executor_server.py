#!/usr/bin/env python3
"""forge-executor: the delegation tool for interactive sessions.

This is the *manual* path — Claude Code drives, and calls these tools when it
decides to hand work off. The autonomous path is the daemon (`forge go`), which
owns its own loop and does not use MCP at all.

Both share the provider layer, so a model configured for the loop is reachable
here with no second configuration. When `.hybridforge/config.json` exists, this
server uses the model in its `executor` role; otherwise it falls back to the
environment variables, so it still works in a repo that has not been
initialized.

Transport: stdio. Stateless — runs on whichever machine Claude Code runs on and
reaches the model over the network.
"""

import os
import sys
from pathlib import Path

# Import the provider layer from the plugin checkout without requiring an
# install, so `claude --plugin-dir` works on a fresh clone.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from forge.prompts import EXECUTOR_SYSTEM, TESTER_SYSTEM  # noqa: E402
from forge.providers import Message, ProviderError, build_provider  # noqa: E402

mcp = FastMCP("forge-executor")


def _provider():
    """Resolve the executor model: project config first, then environment."""
    try:
        from forge.config import Config

        config = Config.load(os.environ.get("FORGE_PROJECT_ROOT", "."))
        return config.provider_for("executor"), config.model_name_for("executor")
    except Exception:  # noqa: BLE001 - no project config is a normal case here
        name = "env"
        return (
            build_provider(
                name,
                {
                    "kind": os.environ.get("FORGE_EXECUTOR_KIND", "openai"),
                    "baseUrl": os.environ.get(
                        "FORGE_EXECUTOR_BASE_URL", "http://localhost:11434/v1"
                    ),
                    "model": os.environ.get("FORGE_EXECUTOR_MODEL", "qwen3.6:35b-a3b"),
                    "apiKeyEnv": "FORGE_EXECUTOR_API_KEY",
                },
            ),
            name,
        )


def _complete(system: str, body: str, temperature: float) -> str:
    try:
        provider, name = _provider()
        completion = provider.complete(
            [Message(role="system", content=system), Message(role="user", content=body)],
            max_tokens=provider.capabilities().max_output_tokens,
            temperature=temperature,
        )
    except ProviderError as exc:
        return f"EXECUTOR_UNREACHABLE: {type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface config errors to the caller
        return f"EXECUTOR_MISCONFIGURED: {exc}"

    if completion.truncated:
        return (
            f"{completion.text}\n\n"
            "EXECUTOR_TRUNCATED: the model hit its output limit; this result is incomplete."
        )
    return completion.text


@mcp.tool()
def delegate_implementation(
    ticket_id: str,
    spec: str,
    allowed_files: list[str],
    acceptance_criteria: list[str],
    context: str = "",
) -> str:
    """Hand a fully specified implementation ticket to the executor model.

    Use this only when the spec is complete: interfaces named, libraries chosen,
    and acceptance criteria explicit. Do not use it for design work, for changes
    touching auth/concurrency/public API surface, or for anything where the
    correct approach is still an open question — do those yourself.

    Args:
        ticket_id: Stable identifier, e.g. IM-014.
        spec: The implementation spec. Signatures, libraries, expected behavior.
        allowed_files: Exact paths the executor may create or modify.
        acceptance_criteria: Assertions that must hold when the work is done.
        context: Relevant prior decisions, usually retrieved from project memory.
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
    return _complete(EXECUTOR_SYSTEM, body, temperature=0.2)


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
        context: Relevant prior decisions, usually retrieved from project memory.
    """
    body = f"""Ticket: {ticket_id}

## Established project context
{context or "(none supplied)"}

## Task
Write tests in {test_file} using {framework}, covering the files below.

## Files under test
{chr(10).join(f"- {p}" for p in target_files)}

## Criteria to encode as assertions
{chr(10).join(f"- {c}" for c in acceptance_criteria)}
"""
    return _complete(TESTER_SYSTEM, body, temperature=0.1)


@mcp.tool()
def executor_health() -> str:
    """Check whether the executor model is reachable and which one answers."""
    try:
        provider, name = _provider()
    except Exception as exc:  # noqa: BLE001
        return f"EXECUTOR_MISCONFIGURED: {exc}"
    caps = provider.capabilities()
    return (
        f"{provider.health()} context={caps.context_window} "
        f"max_output={caps.max_output_tokens} (role=executor, model={name})"
    )


if __name__ == "__main__":
    mcp.run()
