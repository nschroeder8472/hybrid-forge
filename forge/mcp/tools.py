"""The shape of a tool the MCP server presents. The tools themselves live in
`forge.smith.tools`; the descriptions there are what a model reads when
deciding whether to pick one up."""

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
