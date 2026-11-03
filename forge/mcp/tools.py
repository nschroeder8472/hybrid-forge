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
    Tool(
        name="forge_contract",
        description=(
            "Open or revise a ticket — the contract delegates build against. To "
            "open: `id`, `spec`, `allowed_files` (what may be written), "
            "`criteria` (what the tester encodes; never 'tests pass', the harness "
            "runs those), optional `reference_files`, `needs`, `kind` "
            "(code|bug|image), `route` (delegate | withheld:<reason>). Or "
            "`spec_file`: a ticket-shaped markdown document, opened verbatim as "
            "a person's words. To revise: an existing `id`, the fields to change, "
            "and `reason`. Revisions are ratcheted: numbers a person wrote never "
            "move, a contract may never excuse a failing check, and once an "
            "attempt exists a criterion can be sharpened but never dropped. "
            "Returns the contract as delegates will see it (reading scope "
            "widened) and warnings worth fixing before delegating."
        ),
        schema=_object(
            {
                "id": _STRING,
                "spec_file": _STRING,
                "reason": _STRING,
                "title": _STRING,
                "kind": {"type": "string", "enum": ["code", "bug", "image"]},
                "spec": _STRING,
                "criteria": {"type": "array", "items": _STRING},
                "allowed_files": {"type": "array", "items": _STRING},
                "reference_files": {"type": "array", "items": _STRING},
                "needs": {"type": "array", "items": _STRING},
                "route": _STRING,
                "context": _STRING,
            }
        ),
        handler=lambda forge, a: forge.contract(**a),
    ),
    Tool(
        name="forge_delegate",
        description=(
            "Have the delegate seats build one ticket: executor writes it, tester "
            "encodes the criteria, verify commands run with attribution, reviewer "
            "reads the diff. Every scope, parse, import, manifest and baseline "
            "gate applies. Runs in the background: returns a job, or the result "
            "if it ends within `wait` seconds. Result `status`: landed | failed | "
            "blocked (the executor's BLOCKED note says what it needs) | halted "
            "(the tree or toolchain cannot verify anything — fix that first) | "
            "cancelled. Failed and blocked work is set aside, not left in the "
            "tree. `seats` overrides a delegate seat's model for this call, e.g. "
            "{\"executor\": \"qwen\"}. Delegating a failed ticket again is a retry."
        ),
        schema=_object(
            {
                "id": _STRING,
                "attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                "seats": {"type": "object", "additionalProperties": _STRING},
                "wait": {"type": "number", "minimum": 0, "maximum": 1800},
            },
            required=("id",),
        ),
        handler=lambda forge, a: forge.delegate(
            id=str(a["id"]),
            attempts=int(a.get("attempts", 3)),
            seats=dict(a.get("seats") or {}),
            wait=float(a.get("wait", 0) or 0),
        ),
    ),
    Tool(
        name="forge_job",
        description=(
            "A delegation's progress or result. `wait` blocks up to that many "
            "seconds for it to finish. `cancel: true` stops it after the current "
            "step — never mid-patch — and returns the ticket to pending."
        ),
        schema=_object(
            {
                "job": _INTEGER,
                "wait": {"type": "number", "minimum": 0, "maximum": 1800},
                "cancel": _BOOLEAN,
            },
            required=("job",),
        ),
        handler=lambda forge, a: forge.job(
            int(a["job"]), wait=float(a.get("wait", 0) or 0), cancel=bool(a.get("cancel"))
        ),
    ),
    Tool(
        name="forge_close",
        description=(
            "Settle a ticket. `land`: accept a done ticket (`commit: true` commits "
            "its scope). `abandon`: give it up, with `argument` saying why; its "
            "set-aside work stays salvageable. `overrule`: accept a ticket the "
            "reviewer rejected — `argument` must cite the path:line that answers "
            "the reviewer; the work is restored and re-verified, and anything "
            "red refuses it. Every overrule is counted. `discharge`: you "
            "implemented the ticket yourself; `against` names the forge_verify "
            "baseline you captured before starting, and nothing may be introduced."
        ),
        schema=_object(
            {
                "id": _STRING,
                "outcome": {"type": "string",
                            "enum": ["land", "abandon", "overrule", "discharge"]},
                "argument": _STRING,
                "against": _STRING,
                "commit": _BOOLEAN,
            },
            required=("id", "outcome"),
        ),
        handler=lambda forge, a: forge.close_ticket(
            id=str(a["id"]),
            outcome=str(a["outcome"]),
            argument=str(a.get("argument", "")),
            against=str(a.get("against", "")),
            commit=bool(a.get("commit", False)),
        ),
    ),
)


BY_NAME = {tool.name: tool for tool in TOOLS}
