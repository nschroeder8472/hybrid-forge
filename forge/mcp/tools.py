"""The tool definitions a host sees: names, descriptions, input schemas, handlers.

One table, read by both the MCP server and the `forge tool` CLI mirror, so the
two cannot describe the same operation differently. The descriptions are the
host's documentation — they are what a model reads when deciding which tool to
call — so they say when to use a tool, not only what it does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..api import Forge


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[Forge, dict[str, Any]], dict[str, Any]]


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_BOOLEAN = {"type": "boolean"}


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="forge_status",
        description=(
            "The project at a glance: which model sits in each seat (executor, "
            "tester, reviewer, planner), the verify commands the project runs, "
            "the current run's tickets and their state, captured baselines, and "
            "token usage per model. Call this first. `probe: true` also asks "
            "every model whether it answers, which is slower."
        ),
        schema=_object({"probe": _BOOLEAN}),
        handler=lambda forge, a: forge.status(probe=bool(a.get("probe", False))),
    ),
    Tool(
        name="forge_inspect",
        description=(
            "Read the ledger, read-only. `step`: one step's full output by id. "
            "`ticket`: a ticket's contract, history and step list (no outputs — "
            "follow a step id for those). `events`: the newest N events. Nothing: "
            "the list of runs. `run` picks which run; default is the current one."
        ),
        schema=_object(
            {
                "ticket": _STRING,
                "step": _INTEGER,
                "run": _INTEGER,
                "events": _INTEGER,
                "limit": _INTEGER,
            }
        ),
        handler=lambda forge, a: forge.inspect(
            ticket=str(a.get("ticket", "")),
            step=int(a.get("step", 0) or 0),
            run=int(a.get("run", 0) or 0),
            events=int(a.get("events", 0) or 0),
            **({"limit": int(a["limit"])} if a.get("limit") else {}),
        ),
    ),
    Tool(
        name="forge_verify",
        description=(
            "Run the project's lint, typecheck and test commands now, with "
            "failure attribution. Before editing files yourself, call with "
            "`capture: \"<label>\"` to record what already fails; afterwards call "
            "with `against: \"<label>\"` and each failure is reported as "
            "`introduced` (yours) or `already` (pre-existing). `clean: true` "
            "means nothing introduced. A step with `environment` set never "
            "reached the code — fix the machine, not the code. `ticket` narrows "
            "to the build that ticket writes to."
        ),
        schema=_object({"capture": _STRING, "against": _STRING, "ticket": _STRING}),
        handler=lambda forge, a: forge.verify(
            capture=str(a.get("capture", "")),
            against=str(a.get("against", "")),
            ticket=str(a.get("ticket", "")),
        ),
    ),
)


BY_NAME = {tool.name: tool for tool in TOOLS}
